"""Model selection and explicit method capabilities, without importing ML packages."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    pretrained: str | None
    extra: str | None
    methods: tuple[str, ...]
    restriction: str = ''


ALL_METHODS = ('standard', 'rtc', 'training_time_rtc', 'vlash')
COMMON_METHODS = ('standard', 'vlash')
MODELS = {
    'flow': ModelSpec('flow', None, None, ALL_METHODS),
    'act': ModelSpec('act', None, None, COMMON_METHODS,
                     'ACT is a CVAE; flow RTC and per-token denoising are not defined.'),
    'diffusion': ModelSpec('diffusion', None, 'diffusion', ('standard', 'rtc', 'vlash'),
                           'This temporal UNet uses a global noise timestep; Training-time RTC needs per-token noise times.'),
    'smolvla': ModelSpec('smolvla', 'lerobot/smolvla_base', 'smolvla', ALL_METHODS),
    'pi0': ModelSpec('pi0', 'lerobot/pi0_base', 'pi', ALL_METHODS),
    'pi05': ModelSpec('pi05', 'lerobot/pi05_base', 'pi', ALL_METHODS),
    'pi0_fast': ModelSpec('pi0_fast', 'lerobot/pi0fast-base', 'pi', COMMON_METHODS,
                          'FAST generates discrete action tokens; flow RTC does not apply.'),
    'groot': ModelSpec('groot', 'nvidia/GR00T-N1.7-3B', 'groot', ALL_METHODS),
    'xvla': ModelSpec('xvla', 'lerobot/xvla-base', 'xvla', ALL_METHODS),
}


def require_method(model, method):
    spec = MODELS[model]
    if method not in spec.methods:
        raise ValueError(f'{model} + {method} is unsupported: {spec.restriction} '
                         f'Supported methods: {", ".join(spec.methods)}. A2C2 can be added to each.')
    return spec


def catalog():
    return [{**asdict(spec), 'a2c2': True} for spec in MODELS.values()]
