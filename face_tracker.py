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
    # NOTE: In MediaPipe's image-space output (mirror flipped from camera):
    # Index 468 cluster = subject's RIGHT eye (appears on LEFT of image)
    # Index 473 cluster = subject's LEFT eye  (appears on RIGHT of image)
    RIGHT_EYE_IRIS  = 468
    RIGHT_EYE_INNER = 133
    RIGHT_EYE_OUTER = 33
    RIGHT_EYE_TOP   = 159
    RIGHT_EYE_BOT   = 145

    LEFT_EYE_IRIS  = 473
    LEFT_EYE_INNER = 362
    LEFT_EYE_OUTER = 263
    LEFT_EYE_TOP   = 386
    LEFT_EYE_BOT   = 374

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

        # Single-eye blink tracking (for left-click / right-click)
        self.left_blink_start:  Optional[float] = None
        self.left_blink_fired:  bool = False
        self.right_blink_start: Optional[float] = None
        self.right_blink_fired: bool = False
        self.SINGLE_BLINK_DURATION: float = 0.18   # 180ms fast wink = click
        self.SINGLE_BLINK_MAX:    float = 0.90   # >900ms = probably just closing eye naturally
        self.SINGLE_EYE_DIFF:    float = 0.25   # other eye must be at least this much more open
        self.frame_timestamp_ms: int = 0

    def _estimate_head_pose(
        self, landmarks: List[Any], image_w: int, image_h: int, h_fov_deg: float = 65.0
    ) -> Tuple[float, float, float]:
        """Calculates pitch, yaw, roll using solvePnP with realistic intrinsic parameters."""
        image_points = []
        for idx in self.LANDMARK_INDICES_FOR_POSE:
            lm = landmarks[idx]
            image_points.append([lm.x * image_w, lm.y * image_h])
        image_points = np.array(image_points, dtype=np.float64)

        # Accurate focal length computation from sensor geometry:
        # fx = (w / 2) / tan(h_fov / 2)
        fov_rad = math.radians(h_fov_deg)
        fx = (image_w / 2.0) / math.tan(fov_rad / 2.0)
        fy = fx  # Square pixel assumption
        cx, cy = image_w / 2.0, image_h / 2.0

        camera_matrix = np.array([
            [fx,  0.0, cx],
            [0.0, fy,  cy],
            [0.0, 0.0, 1.0]
        ], dtype=np.float64)
        dist_coeffs = np.zeros((4, 1), dtype=np.float64)

        success, rvec, tvec = cv2.solvePnP(
            self.FACE_3D_MODEL,
            image_points,
            camera_matrix,
            dist_coeffs,
            flags=cv2.SOLVEPNP_EPNP  # EPNP is much more stable against webcam warping than ITERATIVE
        )
        if not success:
            return 0.0, 0.0, 0.0

        # Refine with iterative Levenberg-Marquardt
        try:
            rvec, tvec = cv2.solvePnPRefineLM(
                self.FACE_3D_MODEL,
                image_points,
                camera_matrix,
                dist_coeffs,
                rvec,
                tvec
            )
        except Exception:
            pass

        rmat, _ = cv2.Rodrigues(rvec)
        # Normalize rotation columns to handle any numerical drift
        r0 = rmat[:, 0] / max(np.linalg.norm(rmat[:, 0]), 1e-9)
        r1 = rmat[:, 1] / max(np.linalg.norm(rmat[:, 1]), 1e-9)
        r2 = rmat[:, 2] / max(np.linalg.norm(rmat[:, 2]), 1e-9)
        rn = np.column_stack([r0, r1, r2])
        pitch = float(math.degrees(math.atan2(-rn[1, 2], rn[2, 2])))  # nod up/down
        yaw   = float(math.degrees(math.atan2( rn[0, 2], rn[2, 2])))  # turn left/right
        roll  = float(math.degrees(math.atan2(-rn[0, 1], rn[0, 0])))  # tilt
        tx = float(tvec[0]); ty = float(tvec[1]); tz = float(tvec[2])
        return yaw, pitch, roll, tx, ty, tz

    def _calculate_iris_ratios(
        self, landmarks: List[Any], blink_l: float = 0.0, blink_r: float = 0.0
    ) -> Tuple[float, float]:
        """Roll-compensated iris gaze offset, averaged across both eyes."""

        def _eye_offset(iris, inner, outer, top, bot):
            """Project iris-to-eye-center onto the eye-corner axis and perpendicular."""
            # Eye corner axis (image-space, outer->inner direction)
            ax = inner.x - outer.x
            ay = inner.y - outer.y
            width = max(math.hypot(ax, ay), 1e-4)
            # Unit along-axis vector
            ux, uy = ax / width, ay / width
            # Perpendicular (rotate 90° CCW: (-y, x))
            vx, vy = -uy, ux

            # Eye center
            cx = (inner.x + outer.x) / 2.0
            cy = (top.y  + bot.y)  / 2.0

            dx = iris.x - cx
            dy = iris.y - cy

            # Project onto axis and perp, normalise by eye width
            horiz = (dx * ux + dy * uy) / width
            vert  = (dx * vx + dy * vy) / width
            return horiz, vert

        r_iris  = landmarks[self.RIGHT_EYE_IRIS]
        r_inner = landmarks[self.RIGHT_EYE_INNER]
        r_outer = landmarks[self.RIGHT_EYE_OUTER]
        r_top   = landmarks[self.RIGHT_EYE_TOP]
        r_bot   = landmarks[self.RIGHT_EYE_BOT]

        l_iris  = landmarks[self.LEFT_EYE_IRIS]
        l_inner = landmarks[self.LEFT_EYE_INNER]
        l_outer = landmarks[self.LEFT_EYE_OUTER]
        l_top   = landmarks[self.LEFT_EYE_TOP]
        l_bot   = landmarks[self.LEFT_EYE_BOT]

        r_rx, r_ry = _eye_offset(r_iris, r_inner, r_outer, r_top, r_bot)
        l_rx, l_ry = _eye_offset(l_iris, l_inner, l_outer, l_top, l_bot)

        # During a wink: hold last values (don't switch to other eye)
        if blink_l > 0.45 or blink_r > 0.45 or (blink_l > 0.50 and blink_r > 0.50):
            if hasattr(self, '_last_valid_rx'):
                return self._last_valid_rx, self._last_valid_ry
            # No cached value yet — fall through to compute

        target_rx = float((r_rx + l_rx) / 2.0)
        target_ry = float((r_ry + l_ry) / 2.0)

        # Centre around 0.5 and low-pass smooth
        avg_rx = float(0.5 + target_rx)
        avg_ry = float(0.5 + target_ry)
        if hasattr(self, '_last_valid_rx'):
            avg_rx = 0.70 * self._last_valid_rx + 0.30 * avg_rx
            avg_ry = 0.70 * self._last_valid_ry + 0.30 * avg_ry
        self._last_valid_rx = avg_rx
        self._last_valid_ry = avg_ry
        return avg_rx, avg_ry

    def process_frame(
        self, frame_bgr: np.ndarray, timestamp_ms: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Processes a single video frame.
        Returns:
            Dictionary containing features, blendshapes, pose, and click events.
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

        # 1. Extract blendshapes first so iris calculation can adjust for blinks
        blendshape_dict = {}
        if result.face_blendshapes and len(result.face_blendshapes) > 0:
            for category in result.face_blendshapes[0]:
                blendshape_dict[category.category_name] = category.score

        blink_l = blendshape_dict.get("eyeBlinkLeft", 0.0)
        blink_r = blendshape_dict.get("eyeBlinkRight", 0.0)
        lookdown_l = blendshape_dict.get("eyeLookDownLeft", 0.0)
        lookdown_r = blendshape_dict.get("eyeLookDownRight", 0.0)

        # 2. Head Pose (Euler angles via calibrated EPNP + LM) + translation
        yaw, pitch, roll, tx, ty, tz = self._estimate_head_pose(landmarks, image_w, image_h)

        # 3. Iris to canthus ratios with blink/wink compensation
        iris_x, iris_y = self._calculate_iris_ratios(landmarks, blink_l, blink_r)

        # 4. Dynamic blink threshold (scales upward when lookDown is high)
        max_lookdown = max(lookdown_l, lookdown_r)
        dynamic_blink_threshold = min(
            0.88, self.base_blink_threshold + self.lookdown_scaling * max_lookdown
        )

        both_eyes_blinking = (blink_l >= dynamic_blink_threshold) and (
            blink_r >= dynamic_blink_threshold
        )
        # Intentional wink requires high closure on one eye and clear asymmetry
        is_left_wink  = (blink_l >= 0.62) and ((blink_l - blink_r) >= 0.28) and not both_eyes_blinking
        is_right_wink = (blink_r >= 0.62) and ((blink_r - blink_l) >= 0.28) and not both_eyes_blinking

        # 5. Extract Full-Range Eye Gaze Direction
        look_out_l = blendshape_dict.get("eyeLookOutLeft", 0.0)
        look_in_l = blendshape_dict.get("eyeLookInLeft", 0.0)
        look_out_r = blendshape_dict.get("eyeLookOutRight", 0.0)
        look_in_r = blendshape_dict.get("eyeLookInRight", 0.0)
        look_up_l = blendshape_dict.get("eyeLookUpLeft", 0.0)
        look_up_r = blendshape_dict.get("eyeLookUpRight", 0.0)
        look_down_l = blendshape_dict.get("eyeLookDownLeft", 0.0)
        look_down_r = blendshape_dict.get("eyeLookDownRight", 0.0)

        # Horizontal gaze (-1.0 to +1.0): positive = looking left, negative = looking right
        eye_look_left = (look_out_l + look_in_r) / 2.0
        eye_look_right = (look_in_l + look_out_r) / 2.0
        eye_gaze_h = float(eye_look_left - eye_look_right)

        # Vertical gaze (-1.0 to +1.0): positive = looking up, negative = looking down
        eye_look_up = (look_up_l + look_up_r) / 2.0
        eye_look_down = (look_down_l + look_down_r) / 2.0
        eye_gaze_v = float(eye_look_up - eye_look_down)

        # Physical landmark iris deviation from center (0.50)
        iris_dev_x = float((0.50 - iris_x) / 0.07)
        iris_dev_y = float((0.50 - iris_y) / 0.07)

        # Fuse neural gaze blendshape + physical iris displacement
        if is_left_wink or is_right_wink or both_eyes_blinking:
            # Freeze eye gaze vector during winks or blinks
            if hasattr(self, '_last_eye_x'):
                eye_x, eye_y = self._last_eye_x, self._last_eye_y
            else:
                eye_x, eye_y = 0.0, 0.0
        else:
            raw_eye_x = 0.65 * eye_gaze_h + 0.35 * max(-1.0, min(1.0, iris_dev_x))
            raw_eye_y = 0.65 * eye_gaze_v + 0.35 * max(-1.0, min(1.0, iris_dev_y))

            if hasattr(self, '_last_eye_x'):
                eye_x = 0.70 * self._last_eye_x + 0.30 * raw_eye_x
                eye_y = 0.70 * self._last_eye_y + 0.30 * raw_eye_y
            else:
                eye_x, eye_y = raw_eye_x, raw_eye_y

            self._last_eye_x = eye_x
            self._last_eye_y = eye_y

        now = time.time()
        blink_click = False
        click_type  = "left"   # default

        # ── Extended eye closure (>1.8s) resting mode ──
        if both_eyes_blinking:
            if self.blink_start_time is None:
                self.blink_start_time = now
            else:
                elapsed = now - self.blink_start_time
                if elapsed >= 1.8 and not self.is_blink_locked:
                    self.is_blink_locked = True
                    print("[FaceTracker] Extended eye closure (>1.8s) - resting cursor.", flush=True)
        else:
            if self.is_blink_locked and self.blink_start_time is not None:
                self.is_blink_locked = False
            self.blink_start_time = None

        # ── Left-eye wink → left click ──
        if is_left_wink:
            if self.left_blink_start is None:
                self.left_blink_start = now
                self.left_blink_fired = False
            else:
                elapsed = now - self.left_blink_start
                if 0.18 <= elapsed <= 0.85 and not self.left_blink_fired:
                    blink_click = True
                    click_type  = "left"
                    self.left_blink_fired = True
                    print(f"[FaceTracker] Left wink → LEFT click! ({elapsed:.2f}s)", flush=True)
        else:
            self.left_blink_start = None
            self.left_blink_fired = False

        # ── Right-eye wink → right click ──
        if is_right_wink:
            if self.right_blink_start is None:
                self.right_blink_start = now
                self.right_blink_fired = False
            else:
                elapsed = now - self.right_blink_start
                if 0.18 <= elapsed <= 0.85 and not self.right_blink_fired:
                    blink_click = True
                    click_type  = "right"
                    self.right_blink_fired = True
                    print(f"[FaceTracker] Right wink → RIGHT click! ({elapsed:.2f}s)", flush=True)
        else:
            self.right_blink_start = None
            self.right_blink_fired = False

        # 6. Extreme pitch abort (head tilted sharply up toward ceiling)
        abort_triggered = pitch >= self.extreme_pitch_abort_deg

        return {
            "features": np.array([yaw, pitch, iris_x, iris_y, tx, ty, tz], dtype=np.float64),
            "yaw": yaw,
            "pitch": pitch,
            "roll": roll,
            "tx": tx,
            "ty": ty,
            "tz": tz,
            "iris_x": iris_x,
            "iris_y": iris_y,
            "eye_x": eye_x,
            "eye_y": eye_y,
            "blink_l": blink_l,
            "blink_r": blink_r,
            "lookdown_l": lookdown_l,
            "lookdown_r": lookdown_r,
            "dynamic_blink_thresh": dynamic_blink_threshold,
            "is_blink_locked": self.is_blink_locked,
            "abort_triggered": abort_triggered,
            "blink_click": blink_click,
            "click_type":  click_type,
            "is_eyes_closed": bool(both_eyes_blinking),
            "landmarks": landmarks,
            "is_winking": bool(is_left_wink or is_right_wink),
        }

    def close(self):
        self.landmarker.close()
