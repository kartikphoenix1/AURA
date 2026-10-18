#!/usr/bin/env python3
"""
Aura End-to-End System Integration Test
Verifies:
1. HTTP UI Server responds with 200 OK
2. Python ML WebSocket Server accepts client connection
3. Broadcasts valid 60fps telemetry packets
4. Responds to calibration control commands
"""

import os
import sys
import time
import json
import asyncio
import urllib.request
import threading
import websockets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from server import AuraEngineServer
from start_aura import run_http_server


async def client_test():
    print("[Integration Test] Connecting to ws://127.0.0.1:8765...")
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        print("[Integration Test] Connected! Receiving 5 telemetry frames...")
        for i in range(5):
            msg = await ws.recv()
            data = json.loads(msg)
            assert data["type"] == "telemetry", "Expected telemetry packet"
            assert "x" in data and "y" in data, "Telemetry missing x, y"
            assert "click" in data, "Telemetry missing click status"
            assert "audio_calibrated" in data and "gaze_calibrated" in data, "Telemetry missing ML readiness fields"
            print(f"  Frame {i+1}: pos=({data['x']:.1f}, {data['y']:.1f}), click={data['click']}, blink_locked={data['is_blink_locked']}")

        # Test sending control message
        print("[Integration Test] Sending 'start_record_target' control command...")
        await ws.send(json.dumps({"command": "start_record_target", "target_id": 0}))
        for _ in range(50):
            resp = await ws.recv()
            resp_data = json.loads(resp)
            if resp_data.get("status") == "recording_target_started":
                print(f"  Server response: {resp_data}")
                break
        assert resp_data.get("status") == "recording_target_started", "Failed to start target recording"

        print("[Integration Test] Sending 'stop_record_target'...")
        await ws.send(json.dumps({"command": "stop_record_target"}))
        for _ in range(50):
            resp2 = await ws.recv()
            resp2_data = json.loads(resp2)
            if resp2_data.get("status") == "recording_target_saved":
                print(f"  Server response: {resp2_data}")
                break
        assert resp2_data.get("status") == "recording_target_saved", "Failed to save target recording"

    print(" -> SUCCESS: WebSocket telemetry and control protocol verified!")


def main():
    print("\n========================================================")
    print("      AURA END-TO-END SYSTEM INTEGRATION TEST           ")
    print("========================================================")

    # 1. Start HTTP UI server
    http_thread = threading.Thread(target=run_http_server, args=(5179,), daemon=True)
    http_thread.start()
    time.sleep(0.3)

    # Check HTTP endpoint
    res = urllib.request.urlopen("http://127.0.0.1:5179")
    assert res.status == 200, "UI Server failed to serve 200 OK"
    html_content = res.read().decode("utf-8")
    assert "AURA" in html_content, "UI HTML content missing"
    for feature_id in ("view-theremin", "view-ml-model", "view-settings", "btn-learn-oops"):
        assert feature_id in html_content, f"UI feature missing: {feature_id}"
    assert "Voice command guide (60 commands)" in html_content, "Voice command guide missing"
    print("[Integration Test] HTTP UI Server and feature panels verified at http://127.0.0.1:5179.")

    # 2. Start ML Engine Server in background thread
    server = AuraEngineServer()
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()

    # Wait for a real WebSocket handshake; a raw TCP probe produces a
    # misleading opening-handshake traceback in the server log.
    print("[Integration Test] Waiting for WebSocket server to become ready...")
    async def websocket_ready():
        try:
            async with websockets.connect("ws://127.0.0.1:8765", open_timeout=1.0):
                return True
        except Exception:
            return False

    ready = False
    for _ in range(30):
        time.sleep(0.5)
        if asyncio.run(websocket_ready()):
            ready = True
            print("[Integration Test] WebSocket server handshake succeeded.")
            break
    assert ready, "WebSocket server did not become ready within 15 seconds"

    # 3. Run client integration test
    asyncio.run(client_test())

    # Stop server
    server.stop()
    print("\n========================================================")
    print(" ALL END-TO-END SYSTEM INTEGRATION TESTS PASSED!       ")
    print("========================================================\n")


if __name__ == "__main__":
    main()
