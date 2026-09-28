"""Desktop shell (roadmap Phase 3.21): OS-native window via pywebview over the
local FastAPI server. Falls back to opening the default browser when the
native webview cannot start (headless environments, missing WebView2 etc.).
"""
from __future__ import annotations

import socket
import threading
import time
import webbrowser


def _wait_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def launch(port: int = 8787) -> None:
    import uvicorn

    from .api import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if not _wait_ready(port):
        raise RuntimeError(f"server did not come up on port {port}")

    url = f"http://127.0.0.1:{port}"
    try:
        import webview

        webview.create_window("SignalDesk", url, width=1280, height=860)
        webview.start()
    except Exception:
        webbrowser.open(url)
        print(f"Native window unavailable; opened {url} in your browser. Ctrl+C to quit.")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
