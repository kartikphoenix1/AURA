import sys
import platform
import asyncio

from macos_controller import MacOSController
from windows_controller import WindowsController


class LinuxController:
    """Linux OS Controller fallback using pynput for X11/Wayland."""
    def __init__(self, ws_url: str = "ws://127.0.0.1:8765"):
        self.ws_url = ws_url
        from pynput.mouse import Controller, Button
        from pynput import keyboard
        self.mouse = Controller()
        self.Button = Button
        self.is_enabled = True
        self.is_paused = False
        self.last_click = False

        def on_press(key):
            if key == keyboard.Key.space:
                self.is_enabled = not self.is_enabled
                print(f"[Linux Killswitch] Mouse Control {'ENABLED' if self.is_enabled else 'DISABLED'}")

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.daemon = True
        self.listener.start()

    async def run(self):
        import websockets, json
        async with websockets.connect(self.ws_url) as ws:
            print("[Aura Linux Controller] Connected! Press [SPACEBAR] for emergency kill switch.")
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") != "telemetry" or not self.is_enabled:
                    continue
                if data.get("is_blink_locked") or data.get("abort_triggered"):
                    continue

                self.mouse.position = (int(data["x"]), int(data["y"]))
                if data.get("click") and not self.last_click:
                    self.mouse.click(self.Button.left, 1)
                self.last_click = data.get("click", False)


def get_controller(ws_url: str = "ws://127.0.0.1:8765"):
    """Auto-detects host OS and returns optimal native controller driver."""
    os_name = platform.system()
    print(f"[Aura OS Bridge] Detected operating system: {os_name} ({platform.machine()})")

    if os_name == "Darwin":
        print("[Aura OS Bridge] Booting macOS Quartz/CoreGraphics driver with AXUIElement sticky buttons...")
        return MacOSController(ws_url=ws_url)
    elif os_name == "Windows":
        print("[Aura OS Bridge] Booting Windows Win32 SetCursorPos driver with UIAutomation...")
        return WindowsController(ws_url=ws_url)
    else:
        print("[Aura OS Bridge] Booting Linux standard input driver...")
        return LinuxController(ws_url=ws_url)


if __name__ == "__main__":
    controller = get_controller()
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        print("\n[Aura OS Bridge] Controller shut down cleanly.")
