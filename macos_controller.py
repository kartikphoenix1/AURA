import json
import math
import os
import sys
import time
import subprocess
import urllib.parse
import threading
from typing import Optional, Tuple, Dict, Any

import asyncio
import websockets

# macOS Native Quartz & Accessibility APIs via PyObjC
try:
    import Quartz
    import ApplicationServices
    MACOS_NATIVE_AVAILABLE = True
except ImportError:
    MACOS_NATIVE_AVAILABLE = False

# System-wide gaze cursor overlay (transparent NSPanel above all windows)
try:
    import cursor_overlay
    OVERLAY_AVAILABLE = True
except Exception:
    OVERLAY_AVAILABLE = False


class _OneEuro:
    """One Euro filter (Casiez et al., CHI 2012)."""
    def __init__(self, min_cutoff: float = 0.6, beta: float = 0.01, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev: Optional[float] = None
        self._dx_prev: float = 0.0
        self._t_prev: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt) if dt > 0 else 1.0

    def filter(self, x: float, t: float) -> float:
        if self._t_prev is None:
            self._x_prev = x
            self._dx_prev = 0.0
            self._t_prev = t
            return x
        dt = max(t - self._t_prev, 1e-6)
        self._t_prev = t
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        edx = a_d * dx + (1.0 - a_d) * self._dx_prev
        self._dx_prev = edx
        cutoff = self.min_cutoff + self.beta * abs(edx)
        a = self._alpha(cutoff, dt)
        x_out = a * x + (1.0 - a) * self._x_prev
        self._x_prev = x_out
        return x_out

    def reset(self):
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None


def check_accessibility(prompt: bool = True) -> bool:
    """
    Checks if macOS Accessibility Permissions are granted.
    If not and prompt=True, asks macOS to display the system permission prompt
    and offers to open System Settings -> Privacy & Security -> Accessibility.
    """
    if not MACOS_NATIVE_AVAILABLE:
        return False
    try:
        trusted = ApplicationServices.AXIsProcessTrusted()
        if not trusted and prompt:
            print("[Aura macOS Controller] ⚠️ Accessibility permission is NOT enabled for this process.")
            print("[Aura macOS Controller] Requesting permission from macOS System Settings...")
            options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
            ApplicationServices.AXIsProcessTrustedWithOptions(options)
        return bool(trusted)
    except Exception as e:
        print(f"[Aura macOS Controller] Accessibility check error: {e}")
        return False


def open_accessibility_settings():
    """Opens macOS System Settings directly to Privacy & Security -> Accessibility."""
    try:
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
        print("[Aura macOS Controller] Opened macOS Accessibility Settings pane.")
    except Exception as e:
        print(f"[Aura macOS Controller] Failed to open system settings: {e}")


class MacOSController:
    """
    Comprehensive Native macOS OS Controller for Aura:
    - Zero-latency system-wide cursor movement via CoreGraphics / Quartz.
    - True kCGEventMouseMoved dispatch so macOS Dock, menu bar, and external apps respond to hover and focus.
    - Hardware-accurate clicks (Left Click, Right Click, Double Click) with proper kCGMouseEventClickState.
    - Dwell-to-Click Engine: hold gaze on any app icon or file to open it automatically!
    - Accessibility-bound sticky friction over buttons and macOS Dock items using AXUIElement.
    - Native macOS Accessibility Zoom (Cmd + Option + 8).
    - Native Smooth Scroll via CGEventCreateScrollWheelEvent2.
    - Direct macOS App Launcher (Safari, Notes, Finder, Terminal, Spotify, etc.).
    - Global Hardware/Software Kill Switch (Spacebar toggle).
    """

    # Keep the native pointer as the click target when macOS Accessibility is
    # granted, and also render the gaze ring so tracking remains visible when
    # macOS blocks native pointer movement.
    VIRTUAL_CURSOR_MODE: bool = False

    # Pixels from screen edge where gaze is considered unreliable.
    EDGE_GUARD_PX: int = 10

    # Magnetic intensity presets
    MAGNETIC_MODES = {
        # Values are movement multipliers while over an accessible control.
        # No mode pulls the cursor toward a control's center.
        "off": {"friction": 1.0},
        "low": {"friction": 0.65},
        "medium": {"friction": 0.45},
        "high": {"friction": 0.32},
        "ultra": {"friction": 0.22},
    }

    # Clickable accessibility roles on macOS
    CLICKABLE_ROLES = [
        "AXButton",
        "AXPopUpButton",
        "AXMenuButton",
        "AXRadioButton",
        "AXCheckBox",
        "AXTab",
        "AXLink",
        "AXMenuItem",
        "AXApplicationDockItem",
        "AXDockItem",
        "AXOutlineRow",
        "AXRow",
    ]

    def __init__(self, ws_url: str = "ws://127.0.0.1:8765"):
        self.ws_url = ws_url
        self.is_enabled: bool = True
        self.is_paused_by_failsafe: bool = False
        self.last_click_state: bool = False

        # Accessibility-based sticky friction settings
        self.magnetic_mode: str = "medium"
        self.enable_magnetic_friction: bool = True
        self.current_friction_multiplier: float = 1.0
        self._sticky_bounds = None
        self._sticky_active = False
        self._base_min_cutoff = 0.35
        self._base_beta = 0.0001

        # Dwell-to-click engine
        self.dwell_enabled: bool = True
        self.blink_enabled: bool = True
        self.dwell_target_time: float = 1.5   # 1.5s hold = left click (more deliberate)
        self.dwell_radius: float = 40.0       # pixels — must stay within 40px to count
        self.dwell_mode: str = "left"
        self.dwell_anchor: Optional[Tuple[float, float]] = None
        self.dwell_start_time: Optional[float] = None
        self.dwell_triggered: bool = False
        self.dwell_cooldown_until: float = 0.0   # no dwell click allowed before this time
        self.DWELL_COOLDOWN: float = 2.0          # seconds between dwell clicks

        # Cursor tracking
        self.last_x: float = 800.0
        self.last_y: float = 500.0
        self.is_zoomed: bool = False
        self._euro_x = _OneEuro(min_cutoff=0.35, beta=0.0001)
        self._euro_y = _OneEuro(min_cutoff=0.35, beta=0.0001)
        self._last_motion_time: Optional[float] = None

        # Screen dimensions for edge guard
        try:
            bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            self._screen_w = int(bounds.size.width)
            self._screen_h = int(bounds.size.height)
        except Exception:
            self._screen_w = 1920
            self._screen_h = 1080

        # Check accessibility status on boot
        self.is_trusted = check_accessibility(prompt=True)
        if not self.is_trusted:
            print("[Aura macOS Controller] ⚠️ NOTICE: Run with macOS Accessibility permissions enabled to control OS apps.")

        # Pending WS reference so killswitch can notify the frontend
        self._ws_ref = None
        self._ws_loop = None

        # Initialise native screen-wide gaze cursor overlay
        if OVERLAY_AVAILABLE and sys.platform == "darwin":
            try:
                cursor_overlay.init_overlay()
                print("[Aura macOS Controller] Screen-wide gaze cursor overlay ready.", flush=True)
            except Exception as e:
                print(f"[Aura macOS Controller] Overlay init failed: {e}", flush=True)

        # Setup spacebar killswitch
        self._setup_killswitch()

    def _setup_killswitch(self):
        """
        Spacebar kill switch using a Quartz CGEventTap.
        CGEventTap operates at the HID driver level and is safe to run from
        any thread — it does NOT touch the Text Services Manager (TSM).
        Falls back to a no-op if Quartz is unavailable.
        """
        if not MACOS_NATIVE_AVAILABLE:
            print("[Aura Killswitch] Quartz unavailable — spacebar kill switch disabled.")
            return

        SPACE_KEYCODE = 49  # kVK_Space

        def tap_callback(proxy, event_type, event, refcon):
            try:
                if event_type == Quartz.kCGEventKeyDown:
                    keycode = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode
                    )
                    if keycode == SPACE_KEYCODE:
                        self.is_enabled = not self.is_enabled
                        state_str = "ENABLED" if self.is_enabled else "STOPPED"
                        print(f"\n[Aura Killswitch] Gaze cursor {state_str} via Spacebar!", flush=True)

                        # Show/hide the native overlay
                        if OVERLAY_AVAILABLE:
                            if self.is_enabled:
                                cursor_overlay.set_position(self.last_x, self.last_y, visible=True)
                            else:
                                cursor_overlay.hide()

                        # Notify the webview frontend
                        if self._ws_ref is not None and self._ws_loop is not None:
                            import asyncio
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self._ws_ref.send(json.dumps({
                                        "type": "cursor_state",
                                        "enabled": self.is_enabled
                                    })),
                                    self._ws_loop
                                )
                            except Exception:
                                pass
            except Exception:
                pass
            return event

        def run_tap():
            try:
                tap = Quartz.CGEventTapCreate(
                    Quartz.kCGSessionEventTap,
                    Quartz.kCGHeadInsertEventTap,
                    Quartz.kCGEventTapOptionListenOnly,
                    Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
                    tap_callback,
                    None
                )
                if tap is None:
                    print("[Aura Killswitch] CGEventTap unavailable (need Accessibility permission).")
                    return

                loop_src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
                run_loop = Quartz.CFRunLoopGetCurrent()
                Quartz.CFRunLoopAddSource(run_loop, loop_src, Quartz.kCFRunLoopDefaultMode)
                Quartz.CGEventTapEnable(tap, True)
                print("[Aura Killswitch] CGEventTap active — press SPACEBAR to toggle gaze cursor.", flush=True)
                Quartz.CFRunLoopRun()
            except Exception as e:
                print(f"[Aura Killswitch] CGEventTap error: {e}")

        self._killswitch_thread = threading.Thread(target=run_tap, daemon=True, name="aura-killswitch-tap")
        self._killswitch_thread.start()

    def _set_sticky_state(self, active: bool):
        """Switch One Euro response and speed multiplier for an AX button."""
        self._sticky_active = active
        mode = self.MAGNETIC_MODES.get(self.magnetic_mode, self.MAGNETIC_MODES["medium"])
        self.current_friction_multiplier = mode["friction"] if active else 1.0
        scale = 0.55 if active else 1.0
        for axis_filter in (self._euro_x, self._euro_y):
            axis_filter.min_cutoff = self._base_min_cutoff * scale
            axis_filter.beta = self._base_beta * scale

    def _accessible_clickable_bounds(self, x: float, y: float):
        """Return a clickable AX element rectangle containing the gaze point."""
        if not MACOS_NATIVE_AVAILABLE or not self.is_trusted:
            return None
        try:
            system_wide = ApplicationServices.AXUIElementCreateSystemWide()
            err, element = ApplicationServices.AXUIElementCopyElementAtPosition(
                system_wide, float(x), float(y), None
            )
            if err != 0 or element is None:
                return None
            err_role, role = ApplicationServices.AXUIElementCopyAttributeValue(
                element, ApplicationServices.kAXRoleAttribute, None
            )
            if err_role != 0 or role not in self.CLICKABLE_ROLES:
                return None

            err_pos, pos_value = ApplicationServices.AXUIElementCopyAttributeValue(
                element, ApplicationServices.kAXPositionAttribute, None
            )
            err_size, size_value = ApplicationServices.AXUIElementCopyAttributeValue(
                element, ApplicationServices.kAXSizeAttribute, None
            )
            if err_pos != 0 or err_size != 0 or pos_value is None or size_value is None:
                return None

            ok_pos, point = ApplicationServices.AXValueGetValue(
                pos_value, ApplicationServices.kAXValueCGPointType, None
            )
            ok_size, size = ApplicationServices.AXValueGetValue(
                size_value, ApplicationServices.kAXValueCGSizeType, None
            )
            if not ok_pos or not ok_size:
                return None
            left, top = float(point.x), float(point.y)
            right, bottom = left + float(size.width), top + float(size.height)
            if left <= x <= right and top <= y <= bottom:
                return (left, top, right, bottom)
        except Exception:
            return None
        return None

    def _apply_magnetic_attraction(self, x: float, y: float) -> Tuple[float, float]:
        """Apply sticky filtering inside AX controls without snapping their centers."""
        if not self.enable_magnetic_friction or self.magnetic_mode == "off":
            self._sticky_bounds = None
            self._set_sticky_state(False)
            return x, y

        bounds = self._accessible_clickable_bounds(x, y)
        if bounds is not None:
            self._sticky_bounds = bounds
            self._set_sticky_state(True)
            return x, y

        # Keep the slower response for a 14px margin around the last control.
        # This hysteresis prevents small gaze noise from releasing the button.
        if self._sticky_bounds is not None:
            left, top, right, bottom = self._sticky_bounds
            margin = 14.0
            if left - margin <= x <= right + margin and top - margin <= y <= bottom + margin:
                self._set_sticky_state(True)
                return x, y

        self._sticky_bounds = None
        self._set_sticky_state(False)
        return x, y

    def _update_dwell(self, x: float, y: float):
        """
        Tracks cursor dwell over an area. If the user fixates their gaze within
        dwell_radius for dwell_target_time, triggers a left click.
        A 2s cooldown prevents rapid-fire clicks when the cursor jumps.
        """
        if not self.dwell_enabled or not self.is_enabled or self.is_paused_by_failsafe:
            self.dwell_start_time = None
            self.dwell_anchor = None
            return

        now = time.time()

        if now < self.dwell_cooldown_until:
            return

        if self.dwell_anchor is None:
            self.dwell_anchor = (x, y)
            self.dwell_start_time = now
            self.dwell_triggered = False
            return

        dist = math.hypot(x - self.dwell_anchor[0], y - self.dwell_anchor[1])
        if dist > self.dwell_radius:
            # Moved beyond dwell circle — start fresh dwell fixation
            self.dwell_anchor = (x, y)
            self.dwell_start_time = now
            self.dwell_triggered = False
        else:
            # Still fixating inside circle: do not re-click if already triggered at this spot
            if self.dwell_triggered:
                return

            dwell_duration = now - (self.dwell_start_time or now)
            if dwell_duration >= self.dwell_target_time:
                self.dwell_triggered = True
                self.dwell_cooldown_until = now + self.DWELL_COOLDOWN
                click_type = self.dwell_mode if self.dwell_mode in ["left", "right", "double"] else "left"
                self.click_mouse(x, y, click_type)
                print(f"[Aura macOS Controller] Dwell {click_type}-click at ({int(x)}, {int(y)})", flush=True)

    def move_mouse(self, x: float, y: float):
        """Move the real pointer and update gaze dwell at the final cursor point."""
        if not self.is_enabled:
            if OVERLAY_AVAILABLE:
                cursor_overlay.hide()
            return
        if self.is_paused_by_failsafe:
            if OVERLAY_AVAILABLE:
                cursor_overlay.set_position(self.last_x, self.last_y, visible=True, paused=True)
            return
        sw, sh = self._screen_w, self._screen_h
        x = max(0.0, min(float(sw - 1), x))
        y = max(0.0, min(float(sh - 1), y))

        if self.VIRTUAL_CURSOR_MODE:
            self.last_x, self.last_y = x, y
            if OVERLAY_AVAILABLE:
                cursor_overlay.set_position(x, y, visible=True)
            self._update_dwell(x, y)
            return

        if MACOS_NATIVE_AVAILABLE:
            target_x, target_y = self._apply_magnetic_attraction(x, y)
            friction = self.current_friction_multiplier
            previous = getattr(self, "_last_os_position", None)
            if previous is not None and friction < 1.0:
                target_x = previous[0] + (target_x - previous[0]) * friction
                target_y = previous[1] + (target_y - previous[1]) * friction

            # Bound pointer speed after filtering so a transient landmark jump
            # cannot fling the native cursor across the screen.
            now = time.monotonic()
            if previous is not None and self._last_motion_time is not None:
                dt = min(max(now - self._last_motion_time, 0.008), 0.05)
                dx, dy = target_x - previous[0], target_y - previous[1]
                distance = math.hypot(dx, dy)
                max_step = 900.0 * dt
                if distance > max_step:
                    scale = max_step / distance
                    target_x = previous[0] + dx * scale
                    target_y = previous[1] + dy * scale
            self._last_motion_time = now
            if OVERLAY_AVAILABLE:
                cursor_overlay.set_position(target_x, target_y, visible=True)
            point = Quartz.CGPoint(x=float(target_x), y=float(target_y))
            Quartz.CGWarpMouseCursorPosition(point)
            try:
                move_event = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft
                )
                if move_event:
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move_event)
            except Exception:
                pass
            self._last_os_position = (target_x, target_y)
            self.last_x, self.last_y = target_x, target_y
            self._update_dwell(target_x, target_y)
        else:
            self.last_x, self.last_y = x, y
            if OVERLAY_AVAILABLE:
                cursor_overlay.set_position(x, y, visible=True)


    def click_mouse(self, x: float, y: float, button: str = "left"):
        """
        Dispatches native macOS mouse clicks with hardware-accurate timing and click count:
        - "left": Standard left click
        - "double": Native double click (crucial for opening files, apps, and desktop items)
        - "right": Secondary right click (context menus)
        """
        if not MACOS_NATIVE_AVAILABLE or not self.is_enabled or self.is_paused_by_failsafe:
            return

        point = Quartz.CGPoint(x=float(x), y=float(y))

        if button == "right":
            down_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventRightMouseDown, point, Quartz.kCGMouseButtonRight)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down_event)
            time.sleep(0.02)
            up_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventRightMouseUp, point, Quartz.kCGMouseButtonRight)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up_event)
            print(f"[Aura macOS Controller] Right Click dispatched at ({x:.1f}, {y:.1f})", flush=True)

        elif button == "double":
            # First click
            d1 = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(d1, Quartz.kCGMouseEventClickState, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, d1)
            time.sleep(0.02)
            u1 = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(u1, Quartz.kCGMouseEventClickState, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, u1)

            time.sleep(0.04)

            # Second click (with click state = 2)
            d2 = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(d2, Quartz.kCGMouseEventClickState, 2)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, d2)
            time.sleep(0.02)
            u2 = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(u2, Quartz.kCGMouseEventClickState, 2)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, u2)
            print(f"[Aura macOS Controller] Double Click dispatched at ({x:.1f}, {y:.1f})", flush=True)

        else:
            # Standard single left click
            down_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(down_event, Quartz.kCGMouseEventClickState, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down_event)
            time.sleep(0.02)
            up_event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(up_event, Quartz.kCGMouseEventClickState, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up_event)
            print(f"[Aura macOS Controller] Left Click dispatched at ({x:.1f}, {y:.1f})", flush=True)

    def scroll(self, direction: str, amount: int = 40):
        """Smoothly scrolls the active macOS window under cursor."""
        if not MACOS_NATIVE_AVAILABLE:
            return
        delta = amount if direction == "up" else -amount
        event = Quartz.CGEventCreateScrollWheelEvent2(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(delta), 0, 0
        )
        if event:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            print(f"[Aura macOS Controller] Scroll {direction} ({delta}px)", flush=True)

    def trigger_zoom(self, state: Optional[bool] = None):
        """
        Toggles macOS system screen zoom via Cmd + Option + 8.
        """
        if not MACOS_NATIVE_AVAILABLE:
            return
        if state is not None and state == self.is_zoomed:
            return
        self.is_zoomed = not self.is_zoomed

        # Key code 0x1C is '8' on US layout
        k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x1C, True)
        Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
        time.sleep(0.02)
        k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x1C, False)
        Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
        print(f"[Aura macOS Controller] Zoom toggled: {self.is_zoomed}", flush=True)

    def _press_shortcut(self, key: str, command: bool = False, shift: bool = False, control: bool = False, option: bool = False):
        if not MACOS_NATIVE_AVAILABLE:
            return
        keycodes = {"a":0x00,"s":0x01,"f":0x03,"h":0x04,"c":0x08,"v":0x09,"x":0x07,"z":0x06,"q":0x0C,"w":0x0D,"t":0x11,"m":0x2E,"tab":0x30,"enter":0x24,"escape":0x35,"page_up":0x74,"page_down":0x79,"left":0x7B,"right":0x7C,"[":0x21,"]":0x1E,"3":0x14,"volume_up":0x48,"volume_down":0x49,"volume_mute":0x4A}
        code = keycodes.get(key)
        if code is None:
            return
        flags = 0
        if command: flags |= Quartz.kCGEventFlagMaskCommand
        if shift: flags |= Quartz.kCGEventFlagMaskShift
        if control: flags |= Quartz.kCGEventFlagMaskControl
        if option: flags |= Quartz.kCGEventFlagMaskAlternate
        down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
        Quartz.CGEventSetFlags(down, flags); Quartz.CGEventSetFlags(up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        time.sleep(0.02)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def open_app(self, app_name: str):
        """Launches or switches to a macOS application by name."""
        try:
            print(f"[Aura macOS Controller] Opening app: '{app_name}'", flush=True)
            subprocess.Popen(["open", "-a", app_name])
        except Exception as e:
            print(f"[Aura macOS Controller] Error opening app '{app_name}': {e}")

    def open_spotlight(self):
        """Opens macOS Spotlight Search via Cmd + Space."""
        if not MACOS_NATIVE_AVAILABLE:
            return
        # Key code 0x31 is Spacebar
        k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x31, True)
        Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
        time.sleep(0.02)
        k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x31, False)
        Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
        print("[Aura macOS Controller] Spotlight opened via Cmd + Space", flush=True)

    def switch_app(self):
        """Switches to the next macOS app via Cmd + Tab."""
        if not MACOS_NATIVE_AVAILABLE:
            return
        # Key code 0x30 is Tab
        k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x30, True)
        Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
        time.sleep(0.03)
        k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x30, False)
        Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
        print("[Aura macOS Controller] Switched app via Cmd + Tab", flush=True)

    def show_desktop(self):
        """Toggles macOS Mission Control Show Desktop via Cmd + F3."""
        if not MACOS_NATIVE_AVAILABLE:
            return
        # Key code 0x63 is F3
        k_down = Quartz.CGEventCreateKeyboardEvent(None, 0x63, True)
        Quartz.CGEventSetFlags(k_down, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_down)
        time.sleep(0.03)
        k_up = Quartz.CGEventCreateKeyboardEvent(None, 0x63, False)
        Quartz.CGEventSetFlags(k_up, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, k_up)
        print("[Aura macOS Controller] Show Desktop triggered", flush=True)

    async def run(self):
        """Main loop listening to WebSocket telemetry and control stream."""
        self._ws_loop = asyncio.get_event_loop()
        while True:
            try:
                print(f"[Aura macOS Controller] Connecting to telemetry stream at {self.ws_url}...")
                async with websockets.connect(self.ws_url) as websocket:
                    self._ws_ref = websocket
                    print("[Aura macOS Controller] Connected! Press [SPACEBAR] at any time for emergency Kill Switch.")

                    # Broadcast initial accessibility state
                    try:
                        await websocket.send(json.dumps({
                            "type": "accessibility_status",
                            "trusted": self.is_trusted
                        }))
                    except Exception:
                        pass

                    prev_x, prev_y = None, None

                    async for message in websocket:
                        data = json.loads(message)
                        msg_type = data.get("type")

                        # 1. Settings updates from frontend or dock
                        if msg_type == "update_settings":
                            if "dwell_time" in data:
                                self.dwell_target_time = min(8.0, max(0.3, float(data["dwell_time"])))
                                print(f"[Aura macOS Controller] Dwell time set: {self.dwell_target_time}s")
                            if "dwell_mode" in data:
                                self.dwell_mode = str(data["dwell_mode"]).lower()
                                print(f"[Aura macOS Controller] Dwell click mode: {self.dwell_mode}")
                            if "magnetic_intensity" in data:
                                self.magnetic_mode = str(data["magnetic_intensity"]).lower()
                                print(f"[Aura macOS Controller] Magnetic mode set: {self.magnetic_mode}")
                            if "gaze_enabled" in data:
                                self.is_enabled = bool(data["gaze_enabled"])
                            if "dwell_enabled" in data:
                                self.dwell_enabled = bool(data["dwell_enabled"])
                            if "blink_enabled" in data:
                                self.blink_enabled = bool(data["blink_enabled"])
                            if "trigger_zoom" in data:
                                self.trigger_zoom(data.get("zoom_state"))
                            if "min_cutoff" in data:
                                self._base_min_cutoff = max(0.05, float(data["min_cutoff"]))
                                self._set_sticky_state(self._sticky_active)
                            if "beta" in data:
                                self._base_beta = max(0.0, float(data["beta"]))
                                self._set_sticky_state(self._sticky_active)

                        # 2. Voice commands
                        elif msg_type == "voice_command":
                            action = data.get("action")
                            if action == "left_click":
                                self.click_mouse(self.last_x, self.last_y, "left")
                            elif action == "right_click":
                                self.click_mouse(self.last_x, self.last_y, "right")
                            elif action == "double_click":
                                self.click_mouse(self.last_x, self.last_y, "double")
                            elif action == "scroll_down":
                                self.scroll("down", 120)
                            elif action == "scroll_up":
                                self.scroll("up", 120)
                            elif action in ("scroll_left", "scroll_right"):
                                if MACOS_NATIVE_AVAILABLE:
                                    delta = -120 if action == "scroll_left" else 120
                                    event = Quartz.CGEventCreateScrollWheelEvent2(None, Quartz.kCGScrollEventUnitPixel, 2, 0, delta)
                                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
                            elif action == "zoom_in":
                                self.trigger_zoom(True)
                            elif action == "zoom_out":
                                self.trigger_zoom(False)
                            elif action == "page_down":
                                self._press_shortcut("page_down")
                            elif action == "page_up":
                                self._press_shortcut("page_up")
                            elif action == "next_tab":
                                self._press_shortcut("tab", command=True)
                            elif action == "previous_tab":
                                self._press_shortcut("tab", command=True, shift=True)
                            elif action == "close_tab":
                                self._press_shortcut("w", command=True)
                            elif action == "close_app":
                                self._press_shortcut("q", command=True)
                            elif action == "new_tab":
                                self._press_shortcut("t", command=True)
                            elif action == "go_back":
                                self._press_shortcut("[", command=True)
                            elif action == "go_forward":
                                self._press_shortcut("]", command=True)
                            elif action == "screenshot":
                                self._press_shortcut("3", command=True, shift=True)
                            elif action == "volume_up":
                                self._press_shortcut("volume_up")
                            elif action == "volume_down":
                                self._press_shortcut("volume_down")
                            elif action == "mute":
                                self._press_shortcut("volume_mute")
                            elif action == "minimize":
                                self._press_shortcut("m", command=True)
                            elif action == "maximize":
                                self._press_shortcut("f", command=True, control=True)
                            elif action == "enter":
                                self._press_shortcut("enter")
                            elif action == "escape":
                                self._press_shortcut("escape")
                            elif action == "copy":
                                self._press_shortcut("c", command=True)
                            elif action == "paste":
                                self._press_shortcut("v", command=True)
                            elif action == "cut":
                                self._press_shortcut("x", command=True)
                            elif action == "undo":
                                self._press_shortcut("z", command=True)
                            elif action == "redo":
                                self._press_shortcut("z", command=True, shift=True)
                            elif action == "select_all":
                                self._press_shortcut("a", command=True)
                            elif action == "save":
                                self._press_shortcut("s", command=True)
                            elif action == "open_app":
                                self.open_app(data.get("app", "Safari"))
                            elif action == "spotlight":
                                self.open_spotlight()
                            elif action == "switch_app":
                                self.switch_app()
                            elif action == "show_desktop":
                                self.show_desktop()

                        # 3. System actions from UI buttons
                        elif msg_type == "system_action":
                            action = data.get("action")
                            if action == "open_app":
                                self.open_app(data.get("app", "Safari"))
                            elif action == "open_spotlight":
                                query = data.get("query")
                                if query:
                                    url = "https://www.google.com/search?q=" + urllib.parse.quote(str(query))
                                    subprocess.Popen(["open", url])
                                else:
                                    self.open_spotlight()
                            elif action == "switch_app":
                                self.switch_app()
                            elif action == "show_desktop":
                                self.show_desktop()
                            elif action == "open_accessibility_settings":
                                open_accessibility_settings()
                            elif action == "check_accessibility":
                                self.is_trusted = check_accessibility(prompt=True)
                                await websocket.send(json.dumps({
                                    "type": "accessibility_status",
                                     "trusted": self.is_trusted
                                 }))

                        # 4. Real-time Telemetry
                        elif msg_type == "telemetry":
                            raw_target_x = data.get("x", 0.0)
                            raw_target_y = data.get("y", 0.0)
                            click_intent = data.get("click", False)
                            abort_triggered = data.get("abort_triggered", False)

                            # Update failsafe pause state
                            is_blink_locked = bool(data.get("is_blink_locked", False))
                            self.is_paused_by_failsafe = is_blink_locked or abort_triggered

                            # If no face is detected, do not hijack the OS mouse pointer
                            if not data.get("face_detected", False):
                                self._euro_x.reset()
                                self._euro_y.reset()
                                prev_x, prev_y = None, None
                                if OVERLAY_AVAILABLE:
                                    cursor_overlay.hide()
                                continue

                            # Apply One Euro filter for smooth, low-latency cursor movement
                            now_mono = time.monotonic()
                            current_x = self._euro_x.filter(raw_target_x, now_mono)
                            current_y = self._euro_y.filter(raw_target_y, now_mono)

                            # Execute movement across all Mac apps and Dock
                            self.move_mouse(current_x, current_y)

                            # Execute click on rising edge of acoustic/blink intent
                            click_source = data.get("click_source", "none")
                            allow_click = click_source != "blink" or self.blink_enabled
                            if click_intent and allow_click and not self.last_click_state:
                                click_type = data.get("click_type", "left")
                                if click_type == "right":
                                    self.click_mouse(current_x, current_y, "right")
                                else:
                                    self.click_mouse(current_x, current_y, "left")

                            self.last_click_state = click_intent


            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError):
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"[Aura macOS Controller] Notice: {e}")
                await asyncio.sleep(1.0)


if __name__ == "__main__":
    controller = MacOSController()
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n[Aura macOS Controller] Exited cleanly.")
