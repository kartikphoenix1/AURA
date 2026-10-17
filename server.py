import asyncio
import json
import os
import sys
import time
import threading
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

        # Core Vision & Filter Subsystems
        self.tracker = FaceGazeTracker()
        self.calibration = GazeCalibration(screen_w=self.screen_w, screen_h=self.screen_h)
        self.filter = DeadbandOneEuroFilter2D(deadband_radius=15.0, min_cutoff=0.8, beta=0.008)

        # Audio Subsystem
        self.audio_classifier = AcousticClassifier(sample_rate=16000)

        # Try loading existing models if present
        if os.path.exists(CALIBRATION_PATH):
            try:
                self.calibration.load(CALIBRATION_PATH)
                print(f"[AuraServer] Loaded gaze calibration from {CALIBRATION_PATH}")
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
        self.blink_click_until: float = 0.0
        self.audio_click_active: bool = False
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

        # Audio rolling circular buffer (last 2 seconds at 16kHz)
        self.audio_buffer_size = 16000 * 2
        self.audio_buffer = np.zeros(self.audio_buffer_size, dtype=np.float32)
        self.audio_lock = threading.Lock()

        # Calibration recording buffers
        self.is_recording_target: bool = False
        self.recording_target_id: Optional[int] = None
        self.recorded_target_frames: List[np.ndarray] = []

        self.recorded_click_audios: List[np.ndarray] = []
        self.recorded_noise_audios: List[np.ndarray] = []

        # Connected WebSocket clients
        self.clients = set()

    def _audio_callback(self, indata, frames, time_info, status):
        """Sounddevice input stream callback running at ~30-50 fps."""
        audio_chunk = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(audio_chunk**2)))

        with self.audio_lock:
            # Roll buffer and insert new chunk
            self.audio_buffer = np.roll(self.audio_buffer, -len(audio_chunk))
            self.audio_buffer[-len(audio_chunk):] = audio_chunk

        is_above_gate = self.audio_classifier.update_noise_floor(rms)

        is_click = False
        confidence = 0.0

        if is_above_gate and self.audio_classifier.is_calibrated:
            # Take candidate burst (last 200ms)
            candidate = self.audio_buffer[-3200:]
            is_click, confidence, _ = self.audio_classifier.classify_audio(candidate)

        self.audio_click_active = bool(is_click)
        self.latest_telemetry["click_confidence"] = float(confidence)
        self.latest_telemetry["audio"] = {
            "rms": rms,
            "noise_floor": float(self.audio_classifier.noise_floor),
            "current_gate": float(self.audio_classifier.current_gate),
            "is_above_gate": is_above_gate,
        }

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
            brightness = float(np.mean(frame))
            self.latest_telemetry["camera_brightness"] = brightness

            try:
                res = self.tracker.process_frame(frame)
            except Exception as e:
                print(f"[AuraServer] Face tracker warning: {e}", flush=True)
                res = None

            if res is not None:
                self.latest_telemetry["face_detected"] = True
                feat = res["features"]  # [yaw, pitch, iris_x, iris_y]

                # Trigger blink click pulse
                if res.get("blink_click"):
                    self.blink_click_until = time.time() + 0.15
                    print(f"[AuraServer] Dispatched Blink Click at ({self.latest_telemetry.get('x'):.0f}, {self.latest_telemetry.get('y'):.0f})", flush=True)

                # If eyes are closed during blink, pin cursor to previous coordinates so it does not drift
                if res.get("is_eyes_closed"):
                    smooth_x = self.last_valid_smooth_x
                    smooth_y = self.last_valid_smooth_y
                else:
                    # If in target recording mode for calibration
                    if self.is_recording_target and self.recording_target_id is not None:
                        self.recorded_target_frames.append(feat)

                    # Map to screen coordinates
                    if self.calibration.model_x is not None:
                        raw_x, raw_y = self.calibration.predict(feat)
                    else:
                        # Responsive default gaze estimate (mirrored for natural webcam feel)
                        # Comfortable range to effortlessly reach Start button and all 4 corners
                        head_x = (-res["yaw"] / 18.0 + 0.5) * self.screen_w
                        # Tilting up (positive pitch) moves toward top of screen (0); looking down reaches taskbar
                        head_y = (-res["pitch"] / 14.0 + 0.5) * self.screen_h
                        # Iris fine offset
                        iris_offset_x = (res["iris_x"] - 0.5) * 400.0
                        iris_offset_y = (res["iris_y"] - 0.5) * 300.0

                        raw_x = max(0.0, min(float(self.screen_w), head_x - iris_offset_x))
                        raw_y = max(0.0, min(float(self.screen_h), head_y + iris_offset_y))

                    # Apply Deadband + One Euro Filter
                    smooth_x, smooth_y = self.filter.filter(raw_x, raw_y)
                    self.last_valid_smooth_x = smooth_x
                    self.last_valid_smooth_y = smooth_y

                # Update telemetry
                self.latest_telemetry["x"] = float(smooth_x)
                self.latest_telemetry["y"] = float(smooth_y)
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

            time.sleep(0.015)  # ~60 Hz vision cadence

        if cap is not None and cap.isOpened():
            cap.release()

    async def _handle_message(self, websocket, message_str: str):
        """Processes control messages from Tauri frontend."""
        try:
            data = json.loads(message_str)
            cmd = data.get("command")

            if cmd == "start_record_target":
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
                # Run LOPO-CV with Center/Edge/Corner error reporting
                diag = self.calibration.run_lopo_cross_validation()
                self.calibration.save(CALIBRATION_PATH)
                await websocket.send(json.dumps({
                    "status": "gaze_calibration_finished",
                    "diagnostics": diag
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
                # "Oops, learn that": user triggered shortcut to add last 1.5s audio
                with self.audio_lock:
                    sample = self.audio_buffer[-24000:].copy()
                self.audio_classifier.add_verified_sample(sample)
                await websocket.send(json.dumps({"status": "oops_learned"}))

            elif cmd == "set_deadband":
                radius = float(data.get("radius", 15.0))
                self.filter.deadband_radius = radius
                await websocket.send(json.dumps({"status": "deadband_updated", "radius": radius}))

            # Forward settings and voice updates to all connected clients
            if data.get("type") in ["update_settings", "voice_feedback", "voice_command"]:
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
            elif self.audio_click_active:
                self.latest_telemetry["click_source"] = "audio"
            else:
                self.latest_telemetry["click_source"] = "none"

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

        # Start Audio Stream
        try:
            self.audio_stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                blocksize=512,
                callback=self._audio_callback
            )
            self.audio_stream.start()
            print("[AuraServer] Audio input stream started at 16000Hz.")
        except Exception as e:
            print(f"[AuraServer] Warning: Could not open audio input stream: {e}")

        # Start Asyncio WebSocket loop
        async def main():
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
