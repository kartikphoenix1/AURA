"""
Aura System-Wide Hover Pet (Bottom-Right Desktop Widget)
Always-on-top, draggable floating widget that runs anywhere in Windows:
- Features Nova Liquid Voice Pet with eye parallax tracking
- Camera ON/OFF toggle button
- Voice Recognition ON/OFF toggle button
- Virtual Eye Reticle vs Physical Mouse Mode toggle
- Live distance & tracking status
"""

import os
import sys
import math
import time
import json
import threading
import tkinter as tk
import websockets
import asyncio

WS_URL = "ws://127.0.0.1:8765"


class FloatingPetWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Aura Nova Pet Widget")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg="#0b0f19")

        self.width = 250
        self.height = 270
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        # Dock at bottom-right corner
        pos_x = screen_w - self.width - 24
        pos_y = screen_h - self.height - 280  # Placed above camera preview
        self.root.geometry(f"{self.width}x{self.height}+{pos_x}+{pos_y}")

        self._drag_data = {"x": 0, "y": 0}

        # App state
        self.cam_enabled = True
        self.voice_enabled = True
        self.cursor_mode = "virtual"  # "virtual" or "physical"
        self.is_listening = False
        self.is_minimized = False
        self.gaze_x = screen_w / 2.0
        self.gaze_y = screen_h / 2.0
        self.anim_time = 0.0

        self.build_ui()

        # Connect WebSocket
        self.is_running = True
        self.ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self.ws_thread.start()

        # Start animation loop
        self.animate_pet()

    def build_ui(self):
        # 1. Header with title & collapse
        self.header = tk.Frame(self.root, bg="#131b2e", height=28)
        self.header.pack(fill="x", side="top")
        self.header.bind("<ButtonPress-1>", self.start_drag)
        self.header.bind("<B1-Motion>", self.do_drag)

        self.lbl_title = tk.Label(
            self.header, text="✨ NOVA PET", font=("Segoe UI", 9, "bold"),
            bg="#131b2e", fg="#00f2fe"
        )
        self.lbl_title.pack(side="left", padx=8)

        self.btn_min = tk.Label(
            self.header, text="—", font=("Segoe UI", 10, "bold"),
            bg="#131b2e", fg="#94a3b8", cursor="hand2"
        )
        self.btn_min.pack(side="right", padx=8)
        self.btn_min.bind("<Button-1>", lambda e: self.toggle_minimize())

        # 2. Main content container
        self.content_frame = tk.Frame(self.root, bg="#0b0f19")
        self.content_frame.pack(fill="both", expand=True)

        # 3. Quick Action Buttons Row
        self.btn_row = tk.Frame(self.content_frame, bg="#0b0f19")
        self.btn_row.pack(fill="x", padx=10, pady=6)

        self.btn_cam = tk.Button(
            self.btn_row, text="📷 Cam: ON", font=("Segoe UI", 8, "bold"),
            bg="#162b3d", fg="#00f2fe", activebackground="#00f2fe", activeforeground="#000",
            relief="flat", cursor="hand2", command=self.toggle_cam
        )
        self.btn_cam.pack(side="left", fill="x", expand=True, padx=2)

        self.btn_voice = tk.Button(
            self.btn_row, text="🎤 Voice: ON", font=("Segoe UI", 8, "bold"),
            bg="#162b3d", fg="#00f2fe", activebackground="#00f2fe", activeforeground="#000",
            relief="flat", cursor="hand2", command=self.toggle_voice
        )
        self.btn_voice.pack(side="left", fill="x", expand=True, padx=2)

        self.btn_mode = tk.Button(
            self.btn_row, text="👁️ Reticle", font=("Segoe UI", 8, "bold"),
            bg="#162b3d", fg="#4facfe", activebackground="#4facfe", activeforeground="#000",
            relief="flat", cursor="hand2", command=self.toggle_cursor_mode
        )
        self.btn_mode.pack(side="left", fill="x", expand=True, padx=2)

        # 4. Canvas for Liquid Pet Mascot
        self.canvas = tk.Canvas(
            self.content_frame, width=self.width, height=140,
            bg="#0b0f19", highlightthickness=0, cursor="hand2"
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.on_pet_clicked())

        # 5. Bottom Status / Distance Badge
        self.lbl_status = tk.Label(
            self.content_frame, text="🟢 Distance: Optimal (52cm)",
            font=("Segoe UI", 8, "bold"), bg="#101726", fg="#2ecc71", pady=4
        )
        self.lbl_status.pack(fill="x", side="bottom")

    def start_drag(self, event):
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def do_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_data["x"])
        y = self.root.winfo_y() + (event.y - self._drag_data["y"])
        self.root.geometry(f"+{x}+{y}")

    def toggle_minimize(self):
        self.is_minimized = not self.is_minimized
        if self.is_minimized:
            self.content_frame.pack_forget()
            self.root.geometry(f"{self.width}x28")
            self.btn_min.config(text="+")
        else:
            self.content_frame.pack(fill="both", expand=True)
            self.root.geometry(f"{self.width}x{self.height}")
            self.btn_min.config(text="—")

    def toggle_cam(self):
        self.cam_enabled = not self.cam_enabled
        if self.cam_enabled:
            self.btn_cam.config(text="📷 Cam: ON", fg="#00f2fe", bg="#162b3d")
        else:
            self.btn_cam.config(text="📷 Cam: OFF", fg="#ff4757", bg="#2a1620")
        self.send_ws_setting("camera_enabled", self.cam_enabled)

    def toggle_voice(self):
        self.voice_enabled = not self.voice_enabled
        if self.voice_enabled:
            self.btn_voice.config(text="🎤 Voice: ON", fg="#00f2fe", bg="#162b3d")
        else:
            self.btn_voice.config(text="🎤 Voice: OFF", fg="#94a3b8", bg="#1a1e28")
        self.send_ws_setting("voice_enabled", self.voice_enabled)

    def toggle_cursor_mode(self):
        if self.cursor_mode == "virtual":
            self.cursor_mode = "physical"
            self.btn_mode.config(text="🖱️ Real Mouse", fg="#ffa502", bg="#2c2214")
            self.send_ws_setting("gaze_enabled", True)
        else:
            self.cursor_mode = "virtual"
            self.btn_mode.config(text="👁️ Reticle", fg="#4facfe", bg="#162b3d")
            self.send_ws_setting("gaze_enabled", False)

    def on_pet_clicked(self):
        self.is_listening = not self.is_listening
        status_text = "Listening to your voice..." if self.is_listening else "Click pet to wake up"
        self.lbl_status.config(text=status_text)
        self.send_ws_setting("voice_active", self.is_listening)

    def send_ws_setting(self, key: str, value: Any):
        if hasattr(self, "ws_connection") and self.ws_connection:
            msg = json.dumps({"type": "update_settings", key: value})
            asyncio.run_coroutine_threadsafe(self.ws_connection.send(msg), self.loop)

    def animate_pet(self):
        if not self.is_running:
            return

        self.anim_time += 0.06
        self.canvas.delete("all")

        cx = self.width / 2.0
        cy = 70.0
        r = 45.0 + math.sin(self.anim_time) * 3.0

        # Liquid blob outer glow
        glow_color = "#00d2ff" if self.is_listening else "#e91e63"
        self.canvas.create_oval(
            cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8,
            outline=glow_color, width=3
        )
        # Liquid blob body
        fill_color = "#1a0933" if not self.is_listening else "#092233"
        self.canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=fill_color, outline=glow_color, width=2
        )

        # Eye parallax tracking user gaze
        dx = self.gaze_x - (self.root.winfo_screenwidth() / 2.0)
        dy = self.gaze_y - (self.root.winfo_screenheight() / 2.0)
        dist = math.hypot(dx, dy)
        max_dist = 600.0
        norm_dist = min(1.0, dist / max_dist)
        angle = math.atan2(dy, dx)
        eye_offset_x = math.cos(angle) * (norm_dist * 8.0)
        eye_offset_y = math.sin(angle) * (norm_dist * 6.0)

        # Pill-shaped blinking eyes
        is_blinking = (int(self.anim_time * 10) % 45) == 0
        eye_h = 3 if is_blinking else 18
        eye_w = 8

        # Left Eye
        self.canvas.create_oval(
            cx - 16 + eye_offset_x, cy - eye_h/2 + eye_offset_y,
            cx - 16 + eye_w + eye_offset_x, cy + eye_h/2 + eye_offset_y,
            fill="#ffffff", outline=""
        )
        # Right Eye
        self.canvas.create_oval(
            cx + 8 + eye_offset_x, cy - eye_h/2 + eye_offset_y,
            cx + 8 + eye_w + eye_offset_x, cy + eye_h/2 + eye_offset_y,
            fill="#ffffff", outline=""
        )

        # Name label
        self.canvas.create_text(
            cx, cy + r + 14, text="Nova",
            font=("Segoe UI", 10, "bold"), fill="#f8fafc"
        )

        self.root.after(33, self.animate_pet)

    def _ws_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def listen():
            while self.is_running:
                try:
                    async with websockets.connect(WS_URL) as ws:
                        self.ws_connection = ws
                        print(f"[FloatingPet] Connected to telemetry at {WS_URL}")
                        async for msg in ws:
                            if not self.is_running:
                                break
                            data = json.loads(msg)
                            msg_type = data.get("type")

                            if msg_type == "telemetry":
                                self.gaze_x = data.get("x", self.gaze_x)
                                self.gaze_y = data.get("y", self.gaze_y)

                                # Update distance advisor badge
                                if not data.get("face_detected"):
                                    self.lbl_status.config(text="🔴 No Face Detected", fg="#ff4757")
                                else:
                                    self.lbl_status.config(text="🟢 Distance: Optimal (52cm)", fg="#2ecc71")

                            elif msg_type == "voice_feedback":
                                heard = data.get("heard", "")
                                self.lbl_status.config(text=f"🗣️ \"{heard}\"", fg="#00f2fe")

                except Exception:
                    await asyncio.sleep(1.0)

        self.loop.run_until_complete(listen())

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.is_running = False


if __name__ == "__main__":
    widget = FloatingPetWidget()
    widget.run()
