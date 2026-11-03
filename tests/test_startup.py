import logging
import io
import threading
import types
from pathlib import Path

from mprice_decoder import __main__ as app
from mprice_decoder import startup
from mprice_decoder.settings import Settings


def test_source_launch_uses_cozy_loading_screen_and_closes_it_once(monkeypatch):
    calls = []
    splash = types.SimpleNamespace(update_text=lambda text: calls.append(text), close=lambda: calls.append("closed"))
    monkeypatch.delattr(app.sys, "frozen", raising=False)
    monkeypatch.setattr(startup, "ProcessSplash", lambda: splash)
    monkeypatch.setattr(startup, "_splash", None)
    monkeypatch.setattr(startup, "_started", None)
    monkeypatch.setattr(startup, "_finished", threading.Event())
    monkeypatch.setattr(startup, "_report", lambda text: None)
    startup.begin()
    startup.begin()
    startup.phase("Opening the application windows...")
    startup.finish()
    startup.finish()
    assert calls == ["Preparing your price helper...", "Opening MaplePrice...", "Ready!", "closed"]


def test_missing_native_splash_falls_back_instead_of_disappearing(monkeypatch):
    splash = types.SimpleNamespace(update_text=lambda text: None, close=lambda: None)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setitem(app.sys.modules, "pyi_splash", types.SimpleNamespace(is_alive=lambda: False))
    monkeypatch.setattr(startup, "ProcessSplash", lambda: splash)
    monkeypatch.setattr(startup, "_splash", None)
    monkeypatch.setattr(startup, "_started", None)
    monkeypatch.setattr(startup, "_finished", threading.Event())
    monkeypatch.setattr(startup, "_report", lambda text: None)
    startup.begin()
    try:
        assert startup._splash is splash
    finally:
        startup.finish()


def test_native_loading_screen_has_friendly_updates_and_closes_when_ready(monkeypatch):
    updates, closed = [], []
    splash = types.SimpleNamespace(is_alive=lambda: True, update_text=updates.append,
                                   close=lambda: closed.append(True))
    monkeypatch.setitem(app.sys.modules, "pyi_splash", splash)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "stderr", None)
    monkeypatch.setattr(startup, "_terminal", None)
    monkeypatch.setattr(startup, "_splash", None)
    monkeypatch.setattr(startup, "_started", None)
    monkeypatch.setattr(startup, "_finished", threading.Event())
    monkeypatch.setattr(startup, "_configured", False)
    startup.begin()
    startup.phase("Loading OCR and image libraries (ONNX Runtime, OpenCV, NumPy)...")
    startup.phase("Opening the local item database...")
    startup.finish("Ready. Options: F10.")
    assert 'Preparing your price helper...' in updates
    assert 'Loading your saved prices...' in updates
    assert updates[-1] == 'Ready!' and closed == [True]
    assert not any('ONNX' in text or 'OpenCV' in text for text in updates)


def test_update_restart_is_windowless_and_loads_a_fresh_runtime(monkeypatch):
    calls = []
    monkeypatch.setenv("MAPLEPRICE_TEST", "retained")
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
    monkeypatch.setattr(app.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    app.restart_updated_app(Path('MaplePrice-2.5.exe'))
    args, kwargs = calls[0]
    assert args[0] == ['MaplePrice-2.5.exe', '--after-update']
    assert kwargs['creationflags'] == 0x08000000
    assert kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
    assert kwargs['env']['MAPLEPRICE_TEST'] == 'retained'
    assert 'PYINSTALLER_RESET_ENVIRONMENT' not in app.os.environ


def test_windowed_build_keeps_disk_logs_without_console_streams(monkeypatch, tmp_path):
    monkeypatch.setattr(app.sys, "stdout", None)
    monkeypatch.setattr(app.sys, "stderr", None)
    monkeypatch.setattr(startup, "_terminal", None)
    path = tmp_path / "log.txt"
    handlers = app.log_handlers(path)
    try:
        record = logging.LogRecord("windowed-test", logging.INFO, __file__, 0, "scan saved", (), None)
        for handler in handlers:
            handler.handle(record)
        assert "scan saved" in path.read_text()
        assert app.console_notice(Settings()) is None
        assert startup._terminal is None
    finally:
        for handler in handlers:
            handler.close()


def test_frozen_startup_log_never_waits_for_the_selected_console(monkeypatch, tmp_path):
    writing, release, completed = threading.Event(), threading.Event(), threading.Event()

    class PausedConsole:
        def write(self, text):
            writing.set()
            release.wait(5)

        def flush(self):
            pass

    terminal = startup.TerminalHandler(PausedConsole())
    monkeypatch.setattr(startup, "_terminal", terminal)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    path = tmp_path / "log.txt"
    handlers = app.log_handlers(path)

    def log():
        record = logging.LogRecord("startup-test", logging.INFO, __file__, 0, "startup continues", (), None)
        for handler in handlers:
            handler.handle(record)
        completed.set()

    producer = threading.Thread(target=log, daemon=True)
    try:
        producer.start()
        assert writing.wait(2)
        assert completed.wait(2), "application logging waited for the console"
        assert "startup continues" in path.read_text()
    finally:
        release.set()
        producer.join(2)
        for handler in handlers:
            handler.close()
        terminal.writer.join(2)


def test_console_selection_cannot_block_the_ui_thread(monkeypatch):
    writing, release = threading.Event(), threading.Event()

    def paused_write(*args, **kwargs):
        writing.set()
        assert release.wait(5)

    monkeypatch.setattr(app, "print", paused_write, raising=False)
    thread = app.console_notice(Settings())
    try:
        assert writing.wait(2)
        assert thread.is_alive()  # the console is still paused, but startup has returned
        assert thread.daemon
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_verbose_logs_still_reach_disk_when_terminal_queue_fills(monkeypatch, tmp_path):
    writing, release, completed = threading.Event(), threading.Event(), threading.Event()

    class PausedConsole:
        def write(self, text):
            writing.set()
            release.wait(5)

        def flush(self):
            pass

    terminal = startup.TerminalHandler(PausedConsole(), capacity=2)
    monkeypatch.setattr(startup, "_terminal", terminal)
    path = tmp_path / "log.txt"
    handlers = app.log_handlers(path)

    def log():
        for index in range(100):
            record = logging.LogRecord("startup-test", logging.INFO, __file__, 0, "phase %s", (index,), None)
            for handler in handlers:
                handler.handle(record)
        completed.set()

    producer = threading.Thread(target=log, daemon=True)
    try:
        producer.start()
        assert writing.wait(2)
        assert completed.wait(2), "a full terminal queue blocked application logging"
        assert terminal.messages.qsize() <= 2
        assert "phase 99" in path.read_text()
    finally:
        release.set()
        producer.join(2)
        for handler in handlers:
            handler.close()
        terminal.writer.join(2)


def test_startup_progress_is_visible_before_main_logging_is_configured(monkeypatch):
    written = threading.Event()

    class Terminal(io.StringIO):
        def flush(self):
            if "Ready." in self.getvalue():
                written.set()

    stream = Terminal()
    terminal = startup.TerminalHandler(stream)
    monkeypatch.setattr(startup, "_terminal", terminal)
    monkeypatch.setattr(startup, "_started", startup.time.monotonic())
    monkeypatch.setattr(startup, "_configured", False)
    monkeypatch.setattr(startup, "_finished", threading.Event())
    try:
        startup.phase("Loading OCR and image libraries...")
        startup.finish("Ready. Options: F10.")
        assert written.wait(2)
        output = stream.getvalue()
        assert "[startup +" in output
        assert output.index("Loading OCR") < output.index("Ready.")
        assert startup._finished.is_set()
    finally:
        terminal.close()
        terminal.writer.join(2)
