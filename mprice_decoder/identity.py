"""One display name per launch, with stable storage and release compatibility."""
import json
import secrets
from pathlib import Path

from .distribution import private_access

_private = private_access() is not None
APP_ID = "mprice_decoder" if _private else "MaplePrice"
DISPLAY_NAME = secrets.token_hex(6) if _private else "MaplePrice"
WINDOW_NAME = "MaplePrice"
COMPATIBILITY = json.loads((Path(__file__).resolve().parent / "assets" / "compatibility.json").read_text())


def data_directory():
    import os
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / COMPATIBILITY["data_directory"]


def set_console_title():
    import ctypes
    import sys
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleTitleW(DISPLAY_NAME)
