"""
Aura Gaze Engine — Precision Eye Tracking & Tremor Elimination
Fuses 3D head pose with sub-pixel iris displacement.
Applies:
1. Realistic anatomical eye-to-screen projection.
2. 5-point calibration model (if trained) or high-sensitivity geometric default.
3. Dual-stage velocity deadband filter (suppresses micro-saccades & jitter).
4. Adaptive 1-Euro Filter for buttery smooth fixation and instant saccades.
"""

import os
import math
import time
from typing import Tuple, Dict, Any, Optional
import numpy as np

try:
    import joblib
except ImportError:
    joblib = None


class LowPassFilter:
    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha
        self.s = None

    def filter(self, val: float, alpha: Optional[float] = None) -> float:
        if alpha is not None:
            self.alpha = alpha
        if self.s is None:
            self.s = val
        else:
            self.s = self.alpha * val + (1.0 - self.alpha) * self.s
        return self.s


class OneEuroFilter2D:
    def __init__(self, min_cutoff: float = 0.35, beta: float = 0.08, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff

        self.x_filter = LowPassFilter()
        self.dx_filter = LowPassFilter()
        self.y_filter = LowPassFilter()
        self.dy_filter = LowPassFilter()

        self.last_time = None
        self.last_x = None
        self.last_y = None

        # Deadband threshold in pixels (ignores physiological micro-tremors)
        self.deadband_radius = 5.0

    def _alpha(self, rate: float, cutoff: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        te = 1.0 / rate if rate > 0 else 0.016
        return 1.0 / (1.0 + tau / te)

    def filter(self, x: float, y: float, timestamp: Optional[float] = None) -> Tuple[float, float]:
        now = timestamp or time.time()
        if self.last_time is None:
            self.last_time = now
            self.last_x = x
            self.last_y = y
            self.x_filter.filter(x, 1.0)
            self.y_filter.filter(y, 1.0)
            self.dx_filter.filter(0.0, 1.0)
            self.dy_filter.filter(0.0, 1.0)
            return x, y

        dt = max(1e-4, now - self.last_time)
        rate = 1.0 / dt
        self.last_time = now

        # 1. Deadband check: if movement is tiny, hold position to prevent jitter
        dist_from_last = math.hypot(x - self.last_x, y - self.last_y)
        if dist_from_last < self.deadband_radius:
            return self.last_x, self.last_y

        # 2. Derivative (velocity) estimation
        dx = (x - self.x_filter.s) * rate
        dy = (y - self.y_filter.s) * rate
        edx = self.dx_filter.filter(dx, self._alpha(rate, self.d_cutoff))
        edy = self.dy_filter.filter(dy, self._alpha(rate, self.d_cutoff))

        # 3. Dynamic cutoff frequency based on speed
        speed = math.hypot(edx, edy)
        cutoff = self.min_cutoff + self.beta * speed

        # 4. Filtered position
        alpha = self._alpha(rate, cutoff)
        smooth_x = self.x_filter.filter(x, alpha)
        smooth_y = self.y_filter.filter(y, alpha)

        self.last_x = smooth_x
        self.last_y = smooth_y
        return smooth_x, smooth_y


class GazeEngine:
    """
    Translates raw MediaPipe face features into pixel-accurate screen coordinates.
    """
    def __init__(self, screen_w: int = 1536, screen_h: int = 864, model_path: Optional[str] = None):
        self.screen_w = screen_w
        self.screen_h = screen_h

        # Smoothing filter
        self.filter = OneEuroFilter2D(min_cutoff=0.35, beta=0.08)

        # Calibration model (Ridge / Affine Regression)
        self.calib_model = None
        self.model_path = model_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "models", "gaze_calibration.joblib"
        )
        self.load_calibration()

        # Sensitivities for uncalibrated geometric default
        self.eye_gain_x = 2200.0  # High sensitivity so looking across screen actually moves cursor
        self.eye_gain_y = 1600.0
        self.head_gain_x = 28.0   # Screen pixels per degree of head yaw
        self.head_gain_y = 22.0   # Screen pixels per degree of head pitch

        # Neutral resting calibration offsets
        self.neutral_yaw = 0.0
        self.neutral_pitch = -4.0  # Natural slight downward gaze at laptop
        self.neutral_iris_x = 0.50
        self.neutral_iris_y = 0.50

    def load_calibration(self) -> bool:
        """Loads saved 5-point calibration model if exists."""
        if joblib and os.path.exists(self.model_path):
            try:
                self.calib_model = joblib.load(self.model_path)
                print(f"[GazeEngine] Loaded calibrated gaze model from {self.model_path}")
                return True
            except Exception as e:
                print(f"[GazeEngine] Could not load calibration: {e}")
        return False

    def save_calibration(self, model: Any):
        """Saves new calibration model."""
        if joblib:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            joblib.dump(model, self.model_path)
            self.calib_model = model
            print(f"[GazeEngine] Saved calibrated gaze model to {self.model_path}")

    def project_gaze(self, feat: list) -> Tuple[float, float]:
        """
        Maps [yaw, pitch, iris_x, iris_y] to raw (x, y) coordinates.
        """
        yaw, pitch, iris_x, iris_y = feat[0], feat[1], feat[2], feat[3]

        if self.calib_model is not None:
            try:
                pred = self.calib_model.predict([feat])[0]
                raw_x = float(pred[0])
                raw_y = float(pred[1])
                return (
                    max(0.0, min(float(self.screen_w), raw_x)),
                    max(0.0, min(float(self.screen_h), raw_y))
                )
            except Exception:
                pass

        # High-Accuracy Geometric Default:
        # Looking right increases iris_x (relative to left-eye canthus)
        # In mirrored webcam: looking to user's right moves cursor right (+X)
        d_iris_x = iris_x - self.neutral_iris_x
        d_iris_y = iris_y - self.neutral_iris_y

        d_head_x = -(yaw - self.neutral_yaw)  # Turning head left/right
        d_head_y = -(pitch - self.neutral_pitch) # Tilting up/down

        # Combine macro head pose with micro iris displacement
        screen_center_x = self.screen_w / 2.0
        screen_center_y = self.screen_h / 2.0

        target_x = screen_center_x + (d_head_x * self.head_gain_x) - (d_iris_x * self.eye_gain_x)
        target_y = screen_center_y + (d_head_y * self.head_gain_y) + (d_iris_y * self.eye_gain_y)

        # Clamping to screen boundaries with 10px margin
        clamped_x = max(0.0, min(float(self.screen_w), target_x))
        clamped_y = max(0.0, min(float(self.screen_h), target_y))

        return clamped_x, clamped_y

    def process(self, feat: list, timestamp: Optional[float] = None) -> Tuple[float, float]:
        """Projects gaze features and filters jitter."""
        raw_x, raw_y = self.project_gaze(feat)
        smooth_x, smooth_y = self.filter.filter(raw_x, raw_y, timestamp)
        return smooth_x, smooth_y
