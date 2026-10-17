#!/usr/bin/env python3
"""
Aura (Multimodal Intent Fusion) — Native Desktop Application Launcher
Launches:
1. Local UI Assets Server (http://127.0.0.1:5173)
2. Python ML Telemetry Engine (ws://127.0.0.1:8765)
3. Cross-Platform OS Cursor Controller (macOS Quartz / Windows Win32)
4. Native Standalone Desktop Window (via pywebview WKWebView Cocoa)
"""

import os
import sys

# Auto-detect and re-exec into the project virtual environment if not already running in it
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if sys.platform == "win32":
    venv_py = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
else:
    venv_py = os.path.join(BASE_DIR, "venv", "bin", "python")

if os.path.exists(venv_py) and os.path.realpath(sys.executable) != os.path.realpath(venv_py):
    os.execv(venv_py, [venv_py] + sys.argv)

import time
import threading
import asyncio
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler

os.environ["MPLCONFIGDIR"] = "/tmp/mpl"
sys.path.insert(0, BASE_DIR)

from server import AuraEngineServer
from system_controller import get_controller


class QuietHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        aura_ui_dir = os.path.join(BASE_DIR, "aura_ui")
        ui_dir = aura_ui_dir if os.path.isdir(aura_ui_dir) else BASE_DIR
        super().__init__(*args, directory=ui_dir, **kwargs)

    def log_message(self, format, *args):
        pass


def run_http_server(port: int = 5173):
    server = HTTPServer(("127.0.0.1", port), QuietHTTPHandler)
    server.serve_forever()


def run_os_controller_thread():
    """Runs the OS cursor hijacking loop in a dedicated background thread."""
    time.sleep(1.2)  # Give WebSocket server 1.2s to start
    try:
        controller = get_controller()
        asyncio.run(controller.run())
    except Exception as e:
        print(f"[Aura OS Controller] Notice: {e}")


def main():
    print("""
    ========================================================
             AURA: MULTIMODAL INTENT FUSION
             Native Desktop Accessibility Overlay
    ========================================================
    """)

    # 1. Start Local Assets Server
    http_thread = threading.Thread(target=run_http_server, args=(5173,), daemon=True)
    http_thread.start()
    print("[Aura Launcher] UI assets server running.")

    # 2. Start Python ML Engine (Vision + Audio + WebSocket)
    engine_server = AuraEngineServer()
    engine_thread = threading.Thread(target=engine_server.start, daemon=True)
    engine_thread.start()
    print("[Aura Launcher] ML Engine running (ws://127.0.0.1:8765).")

    # 3. Start Native OS Mouse Controller (macOS Quartz / Windows Win32)
    os_controller_thread = threading.Thread(target=run_os_controller_thread, daemon=True)
    os_controller_thread.start()
    print("[Aura Launcher] OS Mouse Controller initialized (Press SPACEBAR for emergency kill switch).")

    # 3.5. Start Voice Commander & Nova Voice Pet Engine
    try:
        from voice_commander import VoiceCommander
        voice_cmd = VoiceCommander()
        voice_cmd.start()
        print("[Aura Launcher] Voice Commander running (Say 'zoom', 'scroll down', 'click', 'hey nova').")
    except Exception as e:
        print(f"[Aura Launcher] Voice Commander warning: {e}")

    # 3.6. Launch Virtual Eye Cursor & Overlays
    sub_procs = []
    for mod_name in ["eye_cursor_overlay.py", "floating_pet.py", "camera_overlay.py"]:
        try:
            p = subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, mod_name)],
                cwd=BASE_DIR,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            )
            sub_procs.append(p)
            print(f"[Aura Launcher] Overlay '{mod_name}' started successfully.")
        except Exception as e:
            print(f"[Aura Launcher] Could not start overlay '{mod_name}': {e}")

    # 4. Launch Native Standalone Desktop Window
    ui_url = "http://127.0.0.1:5173"
    print("[Aura Launcher] Booting Standalone Desktop Window...")

    try:
        import webview
        window = webview.create_window(
            title="Aura — Multimodal Intent Fusion",
            url=ui_url,
            width=1320,
            height=860,
            resizable=True,
            min_size=(1024, 700),
            background_color="#0b0f19",
            easy_drag=False
        )
        webview.start()
    except Exception as e:
        print(f"[Aura Launcher] Webview fallback: {e}")
        import webbrowser
        webbrowser.open(ui_url)
        # Keep main thread alive if browser fallback
        while True:
            time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Aura Launcher] Application closed cleanly.")
        sys.exit(0)
