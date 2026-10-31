from collections import deque
from typing import Protocol
import numpy as np
from .action_smoothing import ActionSmoother


class PolicyInterface(Protocol):
    def reset(self): ...
    def predict(self, observation): ...


class HoldPolicy:
    """Smoke-test policy: hold the observed position, never a learned policy."""
    def reset(self):
        pass

    def predict(self, observation):
        return np.asarray(observation["observation.state"])[None, :]


class ChunkScheduler:
    def __init__(self, n_action_steps, method="standard", correction="none",
                 smoothing='none', joint_names=None):
        if method != "standard" or correction != "none":
            raise ValueError("Only standard/none is implemented; RTC/VLASH/A2C2 require separate adapters")
        if n_action_steps < 1:
            raise ValueError("n_action_steps must be positive")
        self.n_action_steps = n_action_steps
        self.smoothing = smoothing
        self.smoother = ActionSmoother(smoothing, joint_names)
        self.queue = deque()

    def reset(self):
        self.queue.clear()
        self.smoother.reset()

    def next(self, policy, observation):
        if not self.queue:
            chunk = np.asarray(policy.predict(observation))
            if chunk.ndim != 2 or not len(chunk) or not np.isfinite(chunk).all():
                raise ValueError("Policy must return a finite [time,joints] action chunk")
            chunk = self.smoother.chunk(chunk)
            self.queue.extend(chunk[:self.n_action_steps])
        return self.queue.popleft().copy()


class ACTPolicy:
    def __init__(self, checkpoint, device="cuda"):
        import torch
        from .act import load_checkpoint
        self.torch, self.device = torch, device
        self.policy, raw_stats = load_checkpoint(checkpoint, device)
        self.stats = {k: torch.tensor(v, dtype=torch.float32, device=device) for k, v in raw_stats.items()}
        if any(not torch.isfinite(v).all() for v in self.stats.values()) or any(
                (self.stats[k] <= 0).any() for k in ("state_std", "action_std")):
            raise ValueError("Invalid checkpoint normalization")

    def reset(self):
        pass  # Chunk history is managed by ChunkScheduler.

    def predict(self, observation):
        torch, config = self.torch, self.policy.config
        raw = np.asarray(observation["observation.state"], dtype=np.float32)
        if raw.shape != (len(config["joint_names"]),) or not np.isfinite(raw).all():
            raise ValueError("Invalid policy joint state")
        state = torch.tensor(raw, device=self.device).unsqueeze(0)
        state = (state-self.stats["state_mean"])/self.stats["state_std"]
        images = {}
        for name in config["cameras"]:
            image = observation[f"observation.images.{name}"]
            if image.dtype != np.uint8 or list(image.shape) != config["image_shapes"][name]:
                raise ValueError("Policy image shape/dtype mismatch: "+name)
            images[name] = torch.from_numpy(image.copy()).permute(2, 0, 1).float().unsqueeze(0).to(self.device)/255
        with torch.inference_mode():
            chunk, _, _ = self.policy(state, images)
            chunk = chunk*self.stats["action_std"]+self.stats["action_mean"]
        result = chunk[0].cpu().numpy()
        if not np.isfinite(result).all():
            raise ValueError("Nonfinite ACT action")
        return result
