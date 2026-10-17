import json
import time
import sys
from typing import Optional

import asyncio
import websockets
from pynput import keyboard

# Windows Native Win32 API via ctypes
try:
    import ctypes
    from ctypes import wintypes
    WINDOWS_NATIVE_AVAILABLE = (sys.platform == "win32")
except ImportError:
    WINDOWS_NATIVE_AVAILABLE = False


class WindowsController:
    """
    Native Windows OS Controller for Aura:
    - Zero-latency cursor positioning via Win32 SetCursorPos.
    - Click simulation via SendInput / mouse_event.
    - Inspects Windows UI Automation for Dynamic Magnetic Friction over clickable buttons.
    - Global Hardware/Software Kill Switch (Spacebar toggle).
    """

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(self, ws_url: str = "ws://127.0.0.1:8765"):
        self.ws_url = ws_url
        self.is_enabled: bool = True
        self.is_paused_by_failsafe: bool = False
        self.last_click_state: bool = False

        self.enable_magnetic_friction: bool = True
        self.current_friction_multiplier: float = 1.0

        if WINDOWS_NATIVE_AVAILABLE:
            self.user32 = ctypes.windll.user32
        else:
            self.user32 = None

        self._setup_killswitch()

    def _setup_killswitch(self):
        """Global key listener for Spacebar kill switch on Windows."""
        def on_press(key):
            try:
                if key == keyboard.Key.space:
                    self.is_enabled = not self.is_enabled
                    state_str = "ENABLED" if self.is_enabled else "KILLED / DISABLED"
                    print(f"\n[Aura Win32 Killswitch] OS Mouse Control {state_str} via Spacebar!")
            except Exception:
                pass

        self.key_listener = keyboard.Listener(on_press=on_press)
        self.key_listener.daemon = True
        self.key_listener.start()

    def _check_accessibility_element(self, x: float, y: float) -> bool:
        """
        Inspects element under cursor using Windows UI Automation.
        Returns True if over a button, tab, or link.
        """
        if not WINDOWS_NATIVE_AVAILABLE:
            return False

        try:
            # Fallback check or UIAutomation inspect if uiautomation package is present
            import uiautomation as auto
            element = auto.ControlFromPoint(int(x), int(y))
            if element and element.ControlType in [
                auto.ControlType.ButtonControl,
                auto.ControlType.TabItemControl,
                auto.ControlType.HyperlinkControl,
                auto.ControlType.CheckBoxControl,
                auto.ControlType.RadioButtonControl,
            ]:
                return True
        except Exception:
            pass
        return False

    def move_mouse(self, x: float, y: float):
        """Win32 SetCursorPos with magnetic friction."""
        if not self.is_enabled or self.is_paused_by_failsafe or not self.user32:
            return

        # Check for sticky button friction
        if self.enable_magnetic_friction and self._check_accessibility_element(x, y):
            self.current_friction_multiplier = 0.35
        else:
            self.current_friction_multiplier = 1.0

        self.user32.SetCursorPos(int(x), int(y))

    def click_mouse(self, x: float, y: float):
        """Win32 click simulation using SetCursorPos + mouse_event at exact coordinates."""
        if not self.is_enabled or self.is_paused_by_failsafe or not self.user32:
            return

        # Explicitly pin cursor to target coordinate
        self.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.005)
        # Dispatch mouse down & up with dx=0, dy=0 so Windows does not add relative offset
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.025)  # 25ms click dwell
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        print(f"[Aura Windows Controller] Mouse Click dispatched at ({int(x)}, {int(y)})", flush=True)

    async def run(self):
        """Main loop listening to WebSocket telemetry stream with auto-reconnection."""
        while True:
            try:
                print(f"[Aura Windows Controller] Connecting to telemetry stream at {self.ws_url}...")
                async with websockets.connect(self.ws_url) as websocket:
                    print("[Aura Windows Controller] Connected! Press [SPACEBAR] at any time for emergency Kill Switch.")
                    prev_x, prev_y = None, None

                    async for message in websocket:
                        data = json.loads(message)
                        if data.get("type") != "telemetry":
                            continue

                        raw_target_x = data.get("x", 0.0)
                        raw_target_y = data.get("y", 0.0)
                        click_intent = data.get("click", False)
                        is_blink_locked = data.get("is_blink_locked", False)
                        abort_triggered = data.get("abort_triggered", False)

                        self.is_paused_by_failsafe = is_blink_locked or abort_triggered

                        # If no face is detected, do not seize the OS mouse pointer
                        if not data.get("face_detected", False):
                            prev_x, prev_y = None, None
                            continue

                        if prev_x is None:
                            prev_x, prev_y = raw_target_x, raw_target_y

                        # Apply magnetic friction multiplier
                        delta_x = (raw_target_x - prev_x) * self.current_friction_multiplier
                        delta_y = (raw_target_y - prev_y) * self.current_friction_multiplier
                        current_x = prev_x + delta_x
                        current_y = prev_y + delta_y
                        prev_x, prev_y = current_x, current_y

                        self.move_mouse(current_x, current_y)

                        if click_intent and not self.last_click_state:
                            self.click_mouse(current_x, current_y)

                        self.last_click_state = click_intent
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                print(f"[Aura Windows Controller] Telemetry disconnected ({e}). Reconnecting in 1s...")
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"[Aura Windows Controller] Unexpected error: {e}. Retrying in 1s...")
                await asyncio.sleep(1.0)


if __name__ == "__main__":
    controller = WindowsController()
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n[Aura Windows Controller] Exited cleanly.")
