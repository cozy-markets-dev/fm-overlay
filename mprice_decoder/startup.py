"""Early startup progress and terminal logging that never waits on console I/O."""
import logging
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


class ProcessSplash:
    """Source launches have no PyInstaller splash. Keep Tk on its own main thread."""

    def __init__(self):
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--startup-splash"]
        else:
            executable = Path(sys.executable)
            windowed = executable.with_name("pythonw.exe")
            if sys.platform == "win32" and windowed.exists():
                executable = windowed
            command = [str(executable), str(Path(__file__).with_name("splash.py"))]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1", "PYINSTALLER_SUPPRESS_SPLASH_SCREEN": "1"},
        )
        atexit.register(self.close)

    def update_text(self, text):
        if self.process.poll() is None and self.process.stdin is not None:
            self.process.stdin.write(json.dumps(text) + "\n")
            self.process.stdin.flush()

    def close(self):
        atexit.unregister(self.close)
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)


def run_splash_child():
    """Called by frozen entry points before importing the application libraries."""
    if "--startup-splash" not in sys.argv:
        return
    from .splash import run
    run()
    raise SystemExit(0)


class TerminalHandler(logging.Handler):
    """A bounded queue keeps a selected or slow terminal off application threads."""

    def __init__(self, stream=None, capacity=1024):
        super().__init__()
        self.stream = sys.stderr if stream is None else stream
        self.messages = queue.Queue(maxsize=capacity)
        self.stopped = threading.Event()
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        self.writer = threading.Thread(target=self._write, daemon=True, name="terminal-log")
        self.writer.start()

    def emit(self, record):
        if self.stopped.is_set():
            return
        try:
            self.messages.put_nowait(self.format(record))
        except Exception:
            # A full queue or a bad log record must not block the UI or write to stderr here.
            pass

    def _write(self):
        while not self.stopped.is_set():
            try:
                text = self.messages.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                if self.stream is not None:
                    self.stream.write(text + "\n")
                    self.stream.flush()
            except (OSError, ValueError):
                self.stopped.set()
            finally:
                self.messages.task_done()

    def close(self):
        self.stopped.set()
        # Never join: the writer may be waiting for Windows console selection to end.
        super().close()


_terminal = None
_started = None
_phase = ""
_configured = False
_finished = threading.Event()
_state_lock = threading.Lock()
_splash = None


def _loading_text(message):
    message = message.lower()
    if message.startswith('ready.'):
        return 'Ready!'
    if 'options window' in message or 'application windows' in message:
        return 'Opening MaplePrice...'
    if 'dashboard' in message:
        return 'Preparing your market dashboard...'
    if 'database' in message:
        return 'Loading your saved prices...'
    if 'ocr' in message or 'libraries' in message:
        return 'Preparing your price helper...'
    return 'Getting MaplePrice ready...'


def terminal_handler():
    global _terminal
    with _state_lock:
        if _terminal is None and sys.stderr is None:
            return logging.NullHandler()
        if _terminal is None or _terminal.stopped.is_set():
            _terminal = TerminalHandler()
        return _terminal


def _report(message):
    log = logging.getLogger("mprice_decoder.startup")
    if _configured:
        log.info(message)
    else:
        record = logging.LogRecord(log.name, logging.INFO, __file__, 0, message, (), None)
        terminal_handler().handle(record)


def begin():
    global _started, _splash
    if _started is not None:
        return
    _started = time.monotonic()
    if getattr(sys, 'frozen', False):
        try:
            import pyi_splash
            _splash = pyi_splash if pyi_splash.is_alive() else None
        except (ImportError, OSError, RuntimeError):
            pass
    if _splash is None:
        try:
            _splash = ProcessSplash()
        except OSError:
            pass  # lack of a display must not stop the application
    _finished.clear()
    phase("Runtime unpacked. Loading application libraries...")

    def heartbeat():
        while not _finished.wait(2):
            _report(f"[startup +{time.monotonic() - _started:.1f}s] Still working: {_phase}")

    threading.Thread(target=heartbeat, daemon=True, name="startup-progress").start()


def logging_ready():
    global _configured
    _configured = True


def phase(message):
    global _phase
    if _started is None or _finished.is_set():
        return
    _phase = message
    if _splash is not None:
        try:
            _splash.update_text(_loading_text(message))
        except (OSError, RuntimeError):
            pass
    _report(f"[startup +{time.monotonic() - _started:.2f}s] {message}")


def finish(message="Ready. Options: F10."):
    if _started is None or _finished.is_set():
        return
    phase(message)
    _finished.set()
    if _splash is not None:
        try:
            _splash.close()
        except (OSError, RuntimeError):
            pass
