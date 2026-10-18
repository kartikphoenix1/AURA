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
import shutil
import subprocess
import urllib.error
import urllib.request

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
from http.server import HTTPServer, SimpleHTTPRequestHandler

os.environ["MPLCONFIGDIR"] = "/tmp/mpl"
sys.path.insert(0, BASE_DIR)

from server import AuraEngineServer
from system_controller import get_controller

_ollama_process = None
_voice_process = None


def ensure_local_voice_runtime():
    """Set local model/voice defaults and start Ollama when it is installed."""
    global _ollama_process
    os.environ.setdefault("AURA_LOCAL_MODEL", "qwen3.5:4b")
    os.environ.setdefault(
        "AURA_WHISPER_MODEL",
        os.path.join(BASE_DIR, "models", "voice", "whisper-small.en"),
    )
    os.environ.setdefault(
        "AURA_PIPER_VOICE",
        os.path.join(BASE_DIR, "models", "voice", "en_US-lessac-medium.onnx"),
    )

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=0.5):
            print("[Aura Launcher] Local Ollama is already running.")
            return
    except (urllib.error.URLError, TimeoutError):
        pass

    ollama = shutil.which("ollama")
    if not ollama and sys.platform == "darwin":
        app_cli = "/Applications/Ollama.app/Contents/Resources/ollama"
        if os.path.isfile(app_cli):
            ollama = app_cli
    if not ollama:
        print("[Aura Launcher] Ollama is not installed; local chat replies are unavailable.")
        return

    try:
        _ollama_process = subprocess.Popen(
            [ollama, "serve"], cwd=BASE_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            if _ollama_process.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=0.5):
                    print("[Aura Launcher] Local Ollama started.")
                    return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.25)
        print("[Aura Launcher] Ollama did not become ready; local chat replies may be unavailable.")
    except Exception as exc:
        print(f"[Aura Launcher] Could not start local Ollama: {exc}")


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


def start_background_services():
    """
    Starts OS controller + voice commander AFTER the webview Cocoa loop is running.
    On macOS, pynput's CGEventTap keyboard listener must not start before NSApplication
    claims the main run loop — doing so causes a Trace/BPT trap: 5 (SIGTRAP) crash.
    This function is passed as webview.start(func=...) so it runs in a background thread
    after Cocoa is fully initialized.
    """
    ensure_local_voice_runtime()

    # Start OS Mouse Controller
    os_thread = threading.Thread(target=run_os_controller_thread, daemon=True)
    os_thread.start()
    print("[Aura Launcher] OS Mouse Controller initialized (Press SPACEBAR for emergency kill switch).")

    # Keep PyAV/Whisper in a child process. OpenCV and PyAV each bundle
    # AVFoundation classes on macOS; loading both into the Cocoa UI process can
    # cause Objective-C duplicate-class crashes.
    global _voice_process
    try:
        _voice_process = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "voice_commander.py")],
            cwd=BASE_DIR,
        )
        print("[Aura Launcher] Local Flipping voice service started.")
    except Exception as e:
        print(f"[Aura Launcher] Voice Commander warning: {e}")


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

    # 3. Check macOS Accessibility Permissions (before webview starts)
    if sys.platform == "darwin":
        try:
            import ApplicationServices
            if not ApplicationServices.AXIsProcessTrusted():
                print("\n" + "=" * 65)
                print("⚠️  macOS ACCESSIBILITY PERMISSION REQUIRED")
                print("   To select apps, click the Dock, and control the Mac pointer,")
                print("   grant Accessibility permission in macOS System Settings.")
                print("   A system prompt was triggered for this terminal/app.")
                print("=" * 65 + "\n")
                ApplicationServices.AXIsProcessTrustedWithOptions({ApplicationServices.kAXTrustedCheckOptionPrompt: True})
        except Exception:
            pass

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
    # NOTE: OS controller + voice commander start INSIDE webview's func= callback
    # to avoid pynput CGEventTap vs Cocoa NSApplication race (Trace/BPT trap: 5)
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
        # Pass background services as func= so they start after Cocoa event loop is ready
        webview.start(func=start_background_services)
    except Exception as e:
        print(f"[Aura Launcher] Webview fallback: {e}")
        # Fallback: start background services immediately then open browser
        start_background_services()
        import webbrowser
        webbrowser.open(ui_url)
        # Keep main thread alive if browser fallback
        while True:
            time.sleep(1.0)
    finally:
        if _voice_process and _voice_process.poll() is None:
            _voice_process.terminate()
        if _ollama_process and _ollama_process.poll() is None:
            _ollama_process.terminate()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Aura Launcher] Application closed cleanly.")
        sys.exit(0)
