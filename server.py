import asyncio
import base64
import json
import os
import sys
import time
import threading
from collections import deque
from typing import Dict, List, Optional, Any

# macOS AVFoundation Camera Permission verification
if sys.platform == "darwin":
    try:
        import AVFoundation
        auth_status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeVideo)
        if auth_status == 0:  # Not Determined
            print("[AuraServer] Requesting macOS Camera Permission from system...")
            import threading
            auth_event = threading.Event()
            def auth_callback(granted):
                print(f"[AuraServer] macOS Camera Access granted: {granted}")
                auth_event.set()
            AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVFoundation.AVMediaTypeVideo, auth_callback
            )
            auth_event.wait(timeout=3.0)
        elif auth_status == 3:
            print("[AuraServer] macOS Camera Access: Authorized.")
        else:
            print(f"[AuraServer] macOS Camera Access status: {auth_status} (Denied or Restricted). Check System Settings.")
    except Exception as e:
        print(f"[AuraServer] AVFoundation check: {e}")

import cv2
import numpy as np
import sounddevice as sd
import websockets

from face_tracker import FaceGazeTracker
from calibration import GazeCalibration
from one_euro_filter import DeadbandOneEuroFilter2D
from classifier import AcousticClassifier

PORT = 8765
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_PATH = os.path.join(_BASE_DIR, "models", "gaze_calibration.joblib")
AUDIO_MODEL_PATH = os.path.join(_BASE_DIR, "models", "acoustic_model.joblib")


class AuraEngineServer:
    def __init__(self, camera_index: int = 0, screen_w: int = 1920, screen_h: int = 1080):
        self.camera_index = camera_index

        # Dynamically query OS primary display dimensions
        self.screen_w = screen_w
        self.screen_h = screen_h
        if sys.platform == "darwin":
            try:
                import Quartz
                bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
                self.screen_w = int(bounds.size.width)
                self.screen_h = int(bounds.size.height)
                print(f"[AuraServer] Detected macOS display: {self.screen_w}x{self.screen_h}")
            except Exception:
                pass
        elif sys.platform == "win32":
            try:
                import ctypes
                self.screen_w = int(ctypes.windll.user32.GetSystemMetrics(0))
                self.screen_h = int(ctypes.windll.user32.GetSystemMetrics(1))
                print(f"[AuraServer] Detected Windows display: {self.screen_w}x{self.screen_h}")
            except Exception:
                pass

        # Core Vision & Filter Subsystems (18px deadband, stable cursor response)
        self.tracker = FaceGazeTracker()
        self.calibration = GazeCalibration(screen_w=self.screen_w, screen_h=self.screen_h)
        self.filter = DeadbandOneEuroFilter2D(deadband_radius=32.0, min_cutoff=0.45, beta=0.0001)
        self.gaze_history = deque(maxlen=9)  # Suppress short gaze jitter and isolated landmark jumps.

        # Adaptive Neutral Resting Pose (centers cursor on user's relaxed forward gaze)
        self.neutral_yaw: float = 0.0
        self.neutral_pitch: float = -6.0   # Natural downward tilt toward laptop screen center
        self.neutral_iris_x: float = 0.50
        self.neutral_iris_y: float = 0.50
        self.neutral_samples = deque(maxlen=30)

        # Audio Subsystem
        # Native macOS mic rate is 48kHz; we downsample to 16kHz for the classifier
        self._audio_native_rate = 48000 if sys.platform == "darwin" else 16000
        self._audio_target_rate = 16000
        self._audio_downsample = self._audio_native_rate // self._audio_target_rate  # 3 on macOS
        self.audio_classifier = AcousticClassifier(sample_rate=16000)

        # Try loading existing models if present (validate quality before activating)
        if os.path.exists(CALIBRATION_PATH):
            try:
                self.calibration.load(CALIBRATION_PATH)
                diag = self.calibration.diagnostics or {}
                mean_err = diag.get("overall_mean_px", 9999.0)
                best_alpha = diag.get("best_alpha", 9999.0)
                if mean_err > 200.0 or best_alpha >= 1000.0:
                    print(f"[AuraServer] Discarding low-quality calibration model (mean_err={mean_err:.1f}px, alpha={best_alpha}). Using adaptive hybrid tracker.")
                    self.calibration.model_x = None
                    self.calibration.model_y = None
                else:
                    print(f"[AuraServer] Loaded verified gaze calibration from {CALIBRATION_PATH} (mean_err={mean_err:.1f}px)")
            except Exception as e:
                print(f"[AuraServer] Could not load gaze calibration: {e}")

        if os.path.exists(AUDIO_MODEL_PATH):
            try:
                self.audio_classifier.load(AUDIO_MODEL_PATH)
                print(f"[AuraServer] Loaded acoustic model from {AUDIO_MODEL_PATH}")
            except Exception as e:
                print(f"[AuraServer] Could not load acoustic model: {e}")

        # Shared state for thread safety
        self.running = False
        self.voice_listening = False
        self.async_loop = None
        self.blink_click_until: float = 0.0
        self.blink_click_type: str = "left"
        self.audio_click_active: bool = False
        self._audio_gate_started: Optional[float] = None
        self.gaze_established_frames: int = 0
        self.GAZE_ESTABLISH_FRAMES: int = 30
        self.last_valid_smooth_x: float = self.screen_w / 2.0
        self.last_valid_smooth_y: float = self.screen_h / 2.0

        self.latest_telemetry: Dict[str, Any] = {
            "type": "telemetry",
            "timestamp": time.time(),
            "x": self.screen_w / 2.0,
            "y": self.screen_h / 2.0,
            "screen_w": self.screen_w,
            "screen_h": self.screen_h,
            "face_detected": False,
            "camera_connected": False,
            "camera_brightness": 0.0,
            "click": False,
            "click_confidence": 0.0,
            "click_source": "none",
            "is_blink_locked": False,
            "abort_triggered": False,
            "raw_pose": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
            "audio": {"rms": 0.0, "noise_floor": 0.005, "current_gate": 0.015, "is_above_gate": False},
        }

        # Audio rolling circular buffer (last 2 seconds at effective 16kHz)
        self.audio_buffer_size = 16000 * 2
        self.audio_buffer = np.zeros(self.audio_buffer_size, dtype=np.float32)
        self.audio_lock = threading.Lock()

        # Calibration recording buffers
        self.is_recording_target: bool = False
        self.recording_target_id: Optional[int] = None
        self.camera_enabled: bool = True
        self.recorded_target_frames: List[np.ndarray] = []

        self.recorded_click_audios: List[np.ndarray] = []
        self.recorded_noise_audios: List[np.ndarray] = []

        # Connected WebSocket clients
        self.clients = set()

    def _audio_callback(self, indata, frames, time_info, status):
        """Sounddevice input stream callback running at ~30-50 fps."""
        raw_chunk = indata[:, 0].copy()
        # Downsample from native rate (48kHz) to 16kHz by decimation
        step = self._audio_downsample
        audio_chunk = raw_chunk[::step] if step > 1 else raw_chunk
        rms = float(np.sqrt(np.mean(audio_chunk**2)))

        with self.audio_lock:
            # Roll buffer and insert new downsampled chunk
            self.audio_buffer = np.roll(self.audio_buffer, -len(audio_chunk))
            self.audio_buffer[-len(audio_chunk):] = audio_chunk

        # Reuse AURA's existing microphone stream for voice recognition, and only
        # forward PCM to the local assistant while the user has activated Flipping.
        if self.voice_listening and self.async_loop is not None:
            try:
                pcm = np.clip(audio_chunk * 32767.0, -32768, 32767).astype("<i2").tobytes()
                encoded = base64.b64encode(pcm).decode("ascii")
                asyncio.run_coroutine_threadsafe(self._broadcast_voice_audio(encoded), self.async_loop)
            except RuntimeError:
                pass

        is_above_gate = self.audio_classifier.update_noise_floor(rms)

        is_click = False
        confidence = 0.0
        now = time.monotonic()
        if is_above_gate:
            if self._audio_gate_started is None:
                self._audio_gate_started = now
        else:
            self._audio_gate_started = None

        if is_above_gate and self.audio_classifier.is_calibrated:
            # Candidate burst is last 200ms at effective 16kHz.
            candidate = self.audio_buffer[-3200:]
            is_click, confidence, _ = self.audio_classifier.classify_audio(candidate)

        # Require a high-confidence match and a sustained intent sound to avoid
        # triggering on brief environmental transients.
        sustained = self._audio_gate_started is not None and now - self._audio_gate_started >= 0.15
        self.audio_click_active = bool(is_click and confidence >= 0.85 and sustained)
        self.latest_telemetry["click_confidence"] = float(confidence)
        self.latest_telemetry["audio"] = {
            "rms": rms,
            "noise_floor": float(self.audio_classifier.noise_floor),
            "current_gate": float(self.audio_classifier.current_gate),
            "is_above_gate": is_above_gate,
        }

    async def _broadcast_voice_audio(self, pcm_base64: str):
        """Send microphone chunks to local clients only during an active voice session."""
        if not self.voice_listening:
            return
        payload = json.dumps({"type": "voice_audio_chunk", "pcm": pcm_base64})
        for client in list(self.clients):
            try:
                await client.send(payload)
            except Exception:
                pass

    def _open_camera(self):
        """Attempts to open any available camera on macOS or Windows."""
        backends = []
        if sys.platform == "darwin":
            backends = [cv2.CAP_AVFOUNDATION, cv2.CAP_ANY]
        elif sys.platform == "win32":
            backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]

        indices = [self.camera_index, 0, 1, 2]
        seen = set()
        unique_indices = [x for x in indices if not (x in seen or seen.add(x))]

        for backend in backends:
            for idx in unique_indices:
                try:
                    cap = cv2.VideoCapture(idx, backend)
                    if cap.isOpened():
                        ret, test_frame = cap.read()
                        if ret and test_frame is not None:
                            print(f"[AuraServer] Camera opened successfully on index {idx} (backend={backend})!", flush=True)
                            return cap, idx
                        cap.release()
                except Exception as e:
                    pass
        return None, None

    def _vision_loop(self):
        """Vision capture thread running at camera native FPS."""
        cap, active_idx = self._open_camera()
        last_retry_time = time.time()
        consecutive_read_failures = 0

        if cap is None:
            print("[AuraServer] Waiting for camera connection... (Will retry automatically)", flush=True)

        while self.running:
            # If camera not yet connected or failed, periodically attempt reconnection
            if cap is None or not cap.isOpened():
                self.latest_telemetry["camera_connected"] = False
                self.latest_telemetry["face_detected"] = False
                now = time.time()
                if now - last_retry_time > 2.0:
                    last_retry_time = now
                    cap, active_idx = self._open_camera()
                time.sleep(0.05)
                continue

            try:
                ret, frame = cap.read()
            except Exception as e:
                ret, frame = False, None

            if not ret or frame is None:
                consecutive_read_failures += 1
                self.latest_telemetry["face_detected"] = False
                if consecutive_read_failures > 15:
                    self.latest_telemetry["camera_connected"] = False
                    print("[AuraServer] Camera dropped frames. Reopening...", flush=True)
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                time.sleep(0.03)
                continue

            consecutive_read_failures = 0
            self.latest_telemetry["camera_connected"] = True
            if not self.camera_enabled:
                self.latest_telemetry["face_detected"] = False
                time.sleep(0.03)
                continue
            brightness = float(np.mean(frame))
            self.latest_telemetry["camera_brightness"] = brightness

            try:
                res = self.tracker.process_frame(frame)
            except RuntimeError:
                # Executor shut down (window closed) — exit vision loop cleanly
                break
            except Exception as e:
                print(f"[AuraServer] Face tracker warning: {e}", flush=True)
                res = None

            if res is not None:
                self.latest_telemetry["face_detected"] = True
                feat = res["features"]  # [yaw, pitch, iris_x, iris_y]

                # Trigger blink click pulse — store click_type (left vs right)
                # Only arm once gaze is properly established (avoids corner clicks on startup)
                if res.get("blink_click") and self.gaze_established_frames >= self.GAZE_ESTABLISH_FRAMES:
                    self.blink_click_until = time.time() + 0.15
                    self.blink_click_type = res.get("click_type", "left")
                    print(f"[AuraServer] Blink {self.blink_click_type}-click at ({self.latest_telemetry.get('x', 0):.0f}, {self.latest_telemetry.get('y', 0):.0f})", flush=True)

                self.gaze_established_frames += 1

                # Establish a steady resting pose from the median of the first
                # half-second of face samples, avoiding startup baseline drift.
                if self.gaze_established_frames <= self.neutral_samples.maxlen:
                    self.neutral_samples.append((
                        float(res["yaw"]), float(res["pitch"]),
                        float(res["iris_x"]), float(res["iris_y"]),
                    ))
                    neutral = np.median(np.asarray(self.neutral_samples), axis=0)
                    self.neutral_yaw, self.neutral_pitch = float(neutral[0]), float(neutral[1])
                    self.neutral_iris_x, self.neutral_iris_y = float(neutral[2]), float(neutral[3])

                # If eyes are closed during blink or wink, lock cursor to previous coordinates
                if res.get("is_eyes_closed") or res.get("is_winking"):
                    smooth_x = self.last_valid_smooth_x
                    smooth_y = self.last_valid_smooth_y
                else:
                    # If in target recording mode for calibration
                    if self.is_recording_target and self.recording_target_id is not None:
                        self.recorded_target_frames.append(feat)

                    # Map to screen coordinates
                    raw_x, raw_y = None, None
                    if self.calibration.model_x is not None:
                        try:
                            raw_x, raw_y = self.calibration.predict(feat)
                        except Exception as e:
                            print(f"[AuraServer] Calibration predict warning: {e}", flush=True)

                    if raw_x is None or raw_y is None:
                        # Sensor fusion: 70% Head Pose + 30% Iris Offset
                        rel_yaw = float(res["yaw"]) - self.neutral_yaw
                        rel_pitch = float(res["pitch"]) - self.neutral_pitch
                        iris_x_norm = float(res["iris_x"]) - self.neutral_iris_x
                        iris_y_norm = float(res["iris_y"]) - self.neutral_iris_y

                        # The uncalibrated fallback is intentionally conservative:
                        # small natural yaw and iris tremor should not sweep the
                        # cursor across the display before personal calibration.
                        head_x = (-rel_yaw / 48.0 + 0.5) * self.screen_w
                        head_y = (-rel_pitch / 30.0 + 0.5) * self.screen_h

                        iris_x = head_x + (iris_x_norm * self.screen_w * 0.15)
                        iris_y = head_y + (iris_y_norm * self.screen_h * 0.25)

                        # Clamp only to the physical display bounds; the user
                        # must be able to reach the actual screen edges.
                        raw_x = float(np.clip(iris_x, 0.0, self.screen_w - 1.0))
                        raw_y = float(np.clip(iris_y, 0.0, self.screen_h - 1.0))

                    # Remove isolated landmark jumps before temporal smoothing.
                    self.gaze_history.append((float(raw_x), float(raw_y)))
                    history = np.asarray(self.gaze_history, dtype=np.float64)
                    stable_x, stable_y = np.median(history, axis=0)

                    # Apply Deadband + One Euro Filter, then clamp to screen bounds
                    smooth_x, smooth_y = self.filter.filter(stable_x, stable_y)
                    smooth_x = max(0.0, min(float(self.screen_w), smooth_x))
                    smooth_y = max(0.0, min(float(self.screen_h), smooth_y))
                    self.last_valid_smooth_x = smooth_x
                    self.last_valid_smooth_y = smooth_y

                # Update telemetry
                self.latest_telemetry["x"] = float(smooth_x)
                self.latest_telemetry["y"] = float(smooth_y)
                self.latest_telemetry["iris_x"] = float(res["iris_x"])
                self.latest_telemetry["iris_y"] = float(res["iris_y"])
                self.latest_telemetry["is_blink_locked"] = bool(res["is_blink_locked"])
                self.latest_telemetry["abort_triggered"] = bool(res["abort_triggered"])
                self.latest_telemetry["raw_pose"] = {
                    "yaw": float(res["yaw"]),
                    "pitch": float(res["pitch"]),
                    "roll": float(res["roll"]),
                }
                self.latest_telemetry["blendshapes"] = {
                    "blink_l": float(res["blink_l"]),
                    "blink_r": float(res["blink_r"]),
                    "lookdown_l": float(res["lookdown_l"]),
                    "lookdown_r": float(res["lookdown_r"]),
                    "dynamic_blink_thresh": float(res["dynamic_blink_thresh"]),
                }
            else:
                self.latest_telemetry["face_detected"] = False
                self.gaze_established_frames = 0
                self.gaze_history.clear()
                self.neutral_samples.clear()
                self.filter.reset()

            # Generate Annotated Preview Thumbnail for Camera Overlay
            try:
                preview = cv2.resize(frame, (240, 160))
                h_p, w_p = preview.shape[:2]
                landmarks = res.get("landmarks") if res else None
                if landmarks:
                    l_iris = landmarks[468]
                    r_iris = landmarks[473]
                    lx, ly = int(l_iris.x * w_p), int(l_iris.y * h_p)
                    rx, ry = int(r_iris.x * w_p), int(r_iris.y * h_p)
                    cv2.circle(preview, (lx, ly), 3, (0, 255, 255), -1)
                    cv2.circle(preview, (rx, ry), 3, (0, 255, 255), -1)

                    ipd = math.hypot(lx - rx, ly - ry)
                    dist_text = "Optimal (52cm)" if 16 < ipd < 45 else ("Too Far" if ipd <= 16 else "Too Close")
                    cv2.putText(preview, dist_text, (8, 148), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 0), 1)
                else:
                    cv2.putText(preview, "Searching for face...", (8, 148), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 165, 255), 1)

                cv2.imwrite(os.path.join(_BASE_DIR, ".preview_frame.jpg"), preview, [cv2.IMWRITE_JPEG_QUALITY, 70])
            except Exception:
                pass

            time.sleep(0.015)  # ~60 Hz vision cadence

        if cap is not None and cap.isOpened():
            cap.release()

    async def _handle_message(self, websocket, message_str: str):
        """Processes control messages from Tauri frontend."""
        try:
            data = json.loads(message_str)
            cmd = data.get("command")
            if data.get("type") == "update_settings" and "voice_listening" in data:
                self.voice_listening = bool(data.get("voice_listening"))

            if cmd == "toggle_camera":
                self.camera_enabled = not self.camera_enabled
                await websocket.send(json.dumps({"type": "camera_state", "enabled": self.camera_enabled}))

            elif cmd == "start_record_target":
                target_id = data.get("target_id")
                self.recording_target_id = target_id
                self.recorded_target_frames = []
                self.is_recording_target = True
                await websocket.send(json.dumps({"status": "recording_target_started", "target_id": target_id}))

            elif cmd == "stop_record_target":
                self.is_recording_target = False
                if self.recording_target_id is not None and self.recorded_target_frames:
                    self.calibration.add_target_samples(
                        self.recording_target_id, self.recorded_target_frames
                    )
                frames_count = len(self.recorded_target_frames)
                self.recorded_target_frames = []
                await websocket.send(json.dumps({
                    "status": "recording_target_saved",
                    "target_id": self.recording_target_id,
                    "frames_recorded": frames_count
                }))
                self.recording_target_id = None

            elif cmd == "finish_gaze_calibration":
                try:
                    diag = self.calibration.run_lopo_cross_validation()
                    self.calibration.save(CALIBRATION_PATH)
                    await websocket.send(json.dumps({
                        "status": "gaze_calibration_finished",
                        "diagnostics": diag
                    }))
                except Exception as exc:
                    await websocket.send(json.dumps({
                        "status": "gaze_calibration_error",
                        "message": str(exc)
                    }))

            elif cmd == "record_audio_click":
                # Capture current 300ms audio buffer as click sample
                with self.audio_lock:
                    sample = self.audio_buffer[-4800:].copy()
                self.recorded_click_audios.append(sample)
                await websocket.send(json.dumps({
                    "status": "audio_click_recorded",
                    "count": len(self.recorded_click_audios)
                }))

            elif cmd == "record_audio_noise":
                # Capture current 500ms audio buffer as supra-threshold noise sample
                with self.audio_lock:
                    sample = self.audio_buffer[-8000:].copy()
                self.recorded_noise_audios.append(sample)
                await websocket.send(json.dumps({
                    "status": "audio_noise_recorded",
                    "count": len(self.recorded_noise_audios)
                }))

            elif cmd == "finish_audio_calibration":
                # Calibrate acoustic classifier, compute LOO confusion matrix & R_max
                eval_res = self.audio_classifier.calibrate(
                    self.recorded_click_audios, self.recorded_noise_audios
                )
                self.audio_classifier.save(AUDIO_MODEL_PATH)
                await websocket.send(json.dumps({
                    "status": "audio_calibration_finished",
                    "evaluation": eval_res
                }))

            elif cmd == "learn_oops":
                # Retain a user-confirmed recent vocalization in the rolling k-NN bank.
                if not self.audio_classifier.is_calibrated:
                    await websocket.send(json.dumps({
                        "status": "oops_learn_error",
                        "message": "Train the sound model first."
                    }))
                else:
                    with self.audio_lock:
                        sample = self.audio_buffer[-32000:].copy()
                    self.audio_classifier.add_verified_sample(sample)
                    self.audio_classifier.save(AUDIO_MODEL_PATH)
                    await websocket.send(json.dumps({
                        "status": "oops_learned",
                        "count": len(self.audio_classifier.dynamic_clicks)
                    }))

            elif cmd == "set_deadband":
                radius = float(data.get("radius", 15.0))
                self.filter.deadband_radius = radius
                await websocket.send(json.dumps({"status": "deadband_updated", "radius": radius}))

            # Handle system actions (launching macOS apps, opening accessibility settings, etc.)
            if data.get("type") == "system_action":
                action = data.get("action")
                if action == "open_app":
                    app_name = data.get("app")
                    if app_name:
                        print(f"[AuraServer] System action: opening app '{app_name}'", flush=True)
                        if sys.platform == "darwin":
                            import subprocess
                            subprocess.Popen(["open", "-a", app_name])
                        elif sys.platform == "win32":
                            import os
                            try:
                                os.startfile(app_name)
                            except Exception:
                                pass
                elif action == "open_accessibility_settings" and sys.platform == "darwin":
                    import subprocess
                    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
                elif action == "check_accessibility":
                    trusted = True
                    if sys.platform == "darwin":
                        try:
                            import ApplicationServices
                            trusted = bool(ApplicationServices.AXIsProcessTrusted())
                        except Exception:
                            trusted = False
                    await websocket.send(json.dumps({"type": "accessibility_status", "trusted": trusted}))

            # Forward settings, voice updates, and system actions to all connected clients
            if data.get("type") in ["update_settings", "voice_feedback", "voice_command", "system_action", "accessibility_status", "speech_text", "voice_status"]:
                payload = json.dumps(data)
                for client in list(self.clients):
                    if client != websocket:
                        try:
                            await client.send(payload)
                        except Exception:
                            pass

        except Exception as e:
            await websocket.send(json.dumps({"error": str(e)}))

    async def _ws_handler(self, websocket):
        self.clients.add(websocket)
        try:
            async for message in websocket:
                await self._handle_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)

    async def _broadcast_telemetry_loop(self):
        """Broadcasts telemetry packet to all connected clients at 60Hz."""
        while self.running:
            now = time.time()
            is_blink_clicking = (now < self.blink_click_until)
            is_clicking = bool(self.audio_click_active or is_blink_clicking)
            self.latest_telemetry["click"] = is_clicking
            if is_blink_clicking:
                self.latest_telemetry["click_confidence"] = 1.0
                self.latest_telemetry["click_source"] = "blink"
                self.latest_telemetry["click_type"] = self.blink_click_type
            elif self.audio_click_active:
                self.latest_telemetry["click_source"] = "audio"
                self.latest_telemetry["click_type"] = "left"
            else:
                self.latest_telemetry["click_source"] = "none"
                self.latest_telemetry["click_type"] = "none"

            self.latest_telemetry["audio_calibrated"] = bool(self.audio_classifier.is_calibrated)
            self.latest_telemetry["gaze_calibrated"] = self.calibration.model_x is not None and self.calibration.model_y is not None
            if self.clients:
                self.latest_telemetry["timestamp"] = now
                payload = json.dumps(self.latest_telemetry)
                # Broadcast concurrently
                await asyncio.gather(
                    *[client.send(payload) for client in self.clients],
                    return_exceptions=True
                )
            await asyncio.sleep(0.016)  # ~60 FPS

    def start(self):
        self.running = True

        # Start Vision Thread
        self.vision_thread = threading.Thread(target=self._vision_loop, daemon=True)
        self.vision_thread.start()

        # Start Audio Stream — capture at native macOS rate (48kHz) to avoid CoreAudio -50 error
        try:
            self.audio_stream = sd.InputStream(
                samplerate=self._audio_native_rate,
                channels=1,
                blocksize=512 * self._audio_downsample,  # keep ~same time window per callback
                callback=self._audio_callback
            )
            self.audio_stream.start()
            print(f"[AuraServer] Audio input stream started at {self._audio_native_rate}Hz (effective 16kHz after downsample).")
        except Exception as e:
            print(f"[AuraServer] Warning: Could not open audio input stream: {e}")

        # Start Asyncio WebSocket loop
        async def main():
            self.async_loop = asyncio.get_running_loop()
            async with websockets.serve(self._ws_handler, "127.0.0.1", PORT):
                print(f"[AuraServer] WebSocket streaming on ws://127.0.0.1:{PORT}")
                await self._broadcast_telemetry_loop()

        asyncio.run(main())

    def stop(self):
        self.running = False
        if hasattr(self, "audio_stream") and self.audio_stream:
            self.audio_stream.stop()
            self.audio_stream.close()
        self.tracker.close()


if __name__ == "__main__":
    server = AuraEngineServer()
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
        print("[AuraServer] Stopped.")
