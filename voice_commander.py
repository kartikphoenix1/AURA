"""
AURA Local Voice Assistant (Cross-Platform: macOS & Windows)
Consumes gated audio from AURA's local microphone stream and runs local speech recognition:
- "open <app>" / "launch <app>" -> Opens any macOS app (Safari, Notes, Finder, Spotify, Terminal, etc.)
- "spotlight" / "search <query>" -> Triggers macOS Spotlight Search or Windows Search
- "click" / "left click" / "right click" / "double click" -> Mouse clicks
- "scroll down" / "scroll up" / "top" / "bottom" -> Smooth scrolling
- "zoom" / "zoom in" / "zoom out" -> System Magnifier (macOS Cmd+Opt+8 / Win+Plus)
- "switch app" / "next app" -> Cmd+Tab / Alt+Tab
- "show desktop" -> Cmd+F3 / Win+D
- "close app" / "quit app" -> Cmd+Q / Alt+F4
- "enter" / "press enter" -> Enter key
- "nova" / "hey nova" -> Wakes voice pet
"""

import os
import sys
import time
import base64
import json
import queue
import threading
import subprocess
import re
import math
import urllib.request
import urllib.error
import tempfile
import wave
from pathlib import Path
from typing import Optional, Callable
import numpy as np
import sounddevice as sd
try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
try:
    from piper import PiperVoice
except ImportError:
    PiperVoice = None

from pynput.keyboard import Controller as KeyController, Key
from pynput.mouse import Controller as MouseController, Button

# Optional macOS Native Quartz
if sys.platform == "darwin":
    try:
        import Quartz
        import ApplicationServices
        from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps
        MACOS_NATIVE = True
    except ImportError:
        ApplicationServices = None
        NSWorkspace = None
        NSApplicationActivateIgnoringOtherApps = 0
        MACOS_NATIVE = False
else:
    ApplicationServices = None
    NSWorkspace = None
    NSApplicationActivateIgnoringOtherApps = 0
    MACOS_NATIVE = False

# Optional Win32
if sys.platform == "win32":
    import ctypes
    VK_LWIN = 0x5B
    VK_OEM_PLUS = 0xBB
    VK_ESCAPE = 0x1B
    MOUSEEVENTF_WHEEL = 0x0800
    user32 = ctypes.windll.user32
else:
    user32 = None


class VoiceCommander:
    def __init__(self, callback: Optional[Callable[[dict], None]] = None):
        self.callback = callback
        self.is_running = False
        self.is_listening = False
        self.is_zoomed = False
        self.last_response = "I am listening. Say help for commands."
        self._native_speech_process = None
        self._suppress_audio_until = 0.0

        self.asr_model = None
        self.asr_error = "Local speech model is not configured. See README local voice setup."
        self.llm_error = None
        self.piper_voice = None

        self.keyboard = KeyController()
        self.mouse = MouseController()

        self.target_rate = 16000  # Rate expected by local Whisper
        # Bound queued audio so local inference cannot make commands stale.
        self.audio_queue = queue.Queue(maxsize=160)  # about 6.8s at the 16kHz callback chunk rate
        self.worker_thread = None
        self.control_thread = None

    def _control_worker(self):
        """Receive UI listening state and deliberate spoken-text actions."""
        while self.is_running:
            try:
                from websockets.sync.client import connect
                with connect("ws://127.0.0.1:8765", open_timeout=1.0) as ws:
                    while self.is_running:
                        try:
                            message = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        try:
                            data = json.loads(message)
                        except Exception:
                            continue
                        if data.get("type") == "update_settings" and "voice_listening" in data:
                            requested = bool(data["voice_listening"])
                            ready = self.asr_model is not None
                            error = self.asr_error if not ready else None
                            self.is_listening = requested and ready
                            if not self.is_listening:
                                self._clear_audio_queue()
                            self._send_ws({
                                "type": "voice_status", "listening": self.is_listening,
                                "source": "AURA local audio stream",
                                "error": error if requested else None,
                            })
                            if not self.is_listening:
                                process = self._native_speech_process
                                if process and process.poll() is None:
                                    process.terminate()
                        elif data.get("type") == "voice_audio_chunk":
                            if not self.is_listening or time.monotonic() < self._suppress_audio_until:
                                continue
                            try:
                                pcm = base64.b64decode(data.get("pcm", ""), validate=True)
                                chunk = np.frombuffer(pcm, dtype="<i2").copy()
                            except (ValueError, TypeError):
                                continue
                            try:
                                self.audio_queue.put_nowait(chunk)
                            except queue.Full:
                                try:
                                    self.audio_queue.get_nowait()
                                    self.audio_queue.put_nowait(chunk)
                                except (queue.Empty, queue.Full):
                                    pass
                        elif data.get("type") == "speech_text":
                            text = str(data.get("text", "")).strip()
                            if text:
                                self.execute_command(text)
            except Exception:
                time.sleep(0.5)

    def _send_ws(self, msg: dict):
        def _bg():
            try:
                from websockets.sync.client import connect
                with connect("ws://127.0.0.1:8765", open_timeout=0.6) as ws:
                    ws.send(json.dumps(msg))
            except Exception:
                pass
        threading.Thread(target=_bg, daemon=True).start()

    def _speak_native(self, text: str):
        """Speak locally with Piper when configured, with macOS speech as a fallback."""
        if sys.platform != "darwin":
            return False
        try:
            piper_path = os.environ.get("AURA_PIPER_VOICE", "").strip()
            if PiperVoice is not None and piper_path and Path(piper_path).is_file():
                if self.piper_voice is None:
                    self.piper_voice = PiperVoice.load(piper_path)
                self._suppress_audio_until = time.monotonic() + min(10.0, max(1.5, len(text) / 10.0))
                while True:
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        break
                with tempfile.TemporaryDirectory(prefix="aura-voice-") as temp_dir:
                    wav_path = os.path.join(temp_dir, "reply.wav")
                    with wave.open(wav_path, "wb") as wav_file:
                        self.piper_voice.synthesize_wav(text, wav_file)
                    with wave.open(wav_path, "rb") as wav_file:
                        samples = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2").copy()
                        rate = wav_file.getframerate()
                    sd.play(samples, rate)
                    sd.wait()
                return True
            process = getattr(self, "_native_speech_process", None)
            if process and process.poll() is None:
                process.terminate()
            self._native_speech_process = subprocess.Popen(
                ["/usr/bin/say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            # Do not recognize the pet's own voice as a fresh user command.
            self._suppress_audio_until = time.monotonic() + min(8.0, max(1.2, len(text) / 13.0))
            audio_queue = getattr(self, "audio_queue", None)
            if audio_queue is not None:
                while True:
                    try:
                        audio_queue.get_nowait()
                    except queue.Empty:
                        break
            return True
        except Exception as exc:
            print(f"[VoiceCommander] Native speech unavailable: {exc}", flush=True)
            return False

    def _publish_response(self, heard: str, response: str):
        self.last_response = response
        native_speech = self._speak_native(response)
        payload = {
            "type": "voice_feedback",
            "heard": heard,
            "response": response,
            "native_speech": native_speech,
            "timestamp": time.time(),
        }
        self._send_ws(payload)
        if self.callback:
            self.callback(payload)
        return response

    @staticmethod
    def _window_target(text: str, verb: str):
        """Return an explicit app target from phrases such as 'minimize Chrome app'."""
        match = re.match(rf"^(?:please\s+)?(?:{verb})\s+(?:the\s+)?(.+?)\s*$", text)
        if not match:
            return None
        target = re.sub(r"\s+(?:app|window)$", "", match.group(1)).strip()
        if target in ("", "window", "tab", "this window", "current window", "current app", "it", "everything"):
            return None
        return target

    def _activate_app(self, app_name: str):
        """Bring a named application forward before sending its window shortcut."""
        aliases = {
            "chrome": "Google Chrome", "google chrome": "Google Chrome",
            "browser": "Safari", "safari": "Safari", "finder": "Finder",
            "code": "Visual Studio Code", "vscode": "Visual Studio Code",
            "spotify": "Spotify", "terminal": "Terminal", "notes": "Notes",
            "mail": "Mail", "music": "Music", "messages": "Messages",
        }
        target = aliases.get(app_name.lower(), app_name)
        if sys.platform == "darwin":
            try:
                subprocess.run(["open", "-a", target], check=True, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.6)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"[VoiceCommander] Could not activate {target}: {exc}", flush=True)
                return False, target
            return True, target
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "$s=New-Object -ComObject WScript.Shell; if(-not $s.AppActivate($args[0])){exit 1}", target],
                    check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                time.sleep(0.15)
                return True, target
            except (OSError, subprocess.SubprocessError):
                return False, target
        return False, target

    def _macos_window_action(self, app_name: str, action: str):
        """Use macOS Accessibility to operate a named app or titled window."""
        aliases = {
            "chrome": "Google Chrome", "google chrome": "Google Chrome",
            "safari": "Safari", "finder": "Finder",
            "code": "Visual Studio Code", "vscode": "Visual Studio Code",
            "spotify": "Spotify", "terminal": "Terminal", "notes": "Notes",
            "mail": "Mail", "music": "Music", "messages": "Messages",
        }
        target = aliases.get(app_name.strip().lower(), app_name.strip())
        if not MACOS_NATIVE or not ApplicationServices or not NSWorkspace:
            return False, target
        try:
            if not ApplicationServices.AXIsProcessTrusted():
                return False, target

            candidates = []
            for running_app in NSWorkspace.sharedWorkspace().runningApplications():
                if running_app.isTerminated():
                    continue
                app_element = ApplicationServices.AXUIElementCreateApplication(
                    running_app.processIdentifier()
                )
                error, windows = ApplicationServices.AXUIElementCopyAttributeValue(
                    app_element, ApplicationServices.kAXWindowsAttribute, None
                )
                if error != 0 or not windows:
                    continue
                app_match = (running_app.localizedName() or "").casefold() == target.casefold()
                for window in windows:
                    title_error, title = ApplicationServices.AXUIElementCopyAttributeValue(
                        window, ApplicationServices.kAXTitleAttribute, None
                    )
                    title_match = title_error == 0 and target.casefold() in str(title or "").casefold()
                    if app_match or title_match:
                        candidates.append((running_app, app_element, window, app_match))
            if not candidates:
                return False, target
            # Prefer the app's focused window when the app name matched.
            running_app, app_element, window, app_matched = candidates[0]
            focused_error, focused_window = ApplicationServices.AXUIElementCopyAttributeValue(
                app_element, ApplicationServices.kAXFocusedWindowAttribute, None
            )
            if app_matched and focused_error == 0 and focused_window is not None:
                window = focused_window
            # A minimized AURA window must be restored before maximizing it.
            running_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            minimized_error, is_minimized = ApplicationServices.AXUIElementCopyAttributeValue(
                window, ApplicationServices.kAXMinimizedAttribute, None
            )
            if action == "maximize" and minimized_error == 0 and is_minimized:
                ApplicationServices.AXUIElementSetAttributeValue(
                    window, ApplicationServices.kAXMinimizedAttribute, False
                )
                time.sleep(0.2)

            attribute = {
                "minimize": ApplicationServices.kAXMinimizeButtonAttribute,
                "maximize": ApplicationServices.kAXZoomButtonAttribute,
                "close": ApplicationServices.kAXCloseButtonAttribute,
            }.get(action)
            if not attribute:
                return False, target
            button_error, button = ApplicationServices.AXUIElementCopyAttributeValue(
                window, attribute, None
            )
            if button_error == 0 and button is not None and ApplicationServices.AXUIElementPerformAction(
                button, ApplicationServices.kAXPressAction
            ) == 0:
                return True, target
            if action == "minimize":
                return ApplicationServices.AXUIElementSetAttributeValue(
                    window, ApplicationServices.kAXMinimizedAttribute, True
                ) == 0, target
            return False, target
        except Exception as exc:
            print(f"[VoiceCommander] macOS {action} failed for {target}: {exc}", flush=True)
            return False, target

    def _macos_focused_window_action(self, action: str):
        """Operate the foreground window directly; keyboard focus may be in the web view."""
        if not MACOS_NATIVE or not ApplicationServices or not ApplicationServices.AXIsProcessTrusted():
            return False
        try:
            system = ApplicationServices.AXUIElementCreateSystemWide()
            error, app_element = ApplicationServices.AXUIElementCopyAttributeValue(
                system, ApplicationServices.kAXFocusedApplicationAttribute, None
            )
            if error != 0 or app_element is None:
                return False
            error, window = ApplicationServices.AXUIElementCopyAttributeValue(
                app_element, ApplicationServices.kAXFocusedWindowAttribute, None
            )
            if error != 0 or window is None:
                return False
            if action == "maximize":
                min_error, minimized = ApplicationServices.AXUIElementCopyAttributeValue(
                    window, ApplicationServices.kAXMinimizedAttribute, None
                )
                if min_error == 0 and minimized:
                    ApplicationServices.AXUIElementSetAttributeValue(
                        window, ApplicationServices.kAXMinimizedAttribute, False
                    )
            attribute = (ApplicationServices.kAXMinimizeButtonAttribute if action == "minimize"
                         else ApplicationServices.kAXZoomButtonAttribute)
            button_error, button = ApplicationServices.AXUIElementCopyAttributeValue(
                window, attribute, None
            )
            if button_error == 0 and button is not None:
                return ApplicationServices.AXUIElementPerformAction(
                    button, ApplicationServices.kAXPressAction
                ) == 0
            # Some apps omit the button element; set the native minimized flag directly.
            if action == "minimize":
                return ApplicationServices.AXUIElementSetAttributeValue(
                    window, ApplicationServices.kAXMinimizedAttribute, True
                ) == 0
            return False
        except Exception as exc:
            print(f"[VoiceCommander] macOS focused {action} failed: {exc}", flush=True)
            return False

    def _macos_minimize_focused_app_windows(self):
        """Minimize every exposed window belonging to the currently focused app."""
        if not MACOS_NATIVE or not ApplicationServices:
            return 0
        try:
            if not ApplicationServices.AXIsProcessTrusted():
                return 0
            system = ApplicationServices.AXUIElementCreateSystemWide()
            error, app_element = ApplicationServices.AXUIElementCopyAttributeValue(
                system, ApplicationServices.kAXFocusedApplicationAttribute, None
            )
            if error != 0 or app_element is None:
                return 0
            error, windows = ApplicationServices.AXUIElementCopyAttributeValue(
                app_element, ApplicationServices.kAXWindowsAttribute, None
            )
            if error != 0 or not windows:
                return 0
            minimized = 0
            for window in windows:
                button_error, button = ApplicationServices.AXUIElementCopyAttributeValue(
                    window, ApplicationServices.kAXMinimizeButtonAttribute, None
                )
                if button_error == 0 and button is not None and ApplicationServices.AXUIElementPerformAction(
                    button, ApplicationServices.kAXPressAction
                ) == 0:
                    minimized += 1
            return minimized
        except Exception as exc:
            print(f"[VoiceCommander] Minimize all failed: {exc}", flush=True)
            return 0

    def _window_action(self, app_name: str, action: str):
        """Activate a named app, then apply a window operation to that app."""
        if sys.platform == "darwin":
            return self._macos_window_action(app_name, action)
        activated, target = self._activate_app(app_name)
        if not activated:
            return False, target
        if action == "minimize":
            if sys.platform == "darwin":
                # Target the activated app with its native window shortcut. This
                # avoids System Events automation prompts and works for apps that
                # do not expose scriptable window controls.
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press('m')
                    self.keyboard.release('m')
            elif user32:
                user32.ShowWindow(user32.GetForegroundWindow(), 6)
        elif action == "maximize":
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd, Key.ctrl):
                    self.keyboard.press("f"); self.keyboard.release("f")
            elif user32:
                user32.ShowWindow(user32.GetForegroundWindow(), 3)
        elif action == "close":
            mod_key = Key.cmd if sys.platform == "darwin" else Key.alt
            close_key = "q" if sys.platform == "darwin" else Key.f4
            with self.keyboard.pressed(mod_key):
                self.keyboard.press(close_key); self.keyboard.release(close_key)
        return True, target

    def execute_tool(self, name: str, args: dict) -> dict:
        """Execute only the small, explicit set of desktop tools exposed to Flipping."""
        if not isinstance(args, dict):
            return {"ok": False, "message": "Invalid action arguments."}
        app = str(args.get("app", "")).strip()[:100]

        if name in ("minimize_app", "maximize_app"):
            action = "minimize" if name == "minimize_app" else "maximize"
            if not app or app.casefold() in ("current", "active", "this window", "current window"):
                if sys.platform == "darwin":
                    ok = self._macos_focused_window_action(action)
                elif user32:
                    hwnd = user32.GetForegroundWindow()
                    ok = bool(user32.ShowWindow(hwnd, 6 if action == "minimize" else 3))
                else:
                    ok = False
                verb = "minimized" if action == "minimize" else "maximized"
                return {"ok": ok, "message": f"I {verb} the active window." if ok else f"I couldn't {action} the active window."}
            ok, target = self._window_action(app, action)
            if ok:
                verb = "minimized" if action == "minimize" else "maximized"
                return {"ok": True, "message": f"I {verb} {target}."}
            return {"ok": False, "message": f"I couldn't find or control {target}. Check its name and AURA's Accessibility permission."}

        if name in ("open_app", "switch_to_app"):
            if not app:
                return {"ok": False, "message": "The app name was missing."}
            if name == "open_app":
                if sys.platform == "darwin":
                    aliases = {"chrome": "Google Chrome", "google chrome": "Google Chrome"}
                    target = aliases.get(app.lower(), app)
                    try:
                        subprocess.run(["open", "-a", target], check=True, timeout=8,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except (OSError, subprocess.SubprocessError):
                        return {"ok": False, "message": f"I couldn't open {target}."}
                    return {"ok": True, "message": f"I opened {target}."}
                result = self.open_app(app)
                return {"ok": not result.lower().startswith("could not"), "message": result}
            ok, target = self._activate_app(app)
            return {"ok": ok, "message": f"I switched to {target}." if ok else f"I couldn't find {target} open."}

        if name == "scroll":
            direction = str(args.get("direction", ""))
            if direction not in ("up", "down", "left", "right"):
                return {"ok": False, "message": "That scroll direction is not supported."}
            amount = max(1, min(5, int(args.get("amount", 1))))
            for _ in range(amount):
                if not self.scroll(direction, 450):
                    return {"ok": False, "message": "I couldn't scroll. Check AURA's Accessibility permission."}
                time.sleep(0.035)
            return {"ok": True, "message": f"Scrolled {direction}."}

        if name == "click":
            button = str(args.get("button", "left"))
            if button not in ("left", "right", "double"):
                return {"ok": False, "message": "That click type is not supported."}
            target_button = Button.right if button == "right" else Button.left
            self.mouse.click(target_button, 2 if button == "double" else 1)
            return {"ok": True, "message": f"{button.capitalize()} click sent at the current pointer."}

        if name == "search":
            query = str(args.get("query", "")).strip()[:240]
            if not query:
                return {"ok": False, "message": "I didn't hear what you want to search for."}
            self.trigger_spotlight(query)
            return {"ok": True, "message": f"I searched for {query}."}

        if name == "zoom":
            direction = str(args.get("direction", ""))
            if direction == "in":
                self.trigger_zoom_in()
            elif direction == "out":
                self.trigger_zoom_out()
            else:
                return {"ok": False, "message": "Choose zoom in or zoom out."}
            return {"ok": True, "message": f"Zoomed {direction}."}

        return {"ok": False, "message": "That action is not available."}

    def _local_assistant_fallback(self, heard: str) -> str:
        """Ask a local Ollama model to interpret an otherwise-unmatched utterance.

        The model can only return an allow-listed action request or a short chat
        reply. Desktop actions still pass through execute_tool and its validation.
        """
        model = os.environ.get("AURA_LOCAL_MODEL", "qwen3.5:4b").strip()
        if not model or "cloud" in model.lower():
            return "The configured assistant model is not a local model. Set AURA_LOCAL_MODEL to a model installed in Ollama."
        schema = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["action", "chat", "clarify"]},
                "name": {"type": "string", "enum": ["minimize_app", "maximize_app", "open_app", "switch_to_app", "scroll", "click", "search", "zoom", "none"]},
                "arguments": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "direction": {"type": "string", "enum": ["up", "down", "left", "right", "in", "out"]},
                        "amount": {"type": "integer", "minimum": 1, "maximum": 5},
                        "button": {"type": "string", "enum": ["left", "right", "double"]},
                        "query": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "reply": {"type": "string"},
            },
            "required": ["kind", "name", "arguments", "reply"],
        }
        prompt = (
            "You are Flipping, a concise, friendly desktop accessibility assistant. "
            "Interpret the user's request. For a clear supported desktop task, return kind=action "
            "and exactly one allowed action. Never invent that an action has already succeeded. "
            "Use clarify if an app or request is ambiguous. For ordinary conversation, return kind=chat. "
            "Supported actions: minimize_app/maximize_app/open_app/switch_to_app with {app}; "
            "scroll with {direction: up|down|left|right, amount: 1}; click with {button: left|right|double}; "
            "search with {query}; zoom with {direction: in|out}. Never return any other action."
        )
        payload = json.dumps({
            "model": model,
            "stream": False,
            "format": schema,
            "think": False,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": heard[:500]},
            ],
            "options": {"temperature": 0.1, "num_predict": 160, "num_ctx": 4096},
        }).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            message = json.loads(result.get("message", {}).get("content", "{}"))
            kind = message.get("kind")
            name = message.get("name")
            args = message.get("arguments", {})
            if kind == "action" and name in {
                "minimize_app", "maximize_app", "open_app", "switch_to_app",
                "scroll", "click", "search", "zoom",
            } and isinstance(args, dict):
                return self.execute_tool(name, args).get("message", "I couldn't complete that action.")
            if kind == "clarify":
                return str(message.get("reply") or "Which app or action do you mean?")[:300]
            if kind == "chat":
                return str(message.get("reply") or "I’m here. What would you like me to do?")[:500]
            return "I’m not sure what you meant. Try saying help for examples."
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            self.llm_error = str(exc)
            print("[VoiceCommander] Local Ollama assistant unavailable: " + str(exc), flush=True)
            return "I heard you, but the local assistant model is unavailable. Check that Ollama is running with the configured model."

    def start_action_bridge(self):
        """Start the local desktop action and voice control bridge."""
        if self.is_running:
            return
        self.is_running = True
        self.control_thread = threading.Thread(target=self._control_worker, daemon=True)
        self.control_thread.start()

    def trigger_zoom_in(self):
        """Activates System Magnifier centered on cursor."""
        if sys.platform == "darwin" and MACOS_NATIVE:
            # macOS Accessibility Zoom toggle (Cmd + Option + 8)
            k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x1C, True)
            Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
            time.sleep(0.02)
            k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x1C, False)
            Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
            self.is_zoomed = True
            print("[VoiceCommander] macOS Zoom in dispatched!", flush=True)
        elif user32:
            user32.keybd_event(VK_LWIN, 0, 0, 0)
            user32.keybd_event(VK_OEM_PLUS, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_OEM_PLUS, 0, 2, 0)
            user32.keybd_event(VK_LWIN, 0, 2, 0)
            self.is_zoomed = True
            print("[VoiceCommander] Windows Zoom in triggered!", flush=True)

    def trigger_zoom_out(self):
        """Exits System Magnifier."""
        if sys.platform == "darwin" and MACOS_NATIVE:
            k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x1C, True)
            Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
            time.sleep(0.02)
            k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x1C, False)
            Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
            self.is_zoomed = False
            print("[VoiceCommander] macOS Zoom out dispatched!", flush=True)
        elif user32:
            user32.keybd_event(VK_LWIN, 0, 0, 0)
            user32.keybd_event(VK_ESCAPE, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_ESCAPE, 0, 2, 0)
            user32.keybd_event(VK_LWIN, 0, 2, 0)
            self.is_zoomed = False
            print("[VoiceCommander] Windows Zoom out triggered!", flush=True)

    def scroll(self, direction: str, amount: int = 400):
        """Scroll the focused window and report whether an input event was sent."""
        if sys.platform == "darwin" and MACOS_NATIVE:
            try:
                if not ApplicationServices.AXIsProcessTrusted():
                    return False
                vertical = int(amount if direction == "up" else -amount) if direction in ("up", "down") else 0
                horizontal = int(amount if direction == "right" else -amount) if direction in ("left", "right") else 0
                if hasattr(Quartz, "CGEventCreateScrollWheelEvent2"):
                    wheel_count = 1 if vertical else 2
                    if horizontal:
                        event = Quartz.CGEventCreateScrollWheelEvent2(
                            None, Quartz.kCGScrollEventUnitPixel, wheel_count,
                            vertical, horizontal, 0, 0,
                        )
                    else:
                        event = Quartz.CGEventCreateScrollWheelEvent2(
                            None, Quartz.kCGScrollEventUnitPixel, wheel_count,
                            vertical, 0, 0,
                        )
                else:
                    event = Quartz.CGEventCreateScrollWheelEvent(
                        None, Quartz.kCGScrollEventUnitLine, 2,
                        int(vertical / 120), int(horizontal / 120),
                    )
                if not event:
                    return False
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
                return True
            except Exception as exc:
                print(f"[VoiceCommander] Scroll failed: {exc}", flush=True)
                return False
        elif user32:
            if direction in ("left", "right"):
                delta = 120 if direction == "right" else -120
                user32.mouse_event(0x01000, 0, 0, delta, 0)  # MOUSEEVENTF_HWHEEL
            else:
                delta = amount if direction == "up" else -amount
                user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            return True
        else:
            self.mouse.scroll(4 if direction == "right" else -4 if direction == "left" else 0,
                              4 if direction == "up" else -4 if direction == "down" else 0)
            return True

    def open_app(self, app_name: str) -> str:
        """Launches an application by name on macOS or Windows."""
        app_clean = app_name.strip()
        print(f"[VoiceCommander] Opening app: '{app_clean}'", flush=True)
        if sys.platform == "darwin":
            # Map common nicknames to official macOS app names
            app_map = {
                "chrome": "Google Chrome",
                "brave": "Brave Browser",
                "settings": "System Settings",
                "preferences": "System Settings",
                "system preferences": "System Settings",
                "music": "Music",
                "spotify": "Spotify",
                "mail": "Mail",
                "messages": "Messages",
                "imessage": "Messages",
                "notes": "Notes",
                "finder": "Finder",
                "files": "Finder",
                "safari": "Safari",
                "browser": "Safari",
                "terminal": "Terminal",
                "photos": "Photos",
                "calendar": "Calendar",
                "calculator": "Calculator",
                "app store": "App Store",
                "code": "Visual Studio Code",
                "vscode": "Visual Studio Code",
            }
            target_app = app_map.get(app_clean.lower(), app_clean.title())
            try:
                subprocess.Popen(["open", "-a", target_app])
                return f"Opening {target_app}"
            except Exception:
                try:
                    subprocess.Popen(["open", "-a", app_clean])
                    return f"Opening {app_clean}"
                except Exception as e:
                    return f"Could not open {app_clean}: {e}"
        else:
            try:
                os.startfile(app_clean)
                return f"Opening {app_clean}"
            except Exception:
                subprocess.Popen(["cmd", "/c", "start", app_clean], shell=True)
                return f"Opening {app_clean}"

    def trigger_spotlight(self, query: str = ""):
        """Opens macOS Spotlight Search and optionally types query."""
        if sys.platform == "darwin":
            if MACOS_NATIVE:
                k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x31, True)
                Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
                time.sleep(0.02)
                k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x31, False)
                Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
            else:
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press(Key.space)
                    self.keyboard.release(Key.space)

            if query:
                time.sleep(0.35)
                self.keyboard.type(query)
                time.sleep(0.1)
                self.keyboard.press(Key.enter)
                self.keyboard.release(Key.enter)
        elif user32:
            user32.keybd_event(VK_LWIN, 0, 0, 0)
            user32.keybd_event(ord('S'), 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(ord('S'), 0, 2, 0)
            user32.keybd_event(VK_LWIN, 0, 2, 0)
            if query:
                time.sleep(0.3)
                self.keyboard.type(query)
                time.sleep(0.1)
                self.keyboard.press(Key.enter)
                self.keyboard.release(Key.enter)

    def execute_command(self, text: str) -> Optional[str]:
        """Parses speech text and dispatches native OS actions."""
        t = re.sub(r"[\s.,!?;:]+$", "", text.lower()).strip()
        print(f"[VoiceCommander] Recognized: '{t}'", flush=True)
        response_msg = None

        if t in ("help", "what can you do", "commands", "list commands"):
            response_msg = ("I can open, switch, minimize, maximize, and close apps; click, scroll, zoom, search, "
                "and control tabs or text. Just tell me what you want done.")
            return self._publish_response(t, response_msg)
        elif t in ("status", "are you listening", "what is the status"):
            response_msg = "I am listening through the system microphone." if self.is_listening else "The system microphone is paused while voice chat uses the microphone."
            return self._publish_response(t, response_msg)
        elif t in ("repeat", "say that again"):
            response_msg = self.last_response
            return self._publish_response(t, response_msg)
        # 1. Voice rest and wake commands
        if t in ("show aura", "restore aura", "bring aura back", "show flipping"):
            success, target = self._window_action("aura", "maximize") if sys.platform == "darwin" else self._activate_app("AURA")
            response_msg = "I brought AURA back." if success else "I couldn't restore the AURA window."
        elif t in ("snooze", "go to sleep", "pause gaze", "shhh", "shush"):
            self._send_ws({"type": "update_settings", "gaze_enabled": False})
            response_msg = "Snoozing. Say wake up or press the spacebar to resume."
        elif t in ("wake up", "resume gaze", "resume"):
            self._send_ws({"type": "update_settings", "gaze_enabled": True})
            response_msg = "I am awake. Gaze control is back on."

        # 2. App Launching commands
        elif t.startswith("open ") or t.startswith("launch "):
            app = t.replace("open ", "", 1).replace("launch ", "", 1).strip()
            response_msg = self.open_app(app)

        # 2. Spotlight & Search
        elif t == "spotlight" or t == "open spotlight":
            self.trigger_spotlight()
            response_msg = "Opened Spotlight Search"
        elif t.startswith("search ") or t.startswith("find "):
            query = t.replace("search for ", "", 1).replace("search ", "", 1).replace("find ", "", 1).strip()
            self.trigger_spotlight(query)
            response_msg = f"Searching for: {query}"

        # 3. Zoom
        elif "zoom in" in t or t == "zoom" or "magnify" in t:
            self.trigger_zoom_in()
            response_msg = "Zooming in at cursor!"
        elif "zoom out" in t or "unzoom" in t or "exit zoom" in t or "close zoom" in t:
            self.trigger_zoom_out()
            response_msg = "Exited zoom."

        # 4. Scroll
        elif "scroll left" in t or "swipe left" in t:
            response_msg = "Scrolling left." if self.scroll("left", 450) else "I couldn't scroll. Check AURA's Accessibility permission."
        elif "scroll right" in t or "swipe right" in t:
            response_msg = "Scrolling right." if self.scroll("right", 450) else "I couldn't scroll. Check AURA's Accessibility permission."
        elif "scroll down" in t or t == "down":
            response_msg = "Scrolling down." if self.scroll("down", 450) else "I couldn't scroll. Check AURA's Accessibility permission."
        elif "scroll up" in t or t == "up":
            response_msg = "Scrolling up." if self.scroll("up", 450) else "I couldn't scroll. Check AURA's Accessibility permission."
        elif "go to top" in t or t == "top":
            mod_key = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod_key):
                self.keyboard.press(Key.up if sys.platform == "darwin" else Key.home)
                self.keyboard.release(Key.up if sys.platform == "darwin" else Key.home)
            response_msg = "Top of page"
        elif "page down" in t:
            self.keyboard.press(Key.page_down); self.keyboard.release(Key.page_down)
            response_msg = "Page down"
        elif "page up" in t:
            self.keyboard.press(Key.page_up); self.keyboard.release(Key.page_up)
            response_msg = "Page up"
        elif "go to bottom" in t or t == "bottom":
            mod_key = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod_key):
                self.keyboard.press(Key.down if sys.platform == "darwin" else Key.end)
                self.keyboard.release(Key.down if sys.platform == "darwin" else Key.end)
            response_msg = "Bottom of page"

        # 5. Clicks (dispatched at gaze cursor via WebSocket)
        elif "right click" in t:
            self._send_ws({"type": "voice_command", "action": "right_click"})
            response_msg = "Right clicked"
        elif "double click" in t:
            self._send_ws({"type": "voice_command", "action": "double_click"})
            response_msg = "Double clicked"
        elif ("click" in t or "select" in t) and "select all" not in t:
            self._send_ws({"type": "voice_command", "action": "left_click"})
            response_msg = "Selected"

        # 6. Keys & Window Management
        elif "enter" in t or "press enter" in t:
            self.keyboard.press(Key.enter)
            self.keyboard.release(Key.enter)
            response_msg = "Pressed Enter"
        elif "escape" in t or "press escape" in t or "cancel" in t:
            self.keyboard.press(Key.esc)
            self.keyboard.release(Key.esc)
            response_msg = "Pressed Escape"
        elif "switch app" in t or "next app" in t:
            mod_key = Key.cmd if sys.platform == "darwin" else Key.alt
            with self.keyboard.pressed(mod_key):
                self.keyboard.press(Key.tab)
                self.keyboard.release(Key.tab)
            response_msg = "Switched app"
        elif t.startswith("switch to "):
            app_name = re.sub(r"\s+(?:app|window)$", "", t.removeprefix("switch to ")).strip()
            success, target = self._activate_app(app_name)
            response_msg = f"Switched to {target}." if success else f"I couldn't find {target} open."
        elif "switch window" in t or "next window" in t or "previous window" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press("`"); self.keyboard.release("`")
            elif user32:
                with self.keyboard.pressed(Key.alt):
                    self.keyboard.press(Key.tab); self.keyboard.release(Key.tab)
            response_msg = "Switched window"
        elif "new window" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press("n"); self.keyboard.release("n")
            response_msg = "Opened a new window"
        elif "show desktop" in t or "desktop" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press(Key.f3)
                    self.keyboard.release(Key.f3)
            elif user32:
                user32.keybd_event(VK_LWIN, 0, 0, 0)
                user32.keybd_event(ord('D'), 0, 0, 0)
                user32.keybd_event(ord('D'), 0, 2, 0)
                user32.keybd_event(VK_LWIN, 0, 2, 0)
            response_msg = "Show Desktop"
        elif ("minimize" in t or "minimise" in t) and any(
            phrase in t for phrase in ("all windows", "all apps", "everything")
        ):
            if sys.platform == "darwin":
                minimized = self._macos_minimize_focused_app_windows()
                response_msg = (f"Minimized {minimized} windows in the active app." if minimized else
                                "I couldn't minimize those windows. Check AURA's Accessibility permission.")
            elif user32:
                user32.keybd_event(VK_LWIN, 0, 0, 0)
                user32.keybd_event(ord('M'), 0, 0, 0)
                user32.keybd_event(ord('M'), 0, 2, 0)
                user32.keybd_event(VK_LWIN, 0, 2, 0)
                response_msg = "Minimized open windows."
        elif ("minimize" in t or "minimise" in t) and self._window_target(t, r"minimi[sz]e"):
            app_name = self._window_target(t, r"minimi[sz]e")
            success, target = self._window_action(app_name, "minimize")
            if success:
                response_msg = f"Done. I minimized {target}."
            else:
                response_msg = f"I couldn't minimize {target}. Check AURA's Accessibility permission and try again."
        elif "minimize" in t or "minimise" in t:
            if sys.platform == "darwin":
                success = self._macos_focused_window_action("minimize")
                response_msg = "Minimized the active window." if success else "I couldn't minimize the active window. Check AURA's Accessibility permission."
            elif user32:
                success = bool(user32.ShowWindow(user32.GetForegroundWindow(), 6))  # SW_MINIMIZE
                response_msg = "Minimized the active window." if success else "I couldn't minimize the active window."
        elif ("maximize" in t or "maximise" in t) and self._window_target(t, r"maximi[sz]e"):
            app_name = self._window_target(t, r"maximi[sz]e")
            success, target = self._window_action(app_name, "maximize")
            response_msg = f"Done. I maximized {target}." if success else f"I couldn't maximize {target}. Check that it is open and AURA has Accessibility permission."
        elif "maximize" in t or "maximise" in t or "full screen" in t or "fullscreen" in t:
            if sys.platform == "darwin":
                success = self._macos_focused_window_action("maximize")
                response_msg = "Maximized the active window." if success else "I couldn't maximize the active window. Check AURA's Accessibility permission."
            elif user32:
                success = bool(user32.ShowWindow(user32.GetForegroundWindow(), 3))  # SW_MAXIMIZE
                response_msg = "Maximized the active window." if success else "I couldn't maximize the active window."
        elif (self._window_target(t, r"close|quit") is not None):
            app_name = self._window_target(t, r"close|quit")
            success, target = self._window_action(app_name, "close")
            response_msg = f"Closed {target}." if success else f"I couldn't close {target}. Check AURA's Accessibility permission and try again."
        elif "close tab" in t or "close window" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press('w')
                    self.keyboard.release('w')
            response_msg = "Closed tab"
        elif "close app" in t or "quit app" in t or "quit" in t:
            mod_key = Key.cmd if sys.platform == "darwin" else Key.alt
            close_key = 'q' if sys.platform == "darwin" else Key.f4
            with self.keyboard.pressed(mod_key):
                self.keyboard.press(close_key)
                self.keyboard.release(close_key)
            response_msg = "Closed app"
        elif "next tab" in t or "tab right" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd, Key.shift):
                    self.keyboard.press(']')
                    self.keyboard.release(']')
            response_msg = "Next tab"
        elif "previous tab" in t or "prev tab" in t or "tab left" in t or "last tab" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd, Key.shift):
                    self.keyboard.press('[')
                    self.keyboard.release('[')
            response_msg = "Previous tab"
        elif "new tab" in t or "open tab" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press('t')
                    self.keyboard.release('t')
            response_msg = "Opened new tab"
        elif "slide left" in t or "swipe left" in t or "previous slide" in t:
            self.keyboard.press(Key.left)
            self.keyboard.release(Key.left)
            response_msg = "Slide left"
        elif "slide right" in t or "swipe right" in t or "next slide" in t:
            self.keyboard.press(Key.right)
            self.keyboard.release(Key.right)
            response_msg = "Slide right"

        # 7. Edit commands
        elif "copy" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('c'); self.keyboard.release('c')
            response_msg = "Copied"
        elif "paste" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('v'); self.keyboard.release('v')
            response_msg = "Pasted"
        elif "cut" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('x'); self.keyboard.release('x')
            response_msg = "Cut"
        elif "undo" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('z'); self.keyboard.release('z')
            response_msg = "Undo"
        elif "redo" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd, Key.shift):
                    self.keyboard.press('z'); self.keyboard.release('z')
            else:
                with self.keyboard.pressed(Key.ctrl, Key.shift):
                    self.keyboard.press('z'); self.keyboard.release('z')
            response_msg = "Redo"
        elif "select all" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('a'); self.keyboard.release('a')
            response_msg = "Selected all"
        elif "save" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('s'); self.keyboard.release('s')
            response_msg = "Saved"
        elif "screenshot" in t or "screen shot" in t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd, Key.shift):
                    self.keyboard.press('3'); self.keyboard.release('3')
            response_msg = "Screenshot taken"
        elif "go back" in t or "back" == t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press('['); self.keyboard.release('[')
            response_msg = "Going back"
        elif "go forward" in t or "forward" == t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd):
                    self.keyboard.press(']'); self.keyboard.release(']')
            response_msg = "Going forward"
        elif "refresh" in t or "reload" in t:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            with self.keyboard.pressed(mod):
                self.keyboard.press('r'); self.keyboard.release('r')
            response_msg = "Refreshed"
        elif "volume up" in t or "louder" in t:
            self.keyboard.press(Key.media_volume_up)
            self.keyboard.release(Key.media_volume_up)
            response_msg = "Volume up"
        elif "volume down" in t or "quieter" in t:
            self.keyboard.press(Key.media_volume_down)
            self.keyboard.release(Key.media_volume_down)
            response_msg = "Volume down"
        elif "mute" in t:
            self.keyboard.press(Key.media_volume_mute)
            self.keyboard.release(Key.media_volume_mute)
            response_msg = "Muted"
        elif "play" in t or "pause" in t:
            self.keyboard.press(Key.media_play_pause)
            self.keyboard.release(Key.media_play_pause)
            response_msg = "Play / Pause"
        elif "lock screen" in t or "lock" == t:
            if sys.platform == "darwin":
                with self.keyboard.pressed(Key.cmd, Key.ctrl):
                    self.keyboard.press('q'); self.keyboard.release('q')
            response_msg = "Screen locked"
        elif "mission control" in t:
            if sys.platform == "darwin":
                self.keyboard.press(Key.f3); self.keyboard.release(Key.f3)
            response_msg = "Mission Control"

        # 8. Nova Pet greetings
        elif any(word in t for word in ("nova", "hello", "hey aura", "hi aura", "hey flipping", "hi flipping")):
            response_msg = "Hello! I am Flipping, your AURA pet. I am here and listening. Say help for voice commands."

        if not response_msg:
            response_msg = self._local_assistant_fallback(t)

        return self._publish_response(t, response_msg)

    def _audio_worker(self):
        """Detect speech locally and transcribe completed utterances with Whisper."""
        while self.is_running:
            if self.asr_model is None:
                time.sleep(0.5)
                continue
            if not self.is_listening:
                time.sleep(0.1)
                continue
            try:
                audio_frames = []
                speech_started = False
                silence_seconds = 0.0
                speech_start_time = None
                threshold = float(os.environ.get("AURA_VOICE_RMS_THRESHOLD", "450"))
                while self.is_running and self.is_listening:
                    try:
                        chunk = self.audio_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    if time.monotonic() < self._suppress_audio_until:
                        audio_frames.clear()
                        speech_started = False
                        silence_seconds = 0.0
                        speech_start_time = None
                        continue

                    if chunk.size == 0:
                        continue
                    rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                    if not math.isfinite(rms):
                        continue
                    chunk_seconds = len(chunk) / self.target_rate
                    if not speech_started:
                        if rms < threshold:
                            continue
                        speech_started = True
                        speech_start_time = time.monotonic()

                    audio_frames.append(chunk)
                    silence_seconds = silence_seconds + chunk_seconds if rms < threshold else 0.0
                    utterance_seconds = time.monotonic() - speech_start_time
                    if silence_seconds >= 0.55 or utterance_seconds >= 7.0:
                        break

                if not audio_frames or not self.is_listening or time.monotonic() < self._suppress_audio_until:
                    continue

                audio_data = np.concatenate(audio_frames, axis=0)
                if audio_data.size < int(self.target_rate * 0.2):
                    continue
                try:
                    segments, _ = self.asr_model.transcribe(
                        audio_data.astype(np.float32) / 32768.0,
                        language="en", beam_size=1, vad_filter=True,
                        condition_on_previous_text=False,
                    )
                    text = " ".join(segment.text.strip() for segment in segments).strip()
                    if text:
                        self._send_ws({"type": "voice_status", "listening": True, "message": "I heard you. Thinking locally…"})
                        self.execute_command(text)
                except Exception as exc:
                    print(f"[VoiceCommander] Local transcription failed: {exc}", flush=True)
            except Exception:
                time.sleep(0.1)

    def _clear_audio_queue(self):
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

    def start(self):
        """Starts local speech recognition; audio comes from AURA's local stream only when enabled."""
        if self.is_running:
            return
        if WhisperModel is None:
            self.asr_error = "Install faster-whisper to enable local speech recognition."
        else:
            model_dir = Path(os.environ.get(
                "AURA_WHISPER_MODEL",
                str(Path(__file__).resolve().parent / "models" / "voice" / "whisper-small.en"),
            )).expanduser()
            if not model_dir.is_dir():
                self.asr_error = f"Local Whisper model not found at {model_dir}. See README local voice setup."
            else:
                try:
                    self.asr_model = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
                    self.asr_error = ""
                except Exception as exc:
                    self.asr_error = f"Could not load local Whisper model: {exc}"
                    print(f"[VoiceCommander] {self.asr_error}", flush=True)
        self.is_running = True
        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()
        self.control_thread = threading.Thread(target=self._control_worker, daemon=True)
        self.control_thread.start()

        # The engine owns the single microphone stream for acoustic selection. It
        # forwards audio to this worker only while the pet is actively listening.
        self._send_ws({"type": "voice_status", "listening": False, "source": "AURA local audio stream",
                       "error": self.asr_error or None})
        print("[VoiceCommander] Local voice service ready; audio forwarding is gated by Flipping.", flush=True)

    def stop(self):
        """Stops audio stream."""
        self.is_running = False
        self.is_listening = False
        self._clear_audio_queue()


if __name__ == "__main__":
    commander = VoiceCommander()
    commander.start()
    print("Voice Commander running on macOS/Windows.")
    print("Try saying: 'open safari', 'open notes', 'double click', 'spotlight', 'zoom', 'scroll down'...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        commander.stop()
