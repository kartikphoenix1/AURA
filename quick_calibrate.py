"""
Aura Rapid 5-Point Gaze Calibration Utility
Takes ~8 seconds:
Shows 5 pulsating target dots across your screen:
1. Center
2. Top-Left
3. Top-Right
4. Bottom-Left
5. Bottom-Right

Collects your personalized eye + head geometry, fits a 2D Ridge regression model,
and saves to models/gaze_calibration.joblib for 1:1 pixel accuracy.
"""

import os
import sys
import time
import json
import threading
import tkinter as tk
import numpy as np
import websockets
import asyncio
from sklearn.linear_model import Ridge
import joblib

WS_URL = "ws://127.0.0.1:8765"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "gaze_calibration.joblib")


class QuickCalibrateApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Aura 5-Point Rapid Calibration")
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.config(bg="#0b0f19")

        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()

        self.canvas = tk.Canvas(
            self.root, width=self.screen_w, height=self.screen_h,
            bg="#0b0f19", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        # 5 Targets: Center, TL, TR, BL, BR
        self.targets = [
            {"x": int(self.screen_w * 0.50), "y": int(self.screen_h * 0.50), "name": "Center Target"},
            {"x": int(self.screen_w * 0.12), "y": int(self.screen_h * 0.12), "name": "Top-Left Target"},
            {"x": int(self.screen_w * 0.88), "y": int(self.screen_h * 0.12), "name": "Top-Right Target"},
            {"x": int(self.screen_w * 0.12), "y": int(self.screen_h * 0.88), "name": "Bottom-Left Target"},
            {"x": int(self.screen_w * 0.88), "y": int(self.screen_h * 0.88), "name": "Bottom-Right Target"},
        ]

        self.current_idx = 0
        self.target_data = []  # list of (features, (x, y))
        self.current_target_samples = []
        self.latest_features = None
        self.pulse_phase = 0.0

        # Start WebSocket receiver
        self.is_running = True
        self.ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self.ws_thread.start()

        # Keyboard listener
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # Start sequence
        self.root.after(1000, self.start_next_target)
        self.animate_target()

    def _ws_loop(self):
        async def listen():
            while self.is_running:
                try:
                    async with websockets.connect(WS_URL) as ws:
                        self.ws = ws
                        async for msg in ws:
                            if not self.is_running:
                                break
                            data = json.loads(msg)
                            if data.get("type") == "telemetry" and data.get("face_detected"):
                                raw_pose = data.get("raw_pose", {})
                                yaw = raw_pose.get("yaw", 0.0)
                                pitch = raw_pose.get("pitch", 0.0)
                                # Iris ratios
                                blend = data.get("blendshapes", {})
                                iris_x = data.get("iris_x", 0.5)
                                iris_y = data.get("iris_y", 0.5)
                                self.latest_features = [yaw, pitch, iris_x, iris_y]
                except Exception:
                    await asyncio.sleep(0.5)

        asyncio.run(listen())

    def start_next_target(self):
        if self.current_idx >= len(self.targets):
            self.finish_calibration()
            return

        t = self.targets[self.current_idx]
        self.current_target_samples = []
        self.sample_end_time = time.time() + 1.4  # Record for 1.4s

        # Poll samples for this target
        self.collect_samples()

    def collect_samples(self):
        if time.time() < self.sample_end_time:
            if self.latest_features:
                self.current_target_samples.append(list(self.latest_features))
            self.root.after(30, self.collect_samples)
        else:
            # Done recording this target
            t = self.targets[self.current_idx]
            if len(self.current_target_samples) > 5:
                # Discard first few transition frames
                steady_samples = self.current_target_samples[4:]
                avg_feat = np.mean(steady_samples, axis=0)
                self.target_data.append((avg_feat, (t["x"], t["y"])))
                print(f"[QuickCalibrate] Recorded target {self.current_idx+1}/5 at ({t['x']}, {t['y']})")

            self.current_idx += 1
            self.root.after(400, self.start_next_target)

    def finish_calibration(self):
        """Fits Ridge Regression on collected pairs and saves model."""
        self.canvas.delete("all")
        self.canvas.create_text(
            self.screen_w // 2, self.screen_h // 2 - 40,
            text="✨ Calibration Complete!",
            font=("Segoe UI", 32, "bold"), fill="#00f2fe"
        )
        self.canvas.create_text(
            self.screen_w // 2, self.screen_h // 2 + 20,
            text="Optimizing precision model for your face...",
            font=("Segoe UI", 16), fill="#94a3b8"
        )
        self.root.update()

        if len(self.target_data) >= 4:
            X = np.array([item[0] for item in self.target_data])
            y = np.array([item[1] for item in self.target_data])

            # Fit 2D Ridge regression
            model = Ridge(alpha=1.0)
            model.fit(X, y)

            os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
            joblib.dump(model, MODEL_PATH)
            print(f"[QuickCalibrate] Successfully trained and saved model to {MODEL_PATH}")

        self.root.after(1500, self.root.destroy)

    def animate_target(self):
        if self.current_idx < len(self.targets):
            self.canvas.delete("all")
            t = self.targets[self.current_idx]
            self.pulse_phase += 0.15
            pulse_r = int(24 + math.sin(self.pulse_phase) * 6)

            # Outer glow ring
            self.canvas.create_oval(
                t["x"] - pulse_r - 12, t["y"] - pulse_r - 12,
                t["x"] + pulse_r + 12, t["y"] + pulse_r + 12,
                outline="rgba(0, 242, 254, 0.4)", width=2
            )
            # Inner solid target dot
            self.canvas.create_oval(
                t["x"] - pulse_r, t["y"] - pulse_r,
                t["x"] + pulse_r, t["y"] + pulse_r,
                fill="#00f2fe", outline="#ffffff", width=2
            )
            # Center bullseye
            self.canvas.create_oval(
                t["x"] - 4, t["y"] - 4, t["x"] + 4, t["y"] + 4,
                fill="#ffffff", outline=""
            )

            # Instructions
            self.canvas.create_text(
                self.screen_w // 2, 80,
                text=f"Look at Target {self.current_idx + 1} of 5 ({t['name']})",
                font=("Segoe UI", 22, "bold"), fill="#f8fafc"
            )
            self.canvas.create_text(
                self.screen_w // 2, 120,
                text="Keep your head naturally relaxed and fixate on the glowing cyan circle",
                font=("Segoe UI", 13), fill="#94a3b8"
            )

        self.root.after(30, self.animate_target)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = QuickCalibrateApp()
    app.run()
