import json
import time
import math
import sys
from typing import Optional, Tuple

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

try:
    import uiautomation as auto
    UIAUTOMATION_AVAILABLE = True
except ImportError:
    UIAUTOMATION_AVAILABLE = False


class WindowsController:
    """
    Enhanced Native Windows OS Controller for Aura:
    - Zero-latency cursor positioning via Win32 SetCursorPos.
    - Click simulation via mouse_event (Left, Right, Double).
    - Dynamic Magnetic Button Snapping using Windows UI Automation.
    - Dwell-to-click engine with customizable dwell duration.
    - Native hardware Zoom shortcut dispatch (Win + Plus / Win + Esc).
    - Global Hardware/Software Kill Switch (Spacebar toggle).
    """

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    VK_LWIN = 0x5B
    VK_OEM_PLUS = 0xBB
    VK_ESCAPE = 0x1B

    # Magnetic intensity settings
    MAGNETIC_MODES = {
        # Movement multipliers while the gaze is within a UIA control.
        # These modes add friction only; they never pull toward the control center.
        "off": {"friction": 1.0},
        "low": {"friction": 0.65},
        "medium": {"friction": 0.45},
        "high": {"friction": 0.32},
        "ultra": {"friction": 0.22},
    }

    def __init__(self, ws_url: str = "ws://127.0.0.1:8765"):
        self.ws_url = ws_url
        self.is_enabled: bool = False  # False by default: user uses Virtual Overlay Cursor instead
        self.is_paused_by_failsafe: bool = False
        self.last_click_state: bool = False

        # UI Automation-based sticky friction config
        self.magnetic_mode: str = "medium"
        self.current_friction_multiplier: float = 1.0
        self._sticky_bounds = None

        # Dwell to click config
        self.dwell_enabled: bool = True
        self.blink_enabled: bool = True
        self.dwell_mode: str = "left"
        self.dwell_target_time: float = 1.0  # seconds
        self.dwell_radius: float = 30.0      # pixels
        self.dwell_anchor: Optional[Tuple[float, float]] = None
        self.dwell_start_time: Optional[float] = None
        self.dwell_triggered: bool = False

        # Zoom state
        self.is_zoomed: bool = False

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
                    print(f"\n[Aura Win32 Killswitch] OS Mouse Control {state_str} via Spacebar!", flush=True)
            except Exception:
                pass

        self.key_listener = keyboard.Listener(on_press=on_press)
        self.key_listener.daemon = True
        self.key_listener.start()

    def trigger_zoom(self, state: Optional[bool] = None):
        """Dispatches Windows Magnifier Zoom."""
        if not self.user32:
            return
        if state is None:
            self.is_zoomed = not self.is_zoomed
        else:
            self.is_zoomed = state

        if self.is_zoomed:
            self.user32.keybd_event(self.VK_LWIN, 0, 0, 0)
            self.user32.keybd_event(self.VK_OEM_PLUS, 0, 0, 0)
            time.sleep(0.04)
            self.user32.keybd_event(self.VK_OEM_PLUS, 0, 2, 0)
            self.user32.keybd_event(self.VK_LWIN, 0, 2, 0)
            print("[WindowsController] Zoom In dispatched!", flush=True)
        else:
            self.user32.keybd_event(self.VK_LWIN, 0, 0, 0)
            self.user32.keybd_event(self.VK_ESCAPE, 0, 0, 0)
            time.sleep(0.04)
            self.user32.keybd_event(self.VK_ESCAPE, 0, 2, 0)
            self.user32.keybd_event(self.VK_LWIN, 0, 2, 0)
            print("[WindowsController] Zoom Out dispatched!", flush=True)

    def _apply_magnetic_attraction(self, x: float, y: float) -> Tuple[float, float]:
        """Add UIA-based friction inside controls without snapping their centers."""
        cfg = self.MAGNETIC_MODES.get(self.magnetic_mode, self.MAGNETIC_MODES["medium"])
        if cfg["friction"] >= 1.0 or not UIAUTOMATION_AVAILABLE:
            self._sticky_bounds = None
            self.current_friction_multiplier = 1.0
            return x, y

        try:
            element = auto.ControlFromPoint(int(x), int(y))
            if element and element.ControlType in [
                auto.ControlType.ButtonControl,
                auto.ControlType.TabItemControl,
                auto.ControlType.HyperlinkControl,
                auto.ControlType.CheckBoxControl,
                auto.ControlType.RadioButtonControl,
                auto.ControlType.MenuItemControl,
                auto.ControlType.ListItemControl,
            ]:
                rect = element.BoundingRectangle
                if rect and rect.left <= x <= rect.right and rect.top <= y <= rect.bottom:
                    self._sticky_bounds = (rect.left, rect.top, rect.right, rect.bottom)
                    self.current_friction_multiplier = cfg["friction"]
                    return x, y
        except Exception:
            pass

        if self._sticky_bounds is not None:
            left, top, right, bottom = self._sticky_bounds
            margin = 14
            if left - margin <= x <= right + margin and top - margin <= y <= bottom + margin:
                self.current_friction_multiplier = cfg["friction"]
                return x, y

        self._sticky_bounds = None
        self.current_friction_multiplier = 1.0
        return x, y

    def _update_dwell(self, x: float, y: float):
        """Updates dwell-to-click timer based on cursor fixation."""
        if not self.dwell_enabled or not self.is_enabled or self.is_paused_by_failsafe:
            self.dwell_start_time = None
            self.dwell_anchor = None
            return

        now = time.time()
        if self.dwell_anchor is None:
            self.dwell_anchor = (x, y)
            self.dwell_start_time = now
            self.dwell_triggered = False
            return

        dist = math.hypot(x - self.dwell_anchor[0], y - self.dwell_anchor[1])
        if dist > self.dwell_radius:
            # Moved beyond dwell circle, reset anchor
            self.dwell_anchor = (x, y)
            self.dwell_start_time = now
            self.dwell_triggered = False
        else:
            # Still fixating in circle
            dwell_duration = now - (self.dwell_start_time or now)
            if dwell_duration >= self.dwell_target_time and not self.dwell_triggered:
                self.dwell_triggered = True
                self.click_mouse(x, y, self.dwell_mode)
                print(f"[WindowsController] Dwell click triggered at ({int(x)}, {int(y)})!", flush=True)

    def move_mouse(self, x: float, y: float):
        """Win32 SetCursorPos with accessible-control friction and dwell tracking."""
        if not self.is_enabled or self.is_paused_by_failsafe or not self.user32:
            return

        final_x, final_y = self._apply_magnetic_attraction(x, y)
        self.user32.SetCursorPos(int(final_x), int(final_y))
        self._update_dwell(final_x, final_y)

    def click_mouse(self, x: float, y: float, button: str = "left"):
        """Win32 click simulation using SetCursorPos + mouse_event."""
        if not self.is_enabled or self.is_paused_by_failsafe or not self.user32:
            return

        self.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.005)

        if button == "right":
            self.user32.mouse_event(self.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.025)
            self.user32.mouse_event(self.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            print(f"[WindowsController] Right Click dispatched at ({int(x)}, {int(y)})", flush=True)
        elif button == "double":
            for _ in range(2):
                self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                time.sleep(0.02)
                self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                time.sleep(0.04)
            print(f"[WindowsController] Double Click dispatched at ({int(x)}, {int(y)})", flush=True)
        else:
            self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.025)
            self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            print(f"[WindowsController] Left Click dispatched at ({int(x)}, {int(y)})", flush=True)

    async def run(self):
        """Main loop listening to WebSocket telemetry stream."""
        while True:
            try:
                print(f"[Aura Windows Controller] Connecting to telemetry stream at {self.ws_url}...")
                async with websockets.connect(self.ws_url) as websocket:
                    print("[Aura Windows Controller] Connected! Press [SPACEBAR] at any time for emergency Kill Switch.")
                    prev_x, prev_y = None, None

                    async for message in websocket:
                        data = json.loads(message)
                        msg_type = data.get("type")

                        # Settings updates from frontend or voice commander
                        if msg_type == "update_settings":
                            if "dwell_time" in data:
                                self.dwell_target_time = min(8.0, max(0.3, float(data["dwell_time"])))
                                print(f"[WindowsController] Updated dwell time: {self.dwell_target_time}s")
                            if "magnetic_intensity" in data:
                                self.magnetic_mode = str(data["magnetic_intensity"]).lower()
                                print(f"[WindowsController] Updated magnetic mode: {self.magnetic_mode}")
                            if "gaze_enabled" in data:
                                self.is_enabled = bool(data["gaze_enabled"])
                            if "dwell_enabled" in data:
                                self.dwell_enabled = bool(data["dwell_enabled"])
                            if "blink_enabled" in data:
                                self.blink_enabled = bool(data["blink_enabled"])
                            if "dwell_mode" in data and data["dwell_mode"] in ("left", "right", "double"):
                                self.dwell_mode = data["dwell_mode"]
                            if "trigger_zoom" in data:
                                self.trigger_zoom(data.get("zoom_state"))
                            continue

                        if msg_type != "telemetry":
                            continue

                        raw_target_x = data.get("x", 0.0)
                        raw_target_y = data.get("y", 0.0)
                        click_intent = data.get("click", False)
                        click_source = data.get("click_source", "none")
                        is_blink_locked = data.get("is_blink_locked", False)
                        abort_triggered = data.get("abort_triggered", False)

                        self.is_paused_by_failsafe = is_blink_locked or abort_triggered

                        if not data.get("face_detected", False):
                            prev_x, prev_y = None, None
                            continue

                        if prev_x is None:
                            prev_x, prev_y = raw_target_x, raw_target_y

                        delta_x = (raw_target_x - prev_x) * self.current_friction_multiplier
                        delta_y = (raw_target_y - prev_y) * self.current_friction_multiplier
                        current_x = prev_x + delta_x
                        current_y = prev_y + delta_y
                        prev_x, prev_y = current_x, current_y

                        self.move_mouse(current_x, current_y)

                        allow_click = click_source != "blink" or self.blink_enabled
                        if click_intent and allow_click and not self.last_click_state:
                            btn = data.get("click_type", "left")
                            self.click_mouse(current_x, current_y, btn)

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
