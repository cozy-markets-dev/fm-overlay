"""User options, saved next to the database so they survive restarts."""
import json
import logging
import re
from dataclasses import asdict, dataclass, field

from .data import DEFAULT_DB

PATH = DEFAULT_DB.parent / "settings.json"
log = logging.getLogger("mprice_decoder")
HOTKEYS = (("scan_key", "Scan shop"), ("clear_key", "Clear"), ("options_key", "This window"),
           ("hover_key", "Hover price check"), ("alert_key", "Add the hovered item to your price alerts"))


def hotkey_name(key):
    """"F8", "A", "7" for a key we can poll (F1-F24, a letter or digit), else None."""
    key = str(key).upper()
    return key if re.fullmatch(r"F([1-9]|1\d|2[0-4])|[A-Z0-9]", key) else None


OVERLAY_SCALES = (1.0, 1.25, 1.5)  # F10's size button steps through these
LANGUAGES = ("en", "zh")


def volume(value):
    """0-100 from a slider or a hand-edited file; None if it isn't a number."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return None


# A name on Cozy Markets: letters and digits, 2 to 13 of them, like an in-game name (the server's rule).
NAME = re.compile(r"[A-Za-z0-9]{2,13}")
ANONYMOUS = "Anonymous"


def share_name(text):
    """What to share scans under for what was typed in F10: a valid name, "" for anonymous
    (blank, or "Anonymous" in any case), or None if it isn't a name the board takes."""
    text = (text or "").strip()
    if not text or text.lower() == ANONYMOUS.lower():
        return ""
    return text if NAME.fullmatch(text) else None


@dataclass
class Settings:
    channel: int | None = None     # manual channel; None = ask the game's connection
    fm_room: int | None = None     # manual FM room; None = read it from the minimap
    show_labels: bool = True       # prices beside each shop row
    show_panel: bool = True        # detail cards right of the shop
    show_options: bool = True      # open the options window when mprice_decoder starts
    shop_prices: bool = True       # draw prices over shops (F10): off, shops aren't priced at all
    hover_prices: bool = False     # price the item tooltip under the cursor (F10, or hover_key)
    price_alerts: bool = True      # pop up Cozy Markets price alerts (signed in with Discord: alerts.py)
    alert_sound: bool = True       # ... with a chime (assets/alert.wav)
    alert_volume: int = 100        # ... this loud, 0-100 (F10's slider)
    language: str = "en"           # what the overlay and its windows say: "en" or "zh" (i18n.py, F10's 中/EN)
    overlay_scale: float = 1.0     # what's drawn over the game, this many times its size (OVERLAY_SCALES, F10)
    bazaar_sync: bool = True       # keep each character's equipment in your Bazaar stash (signed in: bazaar.py)
    bazaar_public: bool = False    # ... shown in the Bazaar's directory (off: only in your own stash)
    auto_scan: bool = True         # scan a shop by itself once it opens or its list changes
    share_scans: bool = True       # send shop scans to Cozy Markets (opt-out)
    share_name: str = ""           # the name shared scans go under on Cozy Markets; "" = anonymous
    cozy_token: str = field(default="", repr=False)  # hand-edit only, not in F10; empty = this PC's own token
    cozy_url: str = "https://cozy.markets"           # only changed (by hand) to test against a local server
    scan_key: str = "F8"
    clear_key: str = "F9"
    options_key: str = "F10"
    hover_key: str = "F7"         # toggles pricing whatever item tooltip is under the cursor
    alert_key: str = "F6"         # adds the item whose tooltip is under the cursor to your price alerts

    @classmethod
    def load(cls, path=PATH):
        try:
            data = json.loads(path.read_text())
            s = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            for field, _ in HOTKEYS:  # a hand-edited key we can't poll: back to the default
                setattr(s, field, hotkey_name(getattr(s, field)) or getattr(cls, field))
            s.alert_volume = volume(s.alert_volume) if volume(s.alert_volume) is not None else cls.alert_volume
            s.language = s.language if s.language in LANGUAGES else cls.language
            s.overlay_scale = s.overlay_scale if s.overlay_scale in OVERLAY_SCALES else cls.overlay_scale
            return s
        except FileNotFoundError:
            return cls()
        except Exception:
            log.exception("bad settings file %s, using defaults", path)
            return cls()

    def save(self, path=PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
