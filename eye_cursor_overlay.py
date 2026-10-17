"""
Aura Virtual Eye Cursor Overlay
A fullscreen, completely click-through, always-on-top transparent overlay.
Renders a glowing virtual eye cursor reticle at (x, y) where the user is looking.
Does NOT hijack or move the physical Windows mouse pointer.
"""

import math
import time
import json
import threading
import sys
from typing import Optional

import tkinter as tk
import websockets
import asyncio

if sys.platform == "win32":
    import ctypes
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    user32 = ctypes.windll.user32
else:
    user32 = None


class EyeCursorOverlay:
    def __init__(self, ws_url: str = "ws://127.0.0.1:8765"):
        self.ws_url = ws_url
        self.root = tk.Tk()
        self.root.title("Aura Eye Cursor Overlay")
        self.root.overrideredirect(True)

        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        self.root.attributes("-topmost", True)
        
        # Color keying for transparency
        self.trans_color = "#010101"
        self.root.attributes("-transparentcolor", self.trans_color)
        self.root.config(bg=self.trans_color)

        # Fullscreen transparent canvas
        self.canvas = tk.Canvas(
            self.root,
            width=self.screen_w,
            height=self.screen_h,
            bg=self.trans_color,
            highlightthickness=0,
            cursor="none"
        )
        self.canvas.pack(fill="both", expand=True)

        # Enable click-through
        self._apply_click_through()

        # Cursor state
        self.target_x = self.screen_w / 2.0
        self.target_y = self.screen_h / 2.0
        self.curr_x = self.target_x
        self.curr_y = self.target_y
        self.is_visible = False
        self.click_pulse_until = 0.0
        self.click_type = "left"  # left or right

        # Dwell progress tracking
        self.dwell_anchor = (self.curr_x, self.curr_y)
        self.dwell_start_time = time.time()
        self.dwell_target_time = 1.0  # seconds
        self.dwell_progress = 0.0

        # Start WebSocket receiver
        self.is_running = True
        self.ws_thread = threading.Thread(target=self._ws_receiver_thread, daemon=True)
        self.ws_thread.start()

        # Start 60 FPS Canvas Render Loop
        self.render_loop()

    def _apply_click_through(self):
        """Applies WS_EX_TRANSPARENT to allow mouse clicks to pass straight through."""
        if user32:
            try:
                # Force update to ensure window has a valid HWND
                self.root.update_idletasks()
                hwnd = user32.FindWindowW(None, "Aura Eye Cursor Overlay")
                if not hwnd:
                    hwnd = self.root.winfo_id()
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            except Exception as e:
                print(f"[EyeCursorOverlay] Click-through warning: {e}")

    def render_loop(self):
        """High-speed 60 FPS animation loop for smooth reticle movement."""
        if not self.is_running:
            return

        self.canvas.delete("reticle")

        if self.is_visible:
            # Smooth exponential catch-up (lerp)
            self.curr_x += (self.target_x - self.curr_x) * 0.35
            self.curr_y += (self.target_y - self.curr_y) * 0.35

            cx, cy = self.curr_x, self.curr_y
            now = time.time()

            # Dwell circle accumulation
            dist = math.hypot(cx - self.dwell_anchor[0], cy - self.dwell_anchor[1])
            if dist < 25:
                elapsed = now - self.dwell_start_time
                self.dwell_progress = min(1.0, elapsed / self.dwell_target_time)
            else:
                self.dwell_anchor = (cx, cy)
                self.dwell_start_time = now
                self.dwell_progress = 0.0

            # Determine reticle color based on click flash
            is_clicking = now < self.click_pulse_until
            if is_clicking:
                ring_color = "#ff007f" if self.click_type == "right" else "#00ffff"
                radius = 24
                dot_size = 6
            else:
                ring_color = "#00f2fe"
                radius = 16
                dot_size = 4

            # 1. Outer Glow Ring
            self.canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                outline=ring_color, width=2, tags="reticle"
            )

            # 2. Inner Precision Dot
            self.canvas.create_oval(
                cx - dot_size, cy - dot_size, cx + dot_size, cy + dot_size,
                fill=ring_color, outline="", tags="reticle"
            )

            # 3. Crosshair markers
            arm = radius + 6
            self.canvas.create_line(cx - arm, cy, cx - radius + 2, cy, fill=ring_color, width=1.5, tags="reticle")
            self.canvas.create_line(cx + radius - 2, cy, cx + arm, cy, fill=ring_color, width=1.5, tags="reticle")
            self.canvas.create_line(cx, cy - arm, cx, cy - radius + 2, fill=ring_color, width=1.5, tags="reticle")
            self.canvas.create_line(cx, cy + radius - 2, cx, cy + arm, fill=ring_color, width=1.5, tags="reticle")

            # 4. Dwell Progress Arc (fills from 0 to 360 deg)
            if self.dwell_progress > 0.05:
                extent = int(self.dwell_progress * 359)
                dwell_r = radius + 5
                self.canvas.create_arc(
                    cx - dwell_r, cy - dwell_r, cx + dwell_r, cy + dwell_r,
                    start=90, extent=-extent, style="arc",
                    outline="#2ecc71", width=3, tags="reticle"
                )

        # Schedule next frame at ~60 FPS (16ms)
        self.root.after(16, self.render_loop)

    def _ws_receiver_thread(self):
        """Background thread receiving gaze telemetry from server.py."""
        async def listen():
            while self.is_running:
                try:
                    async with websockets.connect(self.ws_url) as ws:
                        print(f"[EyeCursorOverlay] Connected to telemetry stream at {self.ws_url}")
                        async for msg in ws:
                            if not self.is_running:
                                break
                            data = json.loads(msg)
                            msg_type = data.get("type")

                            if msg_type == "telemetry":
                                face_detected = data.get("face_detected", False)
                                is_blink_locked = data.get("is_blink_locked", False)
                                abort_triggered = data.get("abort_triggered", False)

                                if face_detected and not is_blink_locked and not abort_triggered:
                                    self.is_visible = True
                                    self.target_x = data.get("x", self.target_x)
                                    self.target_y = data.get("y", self.target_y)
                                else:
                                    self.is_visible = False

                                # Check click pulses
                                if data.get("click", False):
                                    self.click_pulse_until = time.time() + 0.25
                                    source = data.get("click_source", "left")
                                    self.click_type = "right" if "right" in source else "left"

                            elif msg_type == "update_settings":
                                if "dwell_time" in data:
                                    self.dwell_target_time = max(0.3, float(data["dwell_time"]))

                except Exception:
                    await asyncio.sleep(1.0)

        asyncio.run(listen())

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.is_running = False


if __name__ == "__main__":
    overlay = EyeCursorOverlay()
    overlay.run()
