import cv2
import numpy as np
from .config import resolve
import yaml


class ImagePreprocessor:
    """Same uint8 RGB geometry for dataset generation and deployment.

    Scaling to [0,1] and dataset-statistics normalization belong to the saved
    project ACT checkpoint; the dataset stores lossless uint8 RGB images.
    """
    def __init__(self, config):
        self.config = config
        self.calibration = {}
        for name in config["cameras"]:
            spec = config["preprocessing"][name]
            if spec.get("undistort", {}).get("enabled", False):
                with resolve(config, spec["undistort"]["calibration"]).open() as stream:
                    self.calibration[name] = yaml.safe_load(stream)

    def shape(self, name):
        spec = self.config["preprocessing"][name]
        h, w = spec["resize"]["height"], spec["resize"]["width"]
        pad = spec.get("pad", {})
        return h+pad.get("top", 0)+pad.get("bottom", 0), w+pad.get("left", 0)+pad.get("right", 0), 3

    def __call__(self, name, rgb):
        if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError("Expected uint8 HWC RGB")
        spec = self.config["preprocessing"][name]
        if name in self.calibration:
            cal = self.calibration[name]
            if [rgb.shape[1], rgb.shape[0]] != [cal["image_width"], cal["image_height"]]:
                raise ValueError("Camera calibration resolution mismatch")
            rgb = cv2.undistort(rgb, np.array(cal["camera_matrix"]["data"]).reshape(3, 3),
                                np.array(cal["distortion_coefficients"]["data"]))
        crop = spec.get("crop", {})
        if crop.get("enabled", False):
            x, y, w, h = (crop[k] for k in ("x", "y", "width", "height"))
            if min(x, y) < 0 or min(w, h) <= 0 or x+w > rgb.shape[1] or y+h > rgb.shape[0]:
                raise ValueError("Crop outside image bounds")
            rgb = rgb[y:y+h, x:x+w]
        resize = spec["resize"]
        interpolation = {"bilinear": cv2.INTER_LINEAR, "nearest": cv2.INTER_NEAREST,
                         "area": cv2.INTER_AREA}[resize.get("interpolation", "bilinear")]
        rgb = cv2.resize(rgb, (resize["width"], resize["height"]), interpolation=interpolation)
        pad = spec.get("pad", {})
        rgb = cv2.copyMakeBorder(rgb, pad.get("top", 0), pad.get("bottom", 0),
                                 pad.get("left", 0), pad.get("right", 0), cv2.BORDER_CONSTANT,
                                 value=pad.get("value", [0, 0, 0]))
        return np.ascontiguousarray(rgb)


def image_from_ros(msg):
    if msg.encoding not in ("rgb8", "bgr8") or msg.step < msg.width*3:
        raise ValueError(f"Unsupported image encoding/stride: {msg.encoding}")
    raw = np.frombuffer(bytes(msg.data), dtype=np.uint8).reshape(msg.height, msg.step)
    rgb = raw[:, :msg.width*3].reshape(msg.height, msg.width, 3)
    return np.ascontiguousarray(rgb if msg.encoding == "rgb8" else rgb[:, :, ::-1])
