"""Launch the backend and open the dashboard in a native desktop window."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")  # before reading COACH_PORT, so run.py and server.py agree
HOST, PORT = "127.0.0.1", int(os.getenv("COACH_PORT") or 8765)  # server.py reads COACH_PORT too (Host/Origin checks)


def window_background() -> str:
    """Match the page's theme (Settings > Appearance, or the macOS appearance for "System"),
    so the window never flashes the wrong colour before the page paints."""
    try:
        theme = json.loads((Path(__file__).parent / ".data" / "settings.json").read_text()).get("theme", "system")
    except (OSError, ValueError):
        theme = "system"
    if theme == "system":
        try:
            dark = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True,
                                  timeout=2).stdout.strip() == "Dark"
        except (OSError, subprocess.SubprocessError):
            dark = False
        theme = "dark" if dark else "light"
    return "#0d0e11" if theme == "dark" else "#f4f5f7"  # = --bg in static/index.html


def serve():
    uvicorn.run("server:app", host=HOST, port=PORT, log_level="warning")


def wait_ready():
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex((HOST, PORT)) == 0:
                return
        time.sleep(0.1)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if "--browser" in sys.argv:  # headless / fallback mode
        threading.Thread(target=wait_ready, daemon=True).start()
        webbrowser.open(url)
        serve()
    else:
        threading.Thread(target=serve, daemon=True).start()
        wait_ready()
        try:
            import webview
            webview.create_window("Readiness Coach for Garmin", url, width=1360, height=900,
                                  min_size=(420, 600), background_color=window_background())
            webview.start()
        except ImportError:
            print("pywebview not installed - opening in your browser instead.")
            webbrowser.open(url)
            while True:
                time.sleep(3600)
