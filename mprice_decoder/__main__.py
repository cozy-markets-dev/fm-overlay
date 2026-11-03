"""MapleStory Classic shop price overlay, with Cozy Markets' prices (F10 opens the site).

    python -m mprice_decoder                 # shops scan as they open (or F8), F9 = clear, F10 = options,
                                         # F7 = price item tooltips as you hover (inventory, NPC shops)
    python -m mprice_decoder --image shot.png --out annotated.png   # offline test
"""
import argparse
import array
import base64
import ctypes
import ctypes.wintypes
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import startup

if __name__ == "__main__":
    startup.run_splash_child()
    startup.begin()

startup.phase("Loading process monitoring...")
import psutil

startup.phase("Loading OCR and image libraries (ONNX Runtime, OpenCV, NumPy)...")
from . import ocr
startup.phase("Loading market cache and update support...")
from . import update
from .sync import Sync
from .channel import ScreenChannel, read_channel
from .alerts import SHOWN, Alerts
from .auth import AuthError, AuthService, Login
from .annotate import Panel, Version, fmt, make_alert_card, make_card, make_label, make_price_card, scale_up, tier, usual
from .render import PilPainter, draw_badge, draw_labels, draw_panel
from .cozy_prices import CozyPrices
from .data import DEFAULT_DB, ItemDB
from .feed import Feed
from .i18n import set_language, t
from .icons import pick
from .ocr import read_lines
from .settings import HOTKEYS, Settings, hotkey_name
from .shop import merge_attempts, parse_listings
from .ui_child import taskbar_id
from .ui_host import Fallback, UiHost, WebAlert, WebOptions
from .version import VERSION
from .identity import APP_ID, COMPATIBILITY, DISPLAY_NAME, set_console_title

log = logging.getLogger("mprice_decoder")


def rss():
    """This process's memory in MB, for the log."""
    return psutil.Process().memory_info().rss // 2**20

def vk_code(key):
    key = key.upper()
    if key.startswith("F") and key[1:].isdigit():
        return 0x6F + int(key[1:])
    if len(key) == 1:
        return ord(key)
    raise SystemExit(f"Unsupported hotkey {key!r}; use F1-F24 or a single letter/digit")


STOP_FILE = DEFAULT_DB.parent / "stop"  # touch to quit (an admin instance can't be killed otherwise)
ALERT_SOUND = Path(__file__).resolve().parent / "assets" / "alert.wav"  # made by tools/alert_sound.py


_chimes = {}  # volume -> the chime's WAV at it


def alert_wav(volume, path=ALERT_SOUND):
    """The chime as a WAV file's bytes, at `volume` (0-100, settings.alert_volume): Windows' player has no volume of
    its own, so its samples are scaled. By the square of the volume, so the slider's middle sounds like half."""
    volume = max(0, min(100, int(volume)))
    if volume not in _chimes:
        with wave.open(str(path), "rb") as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
        samples = array.array("h", frames)  # 16-bit (tools/alert_sound.py)
        if sys.byteorder == "big":
            samples.byteswap()
        gain = (volume / 100) ** 2
        samples = array.array("h", (int(v * gain) for v in samples))
        if sys.byteorder == "big":
            samples.byteswap()
        out = tempfile.SpooledTemporaryFile()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(samples.tobytes())
        out.seek(0)
        _chimes[volume] = out.read()
    return _chimes[volume]


def chime(volume=100):
    """The price alerts' sound at `volume` (0-100), played without waiting for it to end (Windows' own player:
    from memory it can't play asynchronously, so it plays on a thread of its own)."""
    if volume <= 0:
        return
    try:
        import winsound
        sound = alert_wav(volume)
        threading.Thread(target=winsound.PlaySound, args=(sound, winsound.SND_MEMORY | winsound.SND_NODEFAULT),
                         daemon=True, name="chime").start()
    except Exception:  # not Windows, no sound device: the card still shows
        log.info("couldn't play the alert sound", exc_info=True)


def open_in_browser(url):
    """Via Explorer, so the browser doesn't inherit our admin rights. Explorer opens the Documents folder for a URL
    with a query string, so one of those goes in an Internet shortcut that Explorer opens instead."""
    try:
        if "?" in url:
            shortcut = Path(tempfile.gettempdir()) / f"{APP_ID}-link.url"
            shortcut.write_text(f"[InternetShortcut]\nURL={url}\n")
            url = str(shortcut)
        subprocess.Popen(["explorer.exe", url])
    except Exception:
        log.exception("couldn't open %s", url)


def restart_updated_app(payload):
    return subprocess.Popen(
        [str(payload), "--after-update"], cwd=payload.parent,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
    )


_instance_mutex = None


def disable_quick_edit():
    """A click in the console window starts selecting text there (QuickEdit: its title becomes "Select"), and
    that pauses whatever writes to it, so the whole overlay froze, mid-scan, until Esc. The console only shows
    the log (log.txt has it all): no selecting in it. Nothing happens without a console."""
    if sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [ctypes.wintypes.DWORD]
    kernel32.GetStdHandle.restype = ctypes.wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = ctypes.wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
    kernel32.SetConsoleMode.restype = ctypes.wintypes.BOOL
    handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    mode = ctypes.wintypes.DWORD()
    if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)  # QuickEdit off (ENABLE_EXTENDED_FLAGS on)


def log_handlers(log_path):
    """Persist every message and send a separate copy to the terminal queue."""
    handlers = [logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 2**20, backupCount=3, encoding="utf-8")]
    handlers.append(startup.terminal_handler())
    return handlers


def console_notice(settings):
    if sys.stdout is None:
        return None
    def write():
        try:
            print(f"{DISPLAY_NAME} {VERSION} running. {settings.scan_key} = scan shop, "
                  f"{settings.hover_key} = hover price check on/off, {settings.clear_key} = clear, "
                  f"{settings.options_key} = options.",
                  flush=True)
        except (OSError, ValueError):
            pass  # the console may already have closed; log.txt has the session details
    # WriteConsole can block while text is selected. Keep that wait off the Tk thread.
    thread = threading.Thread(target=write, daemon=True, name="console-notice")
    thread.start()
    return thread


def already_running():
    """One overlay at a time: a second copy would draw over the first and fight for hotkeys."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, COMPATIBILITY["instance_mutex"])
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def release_instance():
    """Let another copy start (the updated one)."""
    global _instance_mutex
    if _instance_mutex:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(_instance_mutex))
        _instance_mutex = None


MB_YESNO, MB_ICONWARNING, MB_ICONINFORMATION = 0x4, 0x30, 0x40
MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x10000, 0x40000, 6


def message(text, flags=MB_ICONINFORMATION):
    return ctypes.windll.user32.MessageBoxW(None, text, DISPLAY_NAME, flags | MB_TOPMOST | MB_SETFOREGROUND)


def offer_update(events):
    """In the background at startup: offer a newer release, and once it's
    downloaded and checked, ("restart", new exe). Exe builds only: from
    source there's nothing to swap."""
    if not getattr(sys, "frozen", False):
        return
    update.cleanup()
    try:
        manifest = update.check()
    except Exception:
        log.exception("update check failed")
        return
    if not manifest:
        return
    version = manifest["version"]
    notes = f"\n\nWhat's new:\n{manifest['notes']}" if manifest.get("notes") else ""
    if message(f"Version {version} is out (you have {VERSION}).{notes}\n\nUpdate now? "
               "It takes a minute and restarts by itself.", MB_YESNO | MB_ICONINFORMATION) != IDYES:
        log.info("update to %s declined", version)
        return
    log.info("updating to %s", version)
    tenths = [-1]

    def progress(done, total):
        pct = done * 100 // total if total else 0
        if pct // 10 != tenths[0]:
            tenths[0] = pct // 10
            events.put(("update_status", f"Downloading version {version}...  {pct}%"))

    exe = update.installed_exe()
    try:
        if update.download(manifest, exe.with_name(exe.name + ".update"), progress=progress):
            events.put(("restart", update.install(manifest, exe.with_name(exe.name + ".update"))))
            return
    except Exception:
        log.exception("update to %s failed", version)
    events.put(("update_status", None))
    message(f"The update didn't download properly, so you're still on {VERSION}.\n\n"
            "It will be offered again next time the app starts.", MB_ICONWARNING)


def optional(lookup, item_id):
    """Extras for the detail panel (info, icon) never fail a scan."""
    try:
        return lookup(item_id)
    except Exception:
        log.exception("%s failed for item %s", lookup.__name__, item_id)
        return None


def info_or_none(db, item_id):
    return optional(db.info, item_id)


def market_or_none(db, item_id):
    try:
        return db.market(item_id)
    except Exception:  # FM data is a nice-to-have; never fail the scan over it
        log.exception("FM lookup failed for item %s", item_id)
        return None


MAX_ATTEMPTS = 2
DEBUG_IMAGE = DEFAULT_DB.parent / "last_scan.png"


def save_debug_image(img):
    """On its own thread: a PNG of the whole game takes ~60 ms, kept off the scan's way."""
    def save():
        try:
            img.save(DEBUG_IMAGE)
        except Exception:  # only a debugging aid
            log.exception("couldn't save %s", DEBUG_IMAGE)
    threading.Thread(target=save, daemon=True, name="debug-image").start()


def read_shop(grab, tries=MAX_ATTEMPTS):
    """OCR the shop, and once more (fresh screenshot, upscaled) if any row's
    price wasn't read confidently. A shaky single read can be wrong (e.g.
    "1,499" -> "11,499"), so those only count once a second read agrees.
    None when there's no shop window on screen."""
    attempts = []
    for attempt in range(tries):
        img = grab()
        if attempt == 0:
            save_debug_image(img)
        lines = read_lines(img, attempt)
        if lines is None:
            log.info("no shop window found")
            return None
        attempts.append(parse_listings(lines))
        rows = merge_attempts(attempts, img)
        confirmed = sum(r.confirmed for r in rows)
        log.info("OCR attempt %d (memory %d MB): %d listings, %d rows so far (%d confirmed, %d sold): %s",
                 attempt + 1, rss(), len(attempts[-1]), len(rows), confirmed, sum(r.sold for r in rows),
                 [(l.name, l.qty, l.price) for l in attempts[-1]])
        # A cut-off price ("me...") won't read any better a second time.
        if rows and all(r.confirmed or r.uncertain for r in rows):
            break
    return rows


# Rows are looked up in parallel (ROWS), and each row fans its own requests out
# (FETCH). Separate pools, so a row waiting on its fetches can't starve them.
ROWS = ThreadPoolExecutor(max_workers=6, thread_name_prefix="row")
FETCH = ThreadPoolExecutor(max_workers=12, thread_name_prefix="fetch")


def narrow_by_icon(db, row, match):
    """Cut-off names (scroll tiers etc.) match several items; pick the one whose
    icon colour matches the icon in the shop row, if it's a clear winner."""
    try:
        colors = list(FETCH.map(db.icon_color, [i.id for i in match.items]))
    except Exception:
        log.exception("icon lookup failed")
        return
    idx = pick(row.icon_color, colors)
    log.info("icon match for %s: %s", row.names[0],
             match.items[idx].name if idx is not None else "ambiguous")
    if idx is not None:
        match.items = [match.items[idx]]


def cozy_or_none(item_id):
    try:
        return cozy_prices.get(item_id) if cozy_prices is not None else None
    except Exception:  # Cozy Markets' price is a nice-to-have, like MeowDB's
        log.exception("Cozy Markets price lookup failed for item %s", item_id)
        return None


def versions_of(db, items):
    markets = FETCH.map(lambda i: market_or_none(db, i.id), items)
    return [Version(i, m, cozy_or_none(i.id)) for i, m in zip(items, markets)]


def look_up(db, row):
    """Everything about one shop row: (listing, match, versions, seen, siblings, info, same), from the OCR's
    readings of its name (`same` is always None: it needs an equip's exact stats)."""
    # Try each spelling the OCR produced until one matches an item.
    name = row.names[0]
    match = db.lookup(name)
    for alt in row.names[1:]:
        if match.found:
            break
        name, match = alt, db.lookup(alt)
    if not match.found:
        log.info("no item matched any reading of row: %s", row.names)
    if len(match.items) > 1:
        narrow_by_icon(db, row, match)
    listing = row.listing(name)
    seen, siblings, info = None, [], None
    if len(match.items) == 1:  # only track prices when we know exactly which item it is
        item = match.items[0]
        info_job = FETCH.submit(info_or_none, db, item.id)
        if row.confirmed:  # never let a one-off misread into the 24h history
            db.record_seen(item.id, listing.price / listing.qty)
            seen = db.seen(item.id)
        if tier(item.name):
            others = db.lookup(item.name.rsplit(": ", 1)[0] + ": ...").items
            siblings = versions_of(db, [i for i in others if i.id != item.id])
        info = info_job.result()
    return listing, match, versions_of(db, match.items), seen, siblings, info, None


def panel_box(rows):
    """Where the detail panel goes: right of the shop window, from its top edge
    (sliding up if it needs room) down to its bottom edge."""
    box = ocr.last_list_box
    if box:  # shop window edges relative to its item list
        return dict(left=box[0] + 529, top=box[1] - 136, right=10_000, bottom=box[3] + 64)
    return dict(left=rows[0].x + 485, top=rows[0].y - 145, right=10_000, bottom=rows[-1].bottom + 70)


last_map = None  # map name from the last minimap that showed one: only for the F10 hint
last_channel = None  # channel from the last time the game was connected to one
screen_channel = ScreenChannel()  # the channel as the Change Channel window last showed it (watch_shop follows it)


def detect_channel():
    """Your channel: the game's connection, then the Change Channel window (a VPN or ping reducer hides the
    connection). None when neither tells."""
    return read_channel() or screen_channel.channel
cozy_prices = None  # CozyPrices, while the overlay runs: Cozy Markets' prices for the cards


def read_location(grab):
    """(shop title, seller, your character's name) for the current screen. One job,
    so the OCR engine is never used from two threads at once."""
    try:
        img = grab()
        title, seller = ocr.read_header(img)
    except Exception:
        log.exception("couldn't read the shop's title/seller")
        return None, None, None
    try:
        player = ocr.read_player(img)
    except Exception:
        log.exception("couldn't read your character's name")
        player = None
    return title, seller, player


def read_map(grab):
    try:
        map_name = ocr.read_map(grab())
        log.info("minimap: %r (memory %d MB)", map_name, rss())
        return map_name
    except Exception:
        log.exception("couldn't read the minimap")
        return None


def where(settings, detected_map, detected_channel=None):
    """(channel, FM room, map name): a manual setting wins, then what was detected.
    The channel falls back to the last one seen (the connection only drops while
    changing channel), but the map never does: with the minimap hidden you may
    have moved, so it's unknown. FM room is None outside the Free Market."""
    global last_map, last_channel
    if detected_map is not None:
        last_map = detected_map
    if detected_channel is not None:
        last_channel = detected_channel
    channel = settings.channel if settings.channel is not None else last_channel
    if settings.fm_room is not None:
        return channel, settings.fm_room, f"Free Market <{settings.fm_room}>"
    return channel, ocr.fm_room_of(detected_map), detected_map


def where_text(channel, room, map_name=None):
    place = f"FM {room}" if room else map_name or "Map ?"
    return f"Ch {channel or '?'} \u00b7 {place}"


def blocked_reason(channel, room, map_name):
    """Why we won't log a shop here, or None. A shop logged without its channel and
    map can't be found again, and a guessed one would be false data."""
    if map_name is None:
        return t("Please expand the minimap")
    if channel is None:
        return t("Channel unknown: open Change Channel once")
    return None


def log_scan(db, title, seller, rows, results, channel=None, room=None, map_name=None, player=None):
    """Add this OCR scan to the market log, shared with Cozy Markets (sync.py)."""
    entries = []
    for row, (listing, match, *_rest) in zip(rows, results):
        item_id = match.items[0].id if len(match.items) == 1 else None
        entries.append((item_id, listing.name, listing.qty, listing.price, row.confirmed, row.icon, row.sold))
    try:
        db.record_scan(title, seller, entries, channel, room, map_name, scanned_by=player, source="backup")
        log.info("logged OCR scan of %r by %r at %s, scanned by %r: %d listings",
                 title, seller, where_text(channel, room, map_name), player, len(entries))
    except Exception:
        log.exception("couldn't log the scan")


def log_when_read(db, location, rows, results, place):
    title, seller, player = location.result()
    log_scan(db, title, seller, rows, results, *place, player=player)


def scan(grab, db, settings=None):
    """OCR the shop's rows (where they are on screen), then look everything up by what the OCR read.
    Returns (row labels, detail panel, "Ch 5 · FM 2" or "Ch 5 · Henesys Market",
    why it didn't scan or None). Without knowing where we are (a VPN or ping reducer hides the channel's port),
    the shop is still priced but not logged."""
    settings = settings or Settings()
    read = read_map(grab) if settings.fm_room is None else None  # a manual room needs no minimap
    detected_map = ocr.known_map(read)  # a map of the game's, or None: shops aren't saved with a garbled one
    if read and not detected_map:
        log.info("minimap read %r isn't a map we know", read)
    place = where(settings, detected_map, detect_channel())
    unplaced = t("Can't read the map") if read and not detected_map else blocked_reason(*place)  # why it isn't logged
    if unplaced:
        log.info("pricing at %s without logging: %s", where_text(*place), unplaced)
    # All OCR finishes before any MeowDB call.
    rows = read_shop(grab, MAX_ATTEMPTS)
    if rows is None:
        return [], None, where_text(*place), t("No shop window found")
    started = time.perf_counter()
    # The shop's title and seller are only read to log the scan: one we can't place isn't logged at all.
    logging_it = rows and not unplaced
    location = FETCH.submit(read_location, grab) if logging_it else None  # runs while lookups wait
    results = list(ROWS.map(lambda r: look_up(db, r), rows))
    log.info("lookups took %.2fs for %d rows (memory %d MB)", time.perf_counter() - started, len(rows), rss())
    if location:  # logged once its title and seller are read: after the prices are drawn, not before
        FETCH.submit(log_when_read, db, location, rows, results, place)

    labels = []
    panel = Panel(**panel_box(rows)) if rows else None
    for row, (listing, match, versions, seen, siblings, info, same) in zip(rows, results):
        labels.append(make_label(listing, match, versions, seen, siblings))
        if row.sold:  # can't buy it: no card, the label says SOLD
            continue
        card = make_card(listing, match, versions, seen, info, siblings, same)
        same = next((c for c in panel.cards if c.key() == card.key()), None)
        if same:  # e.g. two identical stacks of arrows: one card, "x2"
            same.count += 1
            continue
        # The clean icon (bundled with mprice_decoder) once we know the item; else the shop's own,
        # cropped from the screenshot. Neither is a download.
        known = match.items[0].id if len(match.items) == 1 else None
        card.icon = (db.stored_icon(known) if known else None) or row.icon
        panel.cards.append(card)
    text = where_text(*place)
    if unplaced:  # and why, so they can fix it
        text += " \u00b7 " + t("not saved: {why}", why=unplaced)
    return labels, panel, text, None


def identify(db, names):
    """(name, data.Match) for an item tooltip's name (OCR readings, best first)."""
    name, match = names[0], db.lookup(names[0])
    for alt in names[1:]:
        if match.found:
            break
        name, match = alt, db.lookup(alt)
    return name, match


def price_card(db, names):
    """The hover card for an item tooltip's name (OCR readings, best first)."""
    name, match = identify(db, names)
    same = None
    log.info("hovered %s -> %s", names, [i.name for i in match.items])
    info, siblings = None, []
    if match.found:
        item = match.items[0]
        info = optional(db.info, item.id)
        if tier(item.name):
            others = db.lookup(item.name.rsplit(": ", 1)[0] + ": ...").items
            siblings = versions_of(db, [i for i in others if i.id != item.id])
    card = make_price_card(name, match, versions_of(db, match.items), info, siblings, same)
    if match.found:
        card.icon = optional(db.icon_png, match.items[0].id)
    return card


def hovered_item(db, rect):
    """(data.Item, what it usually goes for or None) for the item whose tooltip is under the cursor, or None."""
    from . import capture
    x, y = cursor_pos()
    img, scale = capture.grab_game(rect)
    found = ocr.read_tooltip(img, (round((x - rect[0]) / scale), round((y - rect[1]) / scale)))
    if not found:
        return None
    _, match = identify(db, found[0])
    if not match.found:
        return None
    item = match.items[0]
    return item, usual(versions_of(db, [item])[0])


HOVER_POLL = 0.08    # s between cursor checks
HOVER_SETTLE = 0.15  # the cursor rests this long before we look for a tooltip
HOVER_RETRY = 1.0    # keep looking this long after it stops: the tooltip can lag
HOVER_JITTER = 6     # px the cursor can drift before the card is hidden
HOVER_KEEP = 60      # s a hover card is reused before its prices are looked up again


def cursor_pos():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def watch_hover(current_rect, db, events, enabled):
    """While `enabled`, price the item tooltip under the resting cursor:
    ("hover", (card, dot, rect)) when one shows up, ("hover", None) once the
    cursor moves off it. Cards are kept for HOVER_KEEP, so going back to an item is instant."""
    from . import capture
    cards = {}
    pos, since, done, shown_at = None, 0.0, False, None
    while True:
        time.sleep(HOVER_POLL)
        if not enabled.is_set():
            pos, shown_at = None, None
            continue
        now, here = time.monotonic(), cursor_pos()
        if here != pos:
            pos, since, done = here, now, False
            if shown_at and max(abs(here[0] - shown_at[0]), abs(here[1] - shown_at[1])) > HOVER_JITTER:
                events.put(("hover", None))
                shown_at = None
            continue
        if done or now - since < HOVER_SETTLE:
            continue
        try:
            rect = current_rect()
            img, scale = capture.grab_game(rect)
            found = ocr.read_tooltip(img, (round((pos[0] - rect[0]) / scale), round((pos[1] - rect[1]) / scale)))
            if not found:
                done = now - since > HOVER_RETRY
                continue
            done = True
            names, dot = found
            key = names[0]
            made, card = cards.get(key, (None, None))
            if made is None or now - made > HOVER_KEEP:
                card = price_card(db, names)
                cards[key] = now, card
            events.put(("hover", (card, (round(dot[0] * scale), round(dot[1] * scale)), rect, scale)))
            shown_at = pos
        except Exception:
            log.exception("hover price check failed")
            done = True


class ShopWatch:
    """When to scan a shop by itself (debounced). A shop is scanned once its
    list has looked the same for STABLE seconds, so the opening animation or a
    scroll settles first, and again only when the list changes. It counts as
    closed once it's been gone for GONE seconds, so one bad look doesn't clear it."""
    STABLE = 0.3
    GONE = 0.8

    def __init__(self):
        self.anchor = None         # where the shop's buttons are
        self.prev = None           # list fingerprint at the last look
        self.since = 0.0           # when the list last changed
        self.scanned = None        # list fingerprint at the last scan
        self.missing_since = None  # when the shop was last seen gone

    def update(self, now, anchor, fingerprint, settle=None):
        """One look at the screen (anchor None: no shop). "scan", "closed" or None. `settle`: how long the list
        must look the same first (STABLE unless said)."""
        settle = self.STABLE if settle is None else settle
        if anchor is None:
            if self.anchor is None:
                return None
            self.missing_since = self.missing_since or now
            if now - self.missing_since < self.GONE:
                return None
            self.__init__()
            return "closed"
        self.missing_since = None
        if anchor != self.anchor or self.prev is None or ocr.list_changed(self.prev, fingerprint):
            self.since = now
        self.anchor, self.prev = anchor, fingerprint
        if now - self.since >= settle and (self.scanned is None or ocr.list_changed(self.scanned, fingerprint)):
            self.scanned = fingerprint
            return "scan"
        return None


SHOP_POLL = 0.2   # s between looks for a shop window


def watch_shop(current_rect, events, enabled, busy):
    """While `enabled()`, scan shops as they open or change: ("auto_scan", None),
    and ("shop_closed", None) once the shop's gone."""
    import numpy as np
    from . import capture
    watch = ShopWatch()
    while True:
        time.sleep(SHOP_POLL)
        if not enabled():
            watch = ShopWatch()
            continue
        if busy.is_set():  # a scan is reading the screen: don't judge it half-drawn
            continue
        try:
            rect = current_rect()
            img, scale = capture.grab_game(rect)
            rgb = np.array(img)
            screen_channel.follow(rgb, time.monotonic())
            x, y = cursor_pos()
            cursor = round((x - rect[0]) / scale), round((y - rect[1]) / scale)
            anchor = ocr.find_shop(rgb, watch.anchor)
            if anchor and ocr.find_tooltip_dot(rgb, cursor, anywhere=False):
                continue  # a tooltip is over the list: wait until it's gone
            fingerprint = ocr.list_fingerprint(rgb, anchor, cursor) if anchor else None
            what = watch.update(time.monotonic(), anchor, fingerprint)
            if what == "scan":
                log.info("shop list settled at %s: auto scan", anchor)
                events.put(("auto_scan", None))
            elif what == "closed":
                events.put(("shop_closed", None))
        except Exception:
            log.exception("shop watch failed")
            time.sleep(2)


def foreground_title():
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
    return buf.value


def listen_hotkeys(bindings, events, paused=lambda key: False):
    """Poll key state instead of RegisterHotKey: games often suppress normal
    hotkeys (raw input with RIDEV_NOHOTKEYS) while they have focus.
    `bindings()` is read on every poll, so a rebound key works at once; a key
    doesn't fire while `paused(key)` (the options window is waiting for a new key)."""
    user32 = ctypes.windll.user32
    was_down = {}
    while True:
        for key, name in bindings():
            vk = vk_code(key)
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not was_down.get(vk, down) and not paused(key):  # first look at a key: just note it
                log.info("key %s pressed (foreground: %r)", name, foreground_title())
                events.put((name, None))
            was_down[vk] = down
        time.sleep(0.03)


def run_overlay(args, db):
    startup.phase("Loading the window toolkit and overlay...")
    import tkinter as tk
    from . import capture
    from .overlay import AlertDialog, OptionsWindow, Overlay

    startup.phase("Creating the application windows and loading fonts...")
    taskbar_id()
    root = tk.Tk(className=DISPLAY_NAME)
    root.title(DISPLAY_NAME)
    try:  # Cozy, the pet, on every Tk window (the React ones take it in ui_child.py)
        root.iconbitmap(default=str(Path(__file__).resolve().parent / "assets" / "mapleprice.ico"))
    except tk.TclError:
        log.info("couldn't set the window icon", exc_info=True)
    # Tk prints a drawing error to the console and carries on (an empty panel): log it too
    root.report_callback_exception = lambda kind, err, tb: log.error("overlay error", exc_info=(kind, err, tb))
    ov = Overlay(root)
    events = queue.Queue()
    busy = threading.Event()
    startup.phase("Reading saved options...")
    settings = Settings.load()
    set_language(settings.language)
    ov.size = settings.overlay_scale
    for field, _ in HOTKEYS:  # --scan-key etc. override the saved keys
        if getattr(args, field):
            key = hotkey_name(getattr(args, field))
            if not key:
                raise SystemExit(f"Unsupported hotkey {getattr(args, field)!r}; use F1-F24 or a single letter/digit")
            setattr(settings, field, key)
    shown = {}  # last scan result, so toggling an option redraws it
    keys = lambda: t("{clear} clear \u00b7 {options} options", clear=settings.clear_key, options=settings.options_key)
    hovering = threading.Event()  # hover price checks are on (settings.hover_prices, set by apply_switches)

    def show_result():
        if "labels" not in shown:
            return
        ov.clear()
        if settings.show_labels:
            ov.draw(shown["labels"])
        if settings.show_panel and shown["panel"]:
            ov.draw_panel(shown["panel"])
        n = len(shown["labels"])
        if n:
            ov.status(t("{where}  \u00b7  {n} items  |  {keys}", where=shown["where"], n=n, keys=keys()))
        else:
            ov.status(t("{where}  \u00b7  no shop listings found  |  {keys}", where=shown["where"], keys=keys()), "warn")
        log.info("drawn (memory %d MB)", rss())

    sync = Sync(db, settings)
    db.on_scan = sync.notify
    global cozy_prices
    cozy_prices = CozyPrices(settings, reader=sync)

    def apply_switches():
        """Make the overlay match the F10 switches."""
        if not settings.shop_prices and shown:
            shown.clear()
            ov.clear()
        if not settings.price_alerts:
            ov.clear_alerts()
        set_language(settings.language)  # what's drawn from now on (a scan's cards: from the next one)
        if ov.size != settings.overlay_scale:  # show_result redraws what's shown at the new size
            ov.size = settings.overlay_scale
            ov.hover_card()
            ov.clear_alerts()
        if settings.hover_prices and not hovering.is_set():
            hovering.set()
            ov.place(current_rect())
        elif not settings.hover_prices and hovering.is_set():
            hovering.clear()
            ov.hover_card()

    def on_settings_change():
        settings.save()
        log.info("settings changed: %s", settings)
        apply_switches()
        show_result()
        sync.notify()  # sharing switched on/off: act on it now

    try:  # optional: MaplePrice works the same signed out
        signin = Login(AuthService(settings.cozy_url), lambda: events.put(("signin", None)), open_in_browser,
                       before_sign_out=lambda: sync.unlink_owner(signin.service))
    except ValueError:  # a hand-edited cozy_url that isn't a site's address
        log.warning("Discord sign-in is off: %r isn't a site it can sign in to", settings.cozy_url)
        signin = None
    alerts = Alerts(signin.service, settings, events) if signin else None
    alert_dialog = None  # ui_host.WebAlert (or overlay.AlertDialog), while one's open

    open_site = lambda: open_in_browser(settings.cozy_url.rstrip("/") + "/market")  # the market, on Cozy Markets
    open_alerts = (lambda: open_in_browser(signin.service.origin + "/alerts")) if signin else None
    quit_app = lambda: events.put(("quit", None))
    tk_options = OptionsWindow(root, settings, on_settings_change, lambda: last_map, detect_channel,
                               open_site=open_site, sync_status=sync.status,
                               on_quit=quit_app, signin=signin, open_alerts=open_alerts,
                               test_sound=lambda: chime(settings.alert_volume))
    # F10 and adding an alert in the website's React (ui_child.py, its own process); the Tk windows if it can't start
    startup.phase("Starting the options window...")
    ui = UiHost(lambda msg: events.put(("ui", msg)))
    web = WebOptions(ui, settings, on_settings_change, open_site=open_site, on_quit=quit_app, signin=signin,
                     open_alerts=open_alerts, later=root.after,
                     unwatch=lambda watch_id: unwatch(watch_id),
                     set_max=lambda watch_id, price: set_max(watch_id, price),
                     test_sound=lambda: chime(settings.alert_volume)) if ui.start() else None
    options = Fallback(web, tk_options)

    def show_alerts(hits):
        """New price alerts, newest first, at the game's top right."""
        cards = []
        for hit in hits[::-1][:SHOWN]:
            card = make_alert_card(hit)
            card.icon = optional(db.stored_icon, hit.get("itemId"))
            cards.append(card)
        ov.place(current_rect())
        ov.alert_cards(cards)
        if settings.alert_sound:
            chime(settings.alert_volume)
        if len(hits) > SHOWN:
            ov.flash(t("{n} more price alerts: {key} > My alerts", n=len(hits) - SHOWN, key=settings.options_key),
                     "info", 3000)

    def find_alert_item(rect):
        try:
            found = hovered_item(db, rect)
        except Exception:
            log.exception("couldn't read the hovered item for an alert")
            found = None
        events.put(("alert_found", found) if found else
                   ("alert_none", f"Rest the cursor on an item until its tooltip shows, then press {settings.alert_key}"))

    icons = {}  # item id -> its icon as a data: URL, for the watch list

    def icon_url(item_id):
        if item_id not in icons:
            png = optional(db.stored_icon, item_id) if isinstance(item_id, int) else None
            icons[item_id] = "data:image/png;base64," + base64.b64encode(png).decode() if png else None
        return icons[item_id]

    def refresh_watches():
        """The items you watch, asked of Cozy Markets on a thread of its own, then shown in F10."""
        def run():
            try:
                watches, most = alerts.watching()
            except AuthError as e:
                log.warning("couldn't get the watch list: %s", e)
                return
            events.put(("watching", {"most": most, "items": [
                {"id": w["id"], "itemId": w.get("itemId"), "name": w.get("itemName") or "Item",
                 "maxPrice": w.get("maxPrice"), "icon": icon_url(w.get("itemId"))} for w in watches]}))
        if alerts and signin and signin.user:
            threading.Thread(target=run, daemon=True, name="watches").start()

    def set_max(watch_id, price):
        def run():
            log.info("watch %s: at most %s", watch_id, price)
            try:
                alerts.change(watch_id, price)
                events.put(("watch_note", ""))
            except AuthError as e:
                log.warning("couldn't change watch %s to %s: %s", watch_id, price, e)
                events.put(("watch_note", str(e)))
            except Exception:
                log.exception("changing watch %s failed", watch_id)
                events.put(("watch_note", "Couldn't change it. Please try again."))
            refresh_watches()  # the server's list: the new price, or the old one back
        threading.Thread(target=run, daemon=True, name="set-max").start()

    def unwatch(watch_id):
        def run():
            log.info("watch %s: removed", watch_id)
            try:
                alerts.remove(watch_id)
            except AuthError as e:
                log.warning("couldn't stop watching %s: %s", watch_id, e)
            except Exception:
                log.exception("removing watch %s failed", watch_id)
            refresh_watches()
        threading.Thread(target=run, daemon=True, name="unwatch").start()

    def add_alert(dialog, item, price):
        try:
            alerts.add(item.id, item.name, price)
            refresh_watches()
            events.put(("alert_result", (dialog, True, t("Added: you'll get an alert when it's {price} or less.",
                                                          price=fmt(price)))))
        except AuthError as e:
            events.put(("alert_result", (dialog, False, str(e))))
        except Exception:
            log.exception("adding a price alert failed")
            events.put(("alert_result", (dialog, False, t("Couldn't add the alert. Please try again."))))

    def ask_alert(item, worth):
        """The add-an-alert dialog for `item`, its price box filled in with what it usually goes for."""
        nonlocal alert_dialog
        if alert_dialog and alert_dialog.is_open():
            alert_dialog.close()
        suggested = f"{int(worth):,}" if worth else ""
        if options.current is web:
            alert_dialog = WebAlert(web, item.name, suggested, f"~{fmt(worth)}" if worth else None,
                                    optional(db.stored_icon, item.id))
        else:
            alert_dialog = AlertDialog(root, item.name, suggested,
                                       f"Usually ~{fmt(worth)} on Cozy Markets." if worth else "")
        dialog = alert_dialog
        dialog.submit = lambda price: threading.Thread(target=add_alert, args=(dialog, item, price), daemon=True,
                                                       name="alert-add").start()

    def current_rect():
        hwnd = capture.find_window(args.title)
        return capture.client_rect(hwnd) if hwnd else capture.primary_monitor_rect()

    def worker(rect):
        try:
            scale = [1.0]

            def grab():  # the game at its own size, however big it's shown (ocr.game_view)
                img, scale[0] = capture.grab_game(rect)
                return img
            labels, panel, place, blocked = scan(grab, db, settings)
            if blocked:
                events.put(("blocked", blocked))
                return
            labels, panel = scale_up(labels, panel, scale[0])
            log.info("scan found %d listings at %s (memory %d MB): %s", len(labels), place,
                     rss(), [l.text for l in labels])
            events.put(("result", (labels, panel, place)))
        except Exception as e:  # surface network/OCR failures on screen
            log.exception("scan failed")
            events.put(("error", t("Scan failed: {error}", error=e)))
        finally:
            busy.clear()

    def start_scan():
        if busy.is_set():
            return
        busy.set()
        ov.clear()  # don't OCR our own labels
        rect = current_rect()
        log.info("scanning window rect %s (memory %d MB)", rect, rss())
        ov.place(rect)
        ov.status(t("Scanning..."))  # top-centre badge: clear of the minimap and the shop
        threading.Thread(target=worker, args=(rect,), daemon=True).start()

    def poll():
        if STOP_FILE.exists():
            STOP_FILE.unlink()
            log.info("stop file found, quitting")
            root.destroy()
            return
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "scan":
                    if settings.shop_prices:
                        start_scan()
                    else:
                        ov.place(current_rect())
                        ov.flash(t("Shop prices are off: switch them on in {key}", key=settings.options_key), "info", 2500)
                elif kind == "auto_scan":
                    if settings.auto_scan and settings.shop_prices:  # might have been switched off since
                        start_scan()
                elif kind == "shop_closed":
                    if shown:  # its prices would be left floating over the game
                        shown.clear()
                        ov.clear()
                elif kind == "clear":
                    shown.clear()
                    ov.clear()
                elif kind == "options":
                    options.toggle()
                    refresh_watches()  # fresh when it opens (added on the website meanwhile, say)
                elif kind == "signin":
                    options.refresh()
                    sync.owner = signin.service if signin.user else None
                    if signin.user:  # this PC's shared shops show in the account's My scans
                        threading.Thread(target=sync.link_owner, args=(signin.service,), daemon=True,
                                         name="link").start()
                    if signin.user and web is not None and web.watching is None:
                        refresh_watches()
                elif kind == "watching":
                    if web is not None:
                        web.watching = payload
                        options.refresh()
                elif kind == "watch_note":
                    if web is not None:
                        web.watch_note = payload
                        options.refresh()
                elif kind == "ui":
                    if payload.get("event") == "gone":
                        log.warning("the React windows are gone: F10 opens the Tk one")
                    elif web is not None:
                        web.handle(payload)
                elif kind == "alerts":
                    if settings.price_alerts:  # not switched off since
                        show_alerts(payload)
                elif kind == "alerts_unread":
                    if settings.price_alerts:
                        ov.place(current_rect())
                        ov.flash(t("1 unread price alert: {key} > My alerts" if payload == 1 else
                                   "{n} unread price alerts: {key} > My alerts", n=payload, key=settings.options_key),
                                 "info", 3000)
                elif kind == "alert_item":
                    if signin is None or signin.user is None:
                        ov.place(current_rect())
                        ov.flash(t("Sign in with Discord ({key}) to add price alerts", key=settings.options_key),
                                 "info", 2500)
                    else:
                        threading.Thread(target=find_alert_item, args=(current_rect(),), daemon=True,
                                         name="alert-item").start()
                elif kind == "alert_found":
                    ask_alert(*payload)
                elif kind == "alert_none":
                    ov.place(current_rect())
                    ov.flash(payload, "info", 2500)
                elif kind == "alert_result":
                    dialog, ok, text = payload
                    dialog.done(ok, text)
                elif kind == "quit":
                    log.info("Exit MaplePrice requested")
                    root.destroy()
                    return
                elif kind == "hover_toggle":
                    settings.hover_prices = not settings.hover_prices
                    on_settings_change()
                    options.show_settings()
                    if settings.hover_prices:
                        ov.flash(t("Hover price check on: rest the cursor on an item  \u00b7  {key} off",
                                   key=settings.hover_key), "info", 2500)
                    else:
                        ov.flash(t("Hover price check off"), "info", 1200)
                elif kind == "hover":
                    if payload and hovering.is_set():
                        card, dot, rect, scale = payload
                        if rect != ov.rect:  # the game window moved
                            ov.place(rect)
                        ov.hover_card(card, dot, scale)
                    else:
                        ov.hover_card()
                elif kind == "result":
                    if settings.shop_prices:  # not switched off while it was being read
                        shown.update(zip(("labels", "panel", "where"), payload))
                        show_result()
                elif kind == "blocked":
                    shown.clear()
                    ov.clear()
                    ov.flash(payload, "warn")
                elif kind == "error":
                    ov.status(payload, "error")
                elif kind == "update_status":
                    if payload:
                        ov.status(payload)
                    else:
                        ov.clear_status()
                elif kind == "restart":
                    log.info("restarting into %s", payload)
                    release_instance()
                    restart_updated_app(payload)
                    root.destroy()
                    return
        except queue.Empty:
            pass
        except Exception:  # e.g. a panel that failed to draw: log it, but keep scanning (and seeing the stop file)
            log.exception("overlay event failed")
        root.after(50, poll)

    startup.phase("Starting hotkeys, OCR warm-up, and price sync...")
    bindings = lambda: [(settings.scan_key, "scan"), (settings.clear_key, "clear"), (settings.options_key, "options"),
                        (settings.hover_key, "hover_toggle"), (settings.alert_key, "alert_item")]
    paused = lambda key: options.capturing
    threading.Thread(target=listen_hotkeys, args=(bindings, events, paused), daemon=True).start()
    threading.Thread(target=watch_hover, args=(current_rect, db, events, hovering), daemon=True, name="hover").start()
    shops_on = lambda: settings.auto_scan and settings.shop_prices
    threading.Thread(target=watch_shop, args=(current_rect, events, shops_on, busy), daemon=True,
                     name="shop").start()
    threading.Thread(target=offer_update, args=(events,), daemon=True, name="update").start()
    threading.Thread(target=ocr.warm_up, daemon=True, name="ocr-warm-up").start()  # first scan as quick as the rest
    threading.Thread(target=sync.run, daemon=True, name="sync").start()
    threading.Thread(target=Feed(db, settings, reader=sync).run, daemon=True, name="feed").start()
    if signin:
        signin.check()
        threading.Thread(target=alerts.run, daemon=True, name="alerts").start()
        refresh_watches()
    STOP_FILE.unlink(missing_ok=True)
    ov.place(current_rect())
    apply_switches()  # hover prices on from the last session
    if args.after_update:
        ov.flash(t("Updated to version {version}", version=VERSION), "info", 4000)
    log.info("overlay ready; options key=%s", settings.options_key)
    console_notice(settings)
    if settings.show_options:
        startup.phase("Opening the options window...")
        options.toggle()
    poll()
    root.after_idle(lambda: startup.finish(f"Ready. Options: {settings.options_key}."))
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        ui.close()


def run_image(args, db):
    from PIL import Image

    img = Image.open(args.image).convert("RGB")
    labels, panel, place, blocked = scan(lambda: img, db)
    print("location:", place)
    painter = PilPainter(img)  # draws exactly what the live overlay would
    if blocked:
        print("not scanned:", blocked)
        draw_badge(painter, img.width // 2, img.height // 2, blocked, "warn", font="big", center=True)
    else:
        draw_badge(painter, img.width // 2, 6, f"{place}  ·  {len(labels)} items")
    for lb in labels:
        print(f"{lb.text}  @ ({lb.right}, {lb.center_y})")
    draw_labels(painter, labels)
    if panel:
        draw_panel(painter, panel)
        for card in panel.cards:
            pills = "  ".join(f"[{p.text}]" for p in card.pills)
            print(f"  card: {card.title} | {card.subtitle} | {''.join(t for t, _ in card.price)} {pills}")
    if args.out:
        img.save(args.out)
        print(f"Saved {args.out}")


def arguments():
    """The command line: every hotkey in settings.HOTKEYS can be overridden (run_overlay reads them all)."""
    p = argparse.ArgumentParser(prog=APP_ID, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--title", default="MapleStory", help="game window title (exact match)")
    p.add_argument("--scan-key", help="override the saved hotkey (default F8; rebind it in the options window)")
    p.add_argument("--clear-key", help="override the saved hotkey (default F9)")
    p.add_argument("--options-key", help="override the saved hotkey (default F10)")
    p.add_argument("--hover-key", help="override the saved hotkey (default F7)")
    p.add_argument("--alert-key", help="override the saved hotkey (default F6)")
    p.add_argument("--image", help="annotate a saved screenshot instead of running the overlay")
    p.add_argument("--out", help="with --image: where to save the annotated copy")
    p.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)  # started by the old version
    return p


def main():
    startup.begin()
    startup.phase("Application libraries loaded. Configuring logging...")
    set_console_title()
    args = arguments().parse_args()

    log_path = DEFAULT_DB.parent / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        # log.txt, then log.txt.1 to .3 as it fills: never more than ~20 MB in all
        handlers=log_handlers(log_path),
    )
    startup.logging_ready()
    startup.phase(f"Session logging ready: {log_path}")
    if not args.image:
        disable_quick_edit()
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    log.info("start pid=%s admin=%s version=%s", os.getpid(), is_admin, VERSION)
    if args.after_update:  # the old version is still closing: give it a moment
        deadline = time.monotonic() + 15
        while already_running() and time.monotonic() < deadline:
            release_instance()
            time.sleep(0.5)
        release_instance()
    startup.phase("Checking for an existing instance...")
    if not args.image and already_running():
        startup.finish("An instance is already running. Press F10 for its options.")
        log.info("already running, exiting")
        ctypes.windll.user32.MessageBoxW(None, "The app is already running.\n\n"
                                         "Press F10 in game for options and the dashboard.", DISPLAY_NAME, 0x40)
        return
    if getattr(sys, "frozen", False) and not args.image:
        update.rename_to_version()  # mprice_decoder-2.3.exe after an update to 2.5 becomes mprice_decoder-2.5.exe

    from .capture import make_dpi_aware
    startup.phase("Configuring display scaling...")
    make_dpi_aware()
    startup.phase("Opening the local item database...")
    db = ItemDB(mapper=FETCH.map)
    startup.phase("Local item database ready.")
    if args.image:
        run_image(args, db)
        startup.finish("Image processing complete.")
    else:
        run_overlay(args, db)


if __name__ == "__main__":
    try:
        main()
    finally:
        startup.finish()
