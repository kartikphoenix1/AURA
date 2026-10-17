import os
import time
import math
import urllib.request
from typing import Dict, List, Optional, Tuple, Any

import cv2
import numpy as np

# MediaPipe Tasks Vision API imports
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL_PATH = os.path.join(_BASE_DIR, "models", "face_landmarker.task")
# Fallback: accept the model if it was placed directly in the project root
_ROOT_MODEL = os.path.join(_BASE_DIR, "face_landmarker.task")
if os.path.exists(_ROOT_MODEL) and not os.path.exists(DEFAULT_MODEL_PATH):
    import shutil
    os.makedirs(os.path.dirname(DEFAULT_MODEL_PATH), exist_ok=True)
    shutil.copy2(_ROOT_MODEL, DEFAULT_MODEL_PATH)


def ensure_model_downloaded(model_path: str = DEFAULT_MODEL_PATH) -> str:
    """Download the official MediaPipe FaceLandmarker model bundle if missing."""
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    if not os.path.exists(model_path) or os.path.getsize(model_path) < 1000:
        print(f"[FaceTracker] Downloading face_landmarker.task to {model_path}...")
        urllib.request.urlretrieve(MODEL_URL, model_path)
        print(f"[FaceTracker] Model downloaded successfully ({os.path.getsize(model_path)} bytes).")
    return model_path


class FaceGazeTracker:
    """
    Precision Face, Head Pose, Iris, and Blendshape tracker
    using MediaPipe Tasks Vision FaceLandmarker.
    """

    # 3D Reference points from anthropometric face model (in mm, OpenCV image coordinates: +Y down, +X right)
    FACE_3D_MODEL = np.array([
        [0.0, 0.0, 0.0],          # Nose tip (#1)
        [0.0, 330.0, -65.0],       # Chin (#152)
        [-225.0, -170.0, -135.0],  # Left eye outer corner (#33)
        [225.0, -170.0, -135.0],   # Right eye outer corner (#263)
        [-150.0, 150.0, -125.0],   # Left mouth corner (#61)
        [150.0, 150.0, -125.0]     # Right mouth corner (#291)
    ], dtype=np.float64)

    # Corresponding MediaPipe landmark indices
    LANDMARK_INDICES_FOR_POSE = [1, 152, 33, 263, 61, 291]

    # Iris and eye boundary landmark indices
    LEFT_IRIS = 468
    LEFT_INNER = 133
    LEFT_OUTER = 33
    LEFT_TOP = 159
    LEFT_BOTTOM = 145

    RIGHT_IRIS = 473
    RIGHT_INNER = 362
    RIGHT_OUTER = 263
    RIGHT_TOP = 386
    RIGHT_BOTTOM = 374

    def __init__(
        self,
        model_path: Optional[str] = None,
        base_blink_threshold: float = 0.50,
        lookdown_scaling: float = 0.35,
        long_blink_duration: float = 1.3,
        extreme_pitch_abort_deg: float = 25.0,
    ):
        actual_model_path = ensure_model_downloaded(model_path or DEFAULT_MODEL_PATH)

        base_options = mp_python.BaseOptions(model_asset_path=actual_model_path)
        options = mp_vision.FaceLandmarkerOptions(
            base_options=base_options,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        self.landmarker = mp_vision.FaceLandmarker.create_from_options(options)

        self.base_blink_threshold = base_blink_threshold
        self.lookdown_scaling = lookdown_scaling
        self.long_blink_duration = long_blink_duration
        self.extreme_pitch_abort_deg = extreme_pitch_abort_deg

        # State tracking
        self.blink_start_time: Optional[float] = None
        self.blink_click_duration: float = 0.40  # 400ms deliberate blink triggers a click
        self.blink_click_dispatched: bool = False
        self.is_blink_locked: bool = False
        self.frame_timestamp_ms: int = 0

    def _estimate_head_pose(
        self, landmarks: List[Any], image_w: int, image_h: int
    ) -> Tuple[float, float, float]:
        """
        Calculates pitch, yaw, roll using solvePnP against standardized 3D points.
        Returns: (yaw_deg, pitch_deg, roll_deg)
        """
        image_points = []
        for idx in self.LANDMARK_INDICES_FOR_POSE:
            lm = landmarks[idx]
            image_points.append([lm.x * image_w, lm.y * image_h])
        image_points = np.array(image_points, dtype=np.float64)

        # Camera intrinsic matrix approximation
        focal_length = image_w
        cam_center = (image_w / 2.0, image_h / 2.0)
        camera_matrix = np.array([
            [focal_length, 0.0, cam_center[0]],
            [0.0, focal_length, cam_center[1]],
            [0.0, 0.0, 1.0]
        ], dtype=np.float64)
        dist_coeffs = np.zeros((4, 1), dtype=np.float64)

        success, rvec, tvec = cv2.solvePnP(
            self.FACE_3D_MODEL,
            image_points,
            camera_matrix,
            dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE
        )
        if not success:
            return 0.0, 0.0, 0.0

        rmat, _ = cv2.Rodrigues(rvec)
        # Extract Euler angles in degrees via standard RQ decomposition
        angles, _, _, _, _, _ = cv2.RQDecomp3x3(rmat)
        pitch = float(angles[0])
        yaw = float(angles[1])
        roll = float(angles[2])
        return yaw, pitch, roll

    def _calculate_iris_ratios(self, landmarks: List[Any]) -> Tuple[float, float]:
        """
        Calculates normalized horizontal and vertical iris position ratios.
        Ratios are dimensionless and invariant to distance from camera.
        """
        # Left eye
        l_iris = landmarks[self.LEFT_IRIS]
        l_inner = landmarks[self.LEFT_INNER]
        l_outer = landmarks[self.LEFT_OUTER]
        l_top = landmarks[self.LEFT_TOP]
        l_bottom = landmarks[self.LEFT_BOTTOM]

        l_w = max(abs(l_inner.x - l_outer.x), 1e-5)
        l_h = max(abs(l_bottom.y - l_top.y), 1e-5)
        l_rx = (l_iris.x - min(l_inner.x, l_outer.x)) / l_w
        l_ry = (l_iris.y - min(l_top.y, l_bottom.y)) / l_h

        # Right eye
        r_iris = landmarks[self.RIGHT_IRIS]
        r_inner = landmarks[self.RIGHT_INNER]
        r_outer = landmarks[self.RIGHT_OUTER]
        r_top = landmarks[self.RIGHT_TOP]
        r_bottom = landmarks[self.RIGHT_BOTTOM]

        r_w = max(abs(r_inner.x - r_outer.x), 1e-5)
        r_h = max(abs(r_bottom.y - r_top.y), 1e-5)
        r_rx = (r_iris.x - min(r_inner.x, r_outer.x)) / r_w
        r_ry = (r_iris.y - min(r_top.y, r_bottom.y)) / r_h

        avg_rx = float((l_rx + r_rx) / 2.0)
        avg_ry = float((l_ry + r_ry) / 2.0)
        return avg_rx, avg_ry

    def process_frame(
        self, frame_bgr: np.ndarray, timestamp_ms: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Processes a single video frame.
        Returns:
            Dictionary containing:
            - features: [yaw, pitch, iris_x, iris_y]
            - blendshapes: {blink_l, blink_r, lookdown_l, lookdown_r, ...}
            - blink_locked: bool
            - abort_triggered: bool (extreme pitch)
            - raw_pose: (yaw, pitch, roll)
        """
        image_h, image_w = frame_bgr.shape[:2]
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)

        if timestamp_ms is None:
            self.frame_timestamp_ms += 33
            timestamp_ms = self.frame_timestamp_ms

        result = self.landmarker.detect_for_video(mp_image, timestamp_ms)

        if not result.face_landmarks or len(result.face_landmarks) == 0:
            return None

        landmarks = result.face_landmarks[0]

        # 1. Head Pose (Euler angles)
        yaw, pitch, roll = self._estimate_head_pose(landmarks, image_w, image_h)

        # 2. Iris to canthus ratios
        iris_x, iris_y = self._calculate_iris_ratios(landmarks)

        # 3. Extract key blendshapes
        blendshape_dict = {}
        if result.face_blendshapes and len(result.face_blendshapes) > 0:
            for category in result.face_blendshapes[0]:
                blendshape_dict[category.category_name] = category.score

        blink_l = blendshape_dict.get("eyeBlinkLeft", 0.0)
        blink_r = blendshape_dict.get("eyeBlinkRight", 0.0)
        lookdown_l = blendshape_dict.get("eyeLookDownLeft", 0.0)
        lookdown_r = blendshape_dict.get("eyeLookDownRight", 0.0)

        # 4. Dynamic blink threshold (scales upward when lookDown is high)
        max_lookdown = max(lookdown_l, lookdown_r)
        dynamic_blink_threshold = min(
            0.88, self.base_blink_threshold + self.lookdown_scaling * max_lookdown
        )

        both_eyes_blinking = (blink_l >= dynamic_blink_threshold) and (
            blink_r >= dynamic_blink_threshold
        )

        now = time.time()
        blink_click = False

        if both_eyes_blinking:
            if self.blink_start_time is None:
                self.blink_start_time = now
                self.blink_click_dispatched = False
            else:
                elapsed = now - self.blink_start_time
                # Intentional long blink (0.40s) triggers a click!
                if elapsed >= self.blink_click_duration and not self.blink_click_dispatched and elapsed < 1.6:
                    blink_click = True
                    self.blink_click_dispatched = True
                    print(f"[FaceTracker] Long-blink click triggered! (held for {elapsed:.2f}s)", flush=True)
                elif elapsed >= 1.8 and not self.is_blink_locked:
                    self.is_blink_locked = True
                    print("[FaceTracker] Extended eye closure (>1.8s) - resting cursor.", flush=True)
        else:
            if self.is_blink_locked and self.blink_start_time is not None:
                self.is_blink_locked = False
            self.blink_start_time = None
            self.blink_click_dispatched = False

        # 5. Extreme pitch abort (head tilted sharply up toward ceiling)
        abort_triggered = pitch >= self.extreme_pitch_abort_deg

        return {
            "features": np.array([yaw, pitch, iris_x, iris_y], dtype=np.float64),
            "yaw": yaw,
            "pitch": pitch,
            "roll": roll,
            "iris_x": iris_x,
            "iris_y": iris_y,
            "blink_l": blink_l,
            "blink_r": blink_r,
            "lookdown_l": lookdown_l,
            "lookdown_r": lookdown_r,
            "dynamic_blink_thresh": dynamic_blink_threshold,
            "is_blink_locked": self.is_blink_locked,
            "abort_triggered": abort_triggered,
            "blink_click": blink_click,
            "is_eyes_closed": bool(both_eyes_blinking),
        }

    def close(self):
        self.landmarker.close()
