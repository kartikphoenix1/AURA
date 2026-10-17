"""
Aura Floating Camera Preview Overlay
Compact, always-on-top, draggable webcam preview window.
Displays:
- Live face tracking video feed
- Real-time facial wireframe & pupil tracking crosshairs
- Distance Advisor badge (Optimal / Too Close / Too Far)
- Drag to reposition anywhere on screen
"""

import os
import sys
import time
import tkinter as tk
from PIL import Image, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PREVIEW_FILE = os.path.join(BASE_DIR, ".preview_frame.jpg")


class CameraOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Aura Camera Feed")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg="#0b0f19")

        self.width = 240
        self.height = 195
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        # Position at bottom-right (just above taskbar, next to pet)
        pos_x = screen_w - self.width - 24
        pos_y = screen_h - self.height - 70
        self.root.geometry(f"{self.width}x{self.height}+{pos_x}+{pos_y}")

        # Dragging support
        self._drag_data = {"x": 0, "y": 0}

        # Header Title Bar
        self.header = tk.Frame(self.root, bg="#161e31", height=24)
        self.header.pack(fill="x", side="top")
        self.header.bind("<ButtonPress-1>", self.start_drag)
        self.header.bind("<B1-Motion>", self.do_drag)

        self.lbl_title = tk.Label(
            self.header, text="📷 CAMERA PREVIEW", font=("Segoe UI", 8, "bold"),
            bg="#161e31", fg="#00f2fe"
        )
        self.lbl_title.pack(side="left", padx=8)

        self.btn_min = tk.Label(
            self.header, text="—", font=("Segoe UI", 9, "bold"),
            bg="#161e31", fg="#94a3b8", cursor="hand2"
        )
        self.btn_min.pack(side="right", padx=6)
        self.btn_min.bind("<Button-1>", lambda e: self.toggle_minimize())

        # Video Canvas
        self.canvas = tk.Canvas(
            self.root, width=self.width, height=145,
            bg="#0d111a", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.do_drag)

        # Bottom Distance Advisor Badge
        self.lbl_advisor = tk.Label(
            self.root, text="🟢 Distance: Scanning...", font=("Segoe UI", 8, "bold"),
            bg="#101726", fg="#2ecc71", pady=2
        )
        self.lbl_advisor.pack(fill="x", side="bottom")

        self.is_minimized = False
        self.img_tk = None
        self.is_running = True

        # Start update loop
        self.update_feed()

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
            self.canvas.pack_forget()
            self.lbl_advisor.pack_forget()
            self.root.geometry(f"{self.width}x24")
            self.btn_min.config(text="+")
        else:
            self.canvas.pack(fill="both", expand=True)
            self.lbl_advisor.pack(fill="x", side="bottom")
            self.root.geometry(f"{self.width}x{self.height}")
            self.btn_min.config(text="—")

    def update_feed(self):
        if not self.is_running:
            return

        if not self.is_minimized and os.path.exists(PREVIEW_FILE):
            try:
                # Load latest preview frame
                pil_img = Image.open(PREVIEW_FILE)
                pil_img = pil_img.resize((self.width, 145), Image.Resampling.BILINEAR)
                self.img_tk = ImageTk.PhotoImage(pil_img)
                self.canvas.delete("all")
                self.canvas.create_image(0, 0, anchor="nw", image=self.img_tk)
            except Exception:
                pass

        # Update at ~30 FPS
        self.root.after(33, self.update_feed)

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.is_running = False


if __name__ == "__main__":
    overlay = CameraOverlay()
    overlay.run()
