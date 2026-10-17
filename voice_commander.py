"""
Aura Voice Commander & Nova Voice Pet Engine
Listens via sounddevice and runs speech recognition for hands-free computer control:
- "zoom" / "zoom in" -> Windows Magnifier zoom
- "zoom out" / "unzoom" -> Exit zoom
- "scroll down" / "scroll up" / "top" / "bottom" -> Smooth scrolling
- "click" / "left click" / "right click" / "double click" -> Mouse clicks
- "enter" / "press enter" -> Enter key
- "search <query>" -> Opens Windows search or browser
- "nova" / "hey nova" -> Wakes voice pet
"""

import os
import sys
import time
import queue
import threading
import ctypes
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import speech_recognition as sr
from pynput.keyboard import Controller as KeyController, Key
from pynput.mouse import Controller as MouseController, Button

# Win32 Virtual Keys
VK_LWIN = 0x5B
VK_OEM_PLUS = 0xBB
VK_ESCAPE = 0x1B
MOUSEEVENTF_WHEEL = 0x0800


class VoiceCommander:
    def __init__(self, callback: Optional[Callable[[dict], None]] = None):
        self.callback = callback
        self.is_running = False
        self.is_listening = True
        self.is_zoomed = False
        
        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = 300
        self.recognizer.dynamic_energy_threshold = True
        
        self.keyboard = KeyController()
        self.mouse = MouseController()
        if sys.platform == "win32":
            self.user32 = ctypes.windll.user32
        else:
            self.user32 = None

        self.sample_rate = 16000
        self.audio_queue = queue.Queue()
        self.worker_thread = None

    def trigger_zoom_in(self):
        """Activates Windows Magnifier centered on cursor."""
        if self.user32:
            self.user32.keybd_event(VK_LWIN, 0, 0, 0)
            self.user32.keybd_event(VK_OEM_PLUS, 0, 0, 0)
            time.sleep(0.05)
            self.user32.keybd_event(VK_OEM_PLUS, 0, 2, 0)
            self.user32.keybd_event(VK_LWIN, 0, 2, 0)
            self.is_zoomed = True
            print("[VoiceCommander] Zoom in triggered!", flush=True)

    def trigger_zoom_out(self):
        """Exits Windows Magnifier."""
        if self.user32:
            self.user32.keybd_event(VK_LWIN, 0, 0, 0)
            self.user32.keybd_event(VK_ESCAPE, 0, 0, 0)
            time.sleep(0.05)
            self.user32.keybd_event(VK_ESCAPE, 0, 2, 0)
            self.user32.keybd_event(VK_LWIN, 0, 2, 0)
            self.is_zoomed = False
            print("[VoiceCommander] Zoom out triggered!", flush=True)

    def scroll(self, direction: str, amount: int = 400):
        """Smoothly scroll active window."""
        if self.user32:
            delta = amount if direction == "up" else -amount
            self.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

    def execute_command(self, text: str) -> Optional[str]:
        """Parses speech text and dispatches native OS actions."""
        t = text.lower().strip()
        print(f"[VoiceCommander] Recognized: '{t}'", flush=True)
        response_msg = None

        if "zoom in" in t or t == "zoom" or "magnify" in t:
            self.trigger_zoom_in()
            response_msg = "Zooming in at cursor!"
        elif "zoom out" in t or "unzoom" in t or "exit zoom" in t or "close zoom" in t:
            self.trigger_zoom_out()
            response_msg = "Exited zoom."
        elif "scroll down" in t or t == "down":
            self.scroll("down", 450)
            response_msg = "Scrolling down"
        elif "scroll up" in t or t == "up":
            self.scroll("up", 450)
            response_msg = "Scrolling up"
        elif "go to top" in t or t == "top":
            with self.keyboard.pressed(Key.ctrl):
                self.keyboard.press(Key.home)
                self.keyboard.release(Key.home)
            response_msg = "Top of page"
        elif "go to bottom" in t or t == "bottom":
            with self.keyboard.pressed(Key.ctrl):
                self.keyboard.press(Key.end)
                self.keyboard.release(Key.end)
            response_msg = "Bottom of page"
        elif "right click" in t:
            self.mouse.click(Button.right)
            response_msg = "Right clicked"
        elif "double click" in t:
            self.mouse.click(Button.left, 2)
            response_msg = "Double clicked"
        elif "click" in t or "left click" in t:
            self.mouse.click(Button.left)
            response_msg = "Clicked"
        elif "enter" in t or "press enter" in t:
            self.keyboard.press(Key.enter)
            self.keyboard.release(Key.enter)
            response_msg = "Pressed Enter"
        elif t.startswith("search "):
            query = t.replace("search ", "", 1).strip()
            # Open Windows Start search or browser search
            if self.user32:
                self.user32.keybd_event(VK_LWIN, 0, 0, 0)
                self.user32.keybd_event(ord('S'), 0, 0, 0)
                time.sleep(0.05)
                self.user32.keybd_event(ord('S'), 0, 2, 0)
                self.user32.keybd_event(VK_LWIN, 0, 2, 0)
                time.sleep(0.3)
                self.keyboard.type(query)
                time.sleep(0.1)
                self.keyboard.press(Key.enter)
                self.keyboard.release(Key.enter)
            response_msg = f"Searching for: {query}"
        elif "nova" in t or "hello" in t:
            response_msg = "Hello! I am Nova, your voice companion."

        if self.callback and response_msg:
            self.callback({
                "type": "voice_feedback",
                "heard": t,
                "response": response_msg,
                "timestamp": time.time()
            })

        return response_msg

    def _audio_worker(self):
        """Continuously processes audio chunks for speech recognition."""
        while self.is_running:
            try:
                audio_frames = []
                # Collect ~2.5 seconds of audio chunks
                start_t = time.time()
                while time.time() - start_t < 2.5 and self.is_running:
                    try:
                        chunk = self.audio_queue.get(timeout=0.2)
                        audio_frames.append(chunk)
                    except queue.Empty:
                        pass

                if not audio_frames or not self.is_listening:
                    continue

                audio_data = np.concatenate(audio_frames, axis=0)
                # Compute RMS volume to avoid recognizing silence
                rms = np.sqrt(np.mean(audio_data.astype(np.float32)**2))
                if rms < 450:  # Silence threshold
                    continue

                raw_bytes = audio_data.tobytes()
                sr_audio = sr.AudioData(raw_bytes, self.sample_rate, 2)

                try:
                    text = self.recognizer.recognize_google(sr_audio)
                    if text:
                        self.execute_command(text)
                except sr.UnknownValueError:
                    pass
                except sr.RequestError as e:
                    # Fallback or connection issue
                    pass
            except Exception as e:
                time.sleep(0.1)

    def _sd_callback(self, indata, frames, time_info, status):
        """Sounddevice streaming callback."""
        if not self.is_listening:
            return
        # Convert float32 or int16 to int16 bytes
        chunk_int16 = (indata[:, 0] * 32767).astype(np.int16)
        try:
            self.audio_queue.put_nowait(chunk_int16)
        except queue.Full:
            pass

    def start(self):
        """Starts background sounddevice stream & recognizer worker."""
        if self.is_running:
            return
        self.is_running = True
        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()

        try:
            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=2048,
                callback=self._sd_callback
            )
            self.stream.start()
            print("[VoiceCommander] Background audio stream active. Ready for voice commands!", flush=True)
        except Exception as e:
            print(f"[VoiceCommander] Audio stream warning: {e}", flush=True)

    def stop(self):
        """Stops audio stream."""
        self.is_running = False
        try:
            if hasattr(self, "stream"):
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass


if __name__ == "__main__":
    commander = VoiceCommander()
    commander.start()
    print("Voice Commander running. Say 'zoom', 'scroll down', 'click', or 'hey nova'...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        commander.stop()
