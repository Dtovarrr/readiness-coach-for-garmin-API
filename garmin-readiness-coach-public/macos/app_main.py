"""Started by Readiness Coach for Garmin.app (see launcher.c): single-instance and port checks, then run.py.

stdout/stderr already go to logs/readiness_coach.log. Keys still come from .env (server.py loads it).
"""
import datetime
import fcntl
import os
import runpy
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

from run import HOST, PORT  # noqa: E402  (after sys.path is set)

LOCK_FILE = PROJECT / "logs" / "app.lock"


def log(msg: str):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] launcher: {msg}", flush=True)


def alert(title: str, text: str, buttons: list[str]) -> int:
    """Native alert; returns the index of the button clicked (first button is the default)."""
    import AppKit
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    app.activateIgnoringOtherApps_(True)
    a = AppKit.NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    for b in buttons:
        a.addButtonWithTitle_(b)
    return a.runModal() - AppKit.NSAlertFirstButtonReturn


def single_instance():
    """Hold an exclusive lock for our lifetime. A second copy (e.g. `open -n`) raises the first and exits."""
    LOCK_FILE.parent.mkdir(exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            pid = int(os.pread(fd, 32, 0).decode().strip())
            import AppKit
            other = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if other:
                other.unhide()
                other.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
        except (ValueError, ImportError):
            pass
        log("already running; brought the existing window to the front")
        sys.exit(0)
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd  # keep it open: the lock is released when this process exits


def port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


def port_owner() -> tuple[int | None, str]:
    try:
        out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        pid = int(out[0])
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return pid, cmd
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, ""


def is_our_dashboard() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/", timeout=3) as r:
            return b"Readiness Coach" in r.read(4096)
    except OSError:
        return False


def stop(pid: int) -> bool:
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.time() + wait
        while time.time() < deadline:
            if not port_busy():
                return True
            time.sleep(0.2)
    return not port_busy()


def free_port():
    """Port 8765 is taken: offer to stop another copy of the dashboard, otherwise explain and quit."""
    pid, cmd = port_owner()
    log(f"port {PORT} in use by pid={pid} cmd={cmd!r}")
    if is_our_dashboard() and pid:
        choice = alert("Readiness Coach is already running elsewhere",
                       f"Another copy of the dashboard (PID {pid}, probably started from Terminal) "
                       f"is using port {PORT}.\n\nStop it and open Readiness Coach here?",
                       ["Stop It and Open", "Cancel"])
        if choice == 0 and stop(pid):
            log(f"stopped pid {pid}")
            return
        if choice == 0:
            alert("Couldn't free the port", f"PID {pid} is still using port {PORT}.", ["OK"])
    else:
        who = f"{Path(cmd.split()[0]).name if cmd else 'another program'} (PID {pid})" if pid else "another program"
        alert(f"Port {PORT} is in use",
              f"{who} is using port {PORT}, which Readiness Coach needs.\n\n"
              f"Quit it, then open Readiness Coach again. Details are in logs/readiness_coach.log.", ["OK"])
    log("exiting: port unavailable")
    sys.exit(1)


def restore_on_dock_click():
    """pywebview doesn't handle clicking the Dock icon while the window is minimised; add that."""
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def reopen(self, app, has_visible_windows):
            for w in app.windows():
                if w.isMiniaturized():
                    w.deminiaturize_(None)
            app.activateIgnoringOtherApps_(True)
            return True

        objc.classAddMethods(BrowserView.AppDelegate, [objc.selector(
            reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])
    except Exception as e:  # cosmetic; never block startup
        log(f"dock-reopen hook not installed: {e!r}")


if __name__ == "__main__":
    log(f"starting (pid {os.getpid()}, python {sys.version.split()[0]}, prefix {sys.prefix})")
    _lock = single_instance()
    if port_busy():
        free_port()
    restore_on_dock_click()
    sys.argv = [str(PROJECT / "run.py")]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        raise
    finally:
        log("window closed; server stopped with the app")
