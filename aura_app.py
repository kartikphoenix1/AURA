"""
Aura Desktop Application Launcher
Launches:
- Telemetry & Vision Server (server.py)
- Native Windows Controller with Magnetic Snapping & Dwell Click (windows_controller.py)
- Voice Commander & Nova Voice Pet Engine (voice_commander.py)
- Native Desktop Window (via PyWebView Edge Chromium)
"""

import os
import sys
import time
import socket
import threading
import subprocess
import http.server
import socketserver
import webview

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTTP_PORT = 5173
WS_PORT = 8765


def is_port_in_use(port: int) -> bool:
    """Checks if a local TCP port is already open."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def ensure_http_server():
    """Runs a local static file HTTP server in a daemon thread if not running."""
    if is_port_in_use(HTTP_PORT):
        print(f"[AuraApp] HTTP server already active on port {HTTP_PORT}.")
        return

    os.chdir(_BASE_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    
    def run_server():
        with socketserver.TCPServer(("127.0.0.1", HTTP_PORT), handler) as httpd:
            print(f"[AuraApp] Serving static files at http://127.0.0.1:{HTTP_PORT}")
            httpd.serve_forever()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(0.5)


def ensure_server():
    """Ensures server.py is running for telemetry."""
    if is_port_in_use(WS_PORT):
        print(f"[AuraApp] Vision WebSocket server already active on port {WS_PORT}.")
        return

    print("[AuraApp] Starting Vision & Telemetry Server (server.py)...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_BASE_DIR, "server.py")],
        cwd=_BASE_DIR,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    # Wait for WebSocket server to bind
    for _ in range(30):
        if is_port_in_use(WS_PORT):
            print("[AuraApp] Vision Server bound successfully!")
            break
        time.sleep(0.3)


def start_windows_controller():
    """Starts windows_controller.py in background."""
    print("[AuraApp] Starting Windows Native Controller (windows_controller.py)...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_BASE_DIR, "windows_controller.py")],
        cwd=_BASE_DIR,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    return proc


def start_voice_commander():
    """Starts voice_commander.py in background."""
    print("[AuraApp] Starting Voice Commander & Nova Pet Engine (voice_commander.py)...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_BASE_DIR, "voice_commander.py")],
        cwd=_BASE_DIR,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    return proc


def main():
    print("=" * 60)
    print("🔮 LAUNCHING AURA NATIVE DESKTOP APPLICATION")
    print("=" * 60)

    ensure_http_server()
    ensure_server()

    # Launch background controllers
    win_ctrl = start_windows_controller()
    voice_ctrl = start_voice_commander()

    target_url = f"http://127.0.0.1:{HTTP_PORT}"
    print(f"[AuraApp] Opening native desktop window at {target_url}...")

    # Create native WebView window using Edge Chromium
    window = webview.create_window(
        title="Aura — Multimodal Voice Pet & Eye Control",
        url=target_url,
        width=1280,
        height=820,
        min_size=(900, 600),
        background_color="#0d0d12",
        text_select=False
    )

    try:
        webview.start(debug=False)
    finally:
        print("[AuraApp] Application closing. Terminating child controllers...")
        try:
            if win_ctrl and win_ctrl.poll() is None:
                win_ctrl.terminate()
            if voice_ctrl and voice_ctrl.poll() is None:
                voice_ctrl.terminate()
        except Exception:
            pass
        print("[AuraApp] Clean exit.")


if __name__ == "__main__":
    main()
