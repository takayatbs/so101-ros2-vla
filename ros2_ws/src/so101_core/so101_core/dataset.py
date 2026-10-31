"""SO101 Dataset v1: episode NPZ (no pickle) and lossless RGB PNG frames."""
import json
from pathlib import Path
import cv2
import numpy as np

FORMAT = "so101_dataset_v1"
VIDEO_FORMAT = "so101_dataset_v2"
SUPPORTED_FORMATS = (FORMAT, VIDEO_FORMAT)


class DatasetWriter:
    def __init__(self, root, name, fps, joints, image_shapes):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        if not np.isfinite(fps) or fps <= 0:
            raise ValueError("Dataset FPS must be positive")
        self.info = {"format": FORMAT, "name": name, "fps": fps, "joint_names": list(joints),
                     "units": "rad", "image_shapes": {k: list(v) for k, v in image_shapes.items()}, "episodes": []}
        self.states, self.actions, self.timestamps, self.task = [], [], [], None

    def add_frame(self, state, action, images, timestamp, task):
        if set(images) != set(self.info["image_shapes"]):
            raise ValueError("Dataset camera schema mismatch")
        state, action = np.asarray(state, dtype=np.float32), np.asarray(action, dtype=np.float32)
        if any(v.shape != (len(self.info["joint_names"]),) or not np.isfinite(v).all() for v in (state, action)):
            raise ValueError("Invalid dataset joint values")
        if not np.isfinite(timestamp) or (self.timestamps and timestamp <= self.timestamps[-1]):
            raise ValueError("Frame timestamps must be finite and strictly increasing")
        if self.task is not None and task != self.task:
            raise ValueError("Task changed within an episode")
        ep = self.root / f"episodes/{len(self.info['episodes']):06d}"
        frame = len(self.states)
        for name, image in images.items():
            if "/" in name or name in ("", ".", ".."):
                raise ValueError("Invalid camera name")
            if image.dtype != np.uint8 or list(image.shape) != self.info["image_shapes"][name]:
                raise ValueError(f"Invalid dataset image: {name}")
            folder = ep / f"images/{name}"
            folder.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(folder/f"{frame:06d}.png"), image[:, :, ::-1]):
                raise IOError("PNG encoding failed")
        self.states.append(state.copy())
        self.actions.append(action.copy())
        self.timestamps.append(timestamp)
        self.task = task

    def save_episode(self, episode_id):
        if len(self.states) < 2:
            raise ValueError("Episode contains fewer than two aligned frames")
        index = len(self.info["episodes"])
        folder = self.root/f"episodes/{index:06d}"
        folder.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(folder/"frames.npz", state=np.stack(self.states), action=np.stack(self.actions),
                            timestamp=np.array(self.timestamps, dtype=np.float64))
        self.info["episodes"].append({"index": index, "episode_id": episode_id,
                                      "frames": len(self.states), "task": self.task})
        self.states, self.actions, self.timestamps, self.task = [], [], [], None

    def finalize(self):
        if self.states or not self.info["episodes"]:
            raise ValueError("Save complete episodes before finalizing")
        (self.root/"dataset.json").write_text(json.dumps(self.info, indent=2))


class SO101Dataset:
    def __init__(self, root, chunk_size=30, episode_indices=None):
        self.root = Path(root)
        if not (self.root/"dataset.json").is_file():
            raise ValueError("SO101 Dataset v1 required; convert original MCAP with this project")
        self.info = json.loads((self.root/"dataset.json").read_text())
        if self.info.get("format") not in SUPPORTED_FORMATS or self.info.get("units") != "rad":
            raise ValueError("Unsupported dataset format/units")
        if chunk_size < 1:
            raise ValueError("Chunk size must be positive")
        self.chunk_size, self.tables, self.indices = chunk_size, {}, []
        self.video_reader, self.video_indices = None, {}
        self.video_format = self.info["format"] == VIDEO_FORMAT
        if self.video_format:
            from .preprocessing import ImagePreprocessor
            self.preprocessor = ImagePreprocessor.__new__(ImagePreprocessor)
            self.preprocessor.config = self.info["preprocessing"]
            self.preprocessor.calibration = self.info["camera_calibration"]
        selected = set(episode_indices) if episode_indices is not None else {e["index"] for e in self.info["episodes"]}
        if not selected or not selected <= {e["index"] for e in self.info["episodes"]}:
            raise ValueError("Invalid episode selection")
        for episode in self.info["episodes"]:
            index = episode["index"]
            if index not in selected:
                continue
            with np.load(self.root/f"episodes/{index:06d}/frames.npz", allow_pickle=False) as table:
                arrays = {name: table[name].copy() for name in table.files}
            n, d = episode["frames"], len(self.info["joint_names"])
            valid = arrays.get("valid", np.ones(n, dtype=bool))
            if valid.shape != (n,) or valid.dtype != np.bool_ or not valid.any():
                raise ValueError("Invalid episode validity mask")
            if any(arrays[k].shape != (n, d) or not np.isfinite(arrays[k][valid]).all() for k in ("state", "action")):
                raise ValueError("Invalid episode joint arrays")
            if n < 2 or arrays["timestamp"].shape != (n,) or not np.isfinite(arrays["timestamp"]).all() or not (np.diff(arrays["timestamp"]) > 0).all():
                raise ValueError("Invalid episode timestamps")
            self.tables[index] = arrays
            self.indices.extend((index, int(frame)) for frame in np.flatnonzero(valid))
            if self.video_format:
                for name in self.info["image_shapes"]:
                    spec = episode["videos"][name]
                    base = self.root/f"episodes/{index:06d}/videos"
                    for field in ("path", "index_path"):
                        if not (base/spec[field]).resolve().is_relative_to(self.root.resolve()):
                            raise ValueError("Video path escapes dataset")
                    with np.load(base/spec["index_path"], allow_pickle=False) as table:
                        video_index = {k: table[k].copy() for k in table.files}
                    image_indices = arrays["image_index/"+name]
                    if image_indices.shape != (n,) or (image_indices[valid] < 0).any() or (image_indices[valid] >= len(video_index["pts_ns"])).any():
                        raise ValueError("Invalid video frame mapping")
                    if not np.array_equal(arrays["image_time_ns/"+name][valid], video_index["timestamp_ns"][image_indices[valid]]):
                        raise ValueError("Video source timestamps do not match sample mapping")
                    self.video_indices[index,name] = video_index

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, index):
        episode, frame = self.indices[index]
        table = self.tables[episode]
        count = len(table["state"])
        valid = table.get("valid", np.ones(count,dtype=bool))
        invalid = np.flatnonzero(~valid[frame:])
        end = frame+int(invalid[0]) if len(invalid) else count
        chunk_indices = np.minimum(np.arange(frame, frame+self.chunk_size), end-1)
        images = {}
        for name, shape in self.info["image_shapes"].items():
            if self.video_format:
                from .video_storage import VideoReader
                if self.video_reader is None: self.video_reader = VideoReader()
                source_index = int(table["image_index/"+name][frame])
                pts = int(self.video_indices[episode,name]["pts_ns"][source_index])
                spec = self.info["episodes"][episode]["videos"][name]
                image = self.video_reader.frame(self.root/f"episodes/{episode:06d}/videos"/spec["path"], pts)
                image = self.preprocessor(name, image)
                if list(image.shape) != shape:
                    raise ValueError("Preprocessed video shape differs from dataset schema")
                images[name] = image
                continue
            image = cv2.imread(str(self.root/f"episodes/{episode:06d}/images/{name}/{frame:06d}.png"))
            if image is None or list(image.shape) != shape:
                raise ValueError(f"Missing/invalid PNG: episode={episode}, frame={frame}, camera={name}")
            images[name] = np.ascontiguousarray(image[:, :, ::-1])
        return {"state": table["state"][frame].copy(), "action": table["action"][chunk_indices].copy(),
                "padding": np.arange(frame, frame+self.chunk_size) >= end, "images": images,
                "timestamp": float(table["timestamp"][frame])}

    def __getstate__(self):
        state = self.__dict__.copy()
        state["video_reader"] = None
        return state

    def statistics(self):
        result = {}
        for key in ("state", "action"):
            values = np.concatenate([table[key][table.get("valid",np.ones(len(table[key]),dtype=bool))]
                                     for table in self.tables.values()])
            result[key+"_mean"] = values.mean(axis=0, dtype=np.float64).tolist()
            result[key+"_std"] = np.maximum(values.std(axis=0, dtype=np.float64), .01).tolist()
        return result


class VideoDatasetWriter:
    def __init__(self, root, name, fps, joints, preprocessor, synchronization):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.info = {"format": VIDEO_FORMAT, "name": name, "fps": fps, "units": "rad",
                     "joint_names": list(joints), "image_shapes": {n:list(preprocessor.shape(n)) for n in preprocessor.config["cameras"]},
                     "preprocessing": {k:v for k,v in preprocessor.config.items() if not k.startswith('_')},
                     "camera_calibration": preprocessor.calibration, "preprocessing_applied_on_read": True,
                     "synchronization": synchronization, "episodes": []}

    def episode_folder(self):
        return self.root/f"episodes/{len(self.info['episodes']):06d}"

    def save_episode(self, episode_id, task, rows, videos, report):
        folder = self.episode_folder()
        folder.mkdir(parents=True,exist_ok=True)
        if sum(row['valid'] for row in rows) < 2:
            raise ValueError("Episode contains fewer than two valid aligned frames")
        arrays = {"timestamp_ns": np.asarray([r['target_ns'] for r in rows], dtype=np.int64),
                  "valid": np.asarray([r['valid'] for r in rows],dtype=bool),
                  "warning": np.asarray([bool(r['warnings']) for r in rows],dtype=bool)}
        arrays['timestamp'] = arrays['timestamp_ns']/1e9
        with (folder/'alignment.jsonl').open('x') as log:
            for row in rows:
                sources = {"state":row['state'],"action":row['action'],**{"image/"+n:s for n,s in row['images'].items()},**{'input/'+n:s for n,s in row.get('inputs',{}).items()}}
                log.write(json.dumps({"episode_id":episode_id,"frame_index":row['index'],"target_timestamp_ns":row['target_ns'],
                    "action_query_timestamp_ns":row['action_query_ns'],"valid":row['valid'],"warnings":row['warnings'],"reasons":row['reasons'],
                    "sources":{n:None if s is None else {"timestamp_ns":s.timestamp_ns,"message_id":s.message_id,
                        "topic_name":s.topic,"frame_id":s.frame_id,"receive_timestamp_ns":s.receive_ns,
                        "capture_sequence":s.capture_sequence,
                        "time_error_s":row['errors'][n]} for n,s in sources.items()}})+'\n')
        d = len(self.info['joint_names'])
        for kind in ('state','action'):
            samples = [r[kind] for r in rows]
            arrays[kind] = np.asarray([s.value if s else [np.nan]*d for s in samples],dtype=np.float32)
            arrays[kind+'_time_ns'] = np.asarray([s.timestamp_ns if s else -1 for s in samples],dtype=np.int64)
            arrays[kind+'_message_id'] = np.asarray([s.message_id if s else -1 for s in samples],dtype=np.int64)
            arrays[kind+'_time_error'] = np.asarray([r['errors'][kind] if r['errors'][kind] is not None else np.nan for r in rows])
        for field in ('velocity','effort'):
            arrays['state_'+field] = np.asarray([getattr(r['state'],field) if r['state'] is not None and getattr(r['state'],field) is not None else [np.nan]*d for r in rows],dtype=np.float32)
            arrays['state_'+field+'_valid'] = np.isfinite(arrays['state_'+field])
        for name in sorted({n for row in rows for n in row.get('inputs',{})}):
            samples = [r.get('inputs',{}).get(name) for r in rows]
            arrays['input/'+name] = np.asarray([s.value if s else [np.nan]*d for s in samples],dtype=np.float32)
            arrays['input_time_ns/'+name] = np.asarray([s.timestamp_ns if s else -1 for s in samples],dtype=np.int64)
            arrays['input_message_id/'+name] = np.asarray([s.message_id if s else -1 for s in samples],dtype=np.int64)
            arrays['input_time_error/'+name] = np.asarray([r['errors'].get('input/'+name,np.nan) if s else np.nan for r,s in zip(rows,samples)])
            arrays['input_valid/'+name] = np.asarray([s is not None and abs(r['errors']['input/'+name]) <= self.info['synchronization']['reject_s']+1e-9 for r,s in zip(rows,samples)],dtype=bool)
        for name in self.info['image_shapes']:
            samples = [r['images'][name] for r in rows]
            arrays['image_index/'+name] = np.asarray([s.value if s else -1 for s in samples],dtype=np.int64)
            arrays['image_time_ns/'+name] = np.asarray([s.timestamp_ns if s else -1 for s in samples],dtype=np.int64)
            arrays['image_time_error/'+name] = np.asarray([r['errors']['image/'+name] if r['errors']['image/'+name] is not None else np.nan for r in rows])
            arrays['image_message_id/'+name] = np.asarray([s.message_id if s else -1 for s in samples],dtype=np.int64)
            arrays['image_capture_sequence/'+name] = np.asarray([s.capture_sequence if s and s.capture_sequence is not None else -1 for s in samples],dtype=np.int64)
        np.savez_compressed(folder/'frames.npz',**arrays)
        (folder/'sync_report.json').write_text(json.dumps(report,indent=2))
        self.info['episodes'].append({"index":len(self.info['episodes']),"episode_id":episode_id,"task":task,
                                     "frames":len(rows),"valid_frames":int(arrays['valid'].sum()),"videos":videos})

    def finalize(self):
        if not self.info['episodes']: raise ValueError("No episodes")
        (self.root/'dataset.json').write_text(json.dumps(self.info,indent=2))
