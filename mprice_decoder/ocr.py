"""Local OCR with RapidOCR (PaddleOCR models on onnxruntime, CPU only).

Benchmarked on saved shop screenshots against Windows' built-in OCR: it read
30/32 prices correctly vs 24/32, and in ~0.2s per scan vs ~1.5s, because we
only OCR the shop's item list: the shop window is found by template-matching
its BUY ITEM / LEAVE STORE buttons, and the list sits at a fixed offset.
"""
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
import cv2

from .data import strip_upgrades
from .shop import Line

log = logging.getLogger("mprice_decoder")

ASSETS = Path(__file__).resolve().parent / "assets"
# (image, its offset from the BUY ITEM button's top-left, match threshold), tried in order.
# Your own shop shows OPEN STORE / CLOSE STORE instead; only CLOSE STORE is used,
# since OPEN STORE greys out once the store is open. It half-matches a visitor's
# BUY ITEM (~0.85), so a match with another orange button right under it isn't
# taken (_button_under). A 1080p game brought back to its own size blurs it to ~0.90.
TEMPLATES = [
    (ASSETS / "shop_buttons.png", (0, 0), 0.8),        # someone else's shop: BUY ITEM / LEAVE STORE
    (ASSETS / "shop_owner_buttons.png", (0, 18), 0.85),  # your own: CLOSE STORE
]
# Regions relative to the top-left of the buttons template.
LIST_BOX = (-140, 56, 70, 304)     # item list: icons through scrollbar, 6 rows
TITLE_BOX = (-140, -96, 360, -64)  # the shop's name, in the box at the top
SELLER_BOX = (-140, 32, -36, 60)   # seller's name, under their character

_engine = None
_engine_lock = threading.Lock()  # the hover watcher and a shop scan can OCR at the same time
_templates = None
_half_templates = None
_templates_lock = threading.Lock()
last_list_box = None  # where read_lines last found the shop's item list
_last_anchor = None   # where locate_list last found the shop's buttons: looked at first next time


def _ocr():
    global _engine
    if _engine is None:
        # 4 threads is the sweet spot (all 20 cores was 2.5x slower); the
        # rotation classifier is pointless for horizontal UI text.
        _engine = RapidOCR(det_limit_side_len=960, det_limit_type="max",
                           intra_op_num_threads=4, inter_op_num_threads=1)
    return _engine


def warm_up():
    """Load the OCR models and run them once, on a made-up line of text. The first run takes
    ~1s longer than the rest; called in the background at startup, so the first shop scan
    doesn't pay for it (one that starts meanwhile waits on the lock, as it would anyway)."""
    strip = np.full((40, 260, 3), 255, np.uint8)
    cv2.putText(strip, "Warm up 1,234", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    t = time.perf_counter()
    try:
        _read(strip)
    except Exception:
        log.exception("OCR warm-up failed (the first scan just loads it instead)")
        return
    log.info("OCR ready in %.2fs", time.perf_counter() - t)


def _read(rgb):
    """OCR results [(quad, text, score), ...] for an RGB array, or None."""
    with _engine_lock:
        result, _ = _ocr()(rgb, use_cls=False)
    return result


def _half(rgb):
    return cv2.resize(rgb, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)


def _load_templates():
    global _templates, _half_templates
    if _templates is None:
        _templates = [(np.array(Image.open(path).convert("RGB")), offset, threshold)
                      for path, offset, threshold in TEMPLATES]
        _half_templates = [_half(t) for t, _, _ in _templates]


def locate_buttons(rgb):
    """Top-left of the shop's BUY ITEM / LEAVE STORE buttons (or where they'd
    be, in your own shop), or None if no shop is open."""
    _load_templates()
    with _templates_lock:  # the shop watcher and a scan both reorder the list
        for i, (template, (dx, dy), threshold) in enumerate(_templates):
            if rgb.shape[0] < template.shape[0] or rgb.shape[1] < template.shape[1]:
                continue
            _, score, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(rgb, template, cv2.TM_CCOEFF_NORMED))
            if score >= threshold and not (dy and _button_under(rgb, bx, by, template)):
                if i:  # e.g. your own shop: check its buttons first next time
                    _templates.insert(0, _templates.pop(i))
                return bx - dx, by - dy
    return None


def _button_under(rgb, x, y, template):
    """Is there an orange button right under this one? A visitor's BUY ITEM has LEAVE STORE there (~0.8 of the
    pixels), your CLOSE STORE the white mesos box (none)."""
    h, w = template.shape[:2]
    strip = rgb[y + h + 5:y + h + 15, x + 8:x + w - 8].reshape(-1, 3).astype(int)
    return len(strip) > 0 and ((strip[:, 0] - strip[:, 2] > 100) & (strip[:, 0] > strip[:, 1])).mean() > 0.3


SHOP_HINT = 0.62  # a half-size match this good is worth a full-size look (shops: >= 0.68, else ~0.53)
NEAR = 24         # px the shop may have moved since the last look


def find_shop(rgb, near=None):
    """locate_buttons, cheap enough to poll a few times a second: first right
    where the shop was (`near`, ~1ms), then a half-size pass over the screen
    (~20ms, vs 40-80ms full size) that only pays for a full search when
    something there looks like the buttons."""
    _load_templates()
    if near:
        x, y = near
        x0, y0 = max(0, x - NEAR), max(0, y - NEAR)
        w = max(dx + t.shape[1] for t, (dx, _), _ in _templates) + NEAR
        h = max(dy + t.shape[0] for t, (_, dy), _ in _templates) + NEAR
        found = locate_buttons(rgb[y0:y + h, x0:x + w])
        if found:
            return found[0] + x0, found[1] + y0
    small = _half(rgb)
    for template in _half_templates:
        if small.shape[0] >= template.shape[0] and small.shape[1] >= template.shape[1]:
            if cv2.minMaxLoc(cv2.matchTemplate(small, template, cv2.TM_CCOEFF_NORMED))[1] >= SHOP_HINT:
                return locate_buttons(rgb)
    return None


def _region(rgb, anchor, box):
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    return max(0, anchor[0] + x0), max(0, anchor[1] + y0), min(w, anchor[0] + x1), min(h, anchor[1] + y1)


LIST_ICONS = 45      # px of icons at the list's left: our labels can overlap them
CURSOR_BOX = (-12, -12, 36, 40)  # the game's cursor sprite around its tip
CHANGED_PX = 40      # grey levels: a pixel that moved more than this changed
CHANGED_SHARE = 0.005  # share of changed pixels that makes it a different list


def list_fingerprint(rgb, anchor, cursor=None):
    """A greyscale copy of the shop's item names and prices, to tell when the
    list changes (opened, scrolled, something sold). The icons are left out, and
    so is the game's cursor (NaN): neither changes what's for sale."""
    x0, y0, x1, y1 = _region(rgb, anchor, LIST_BOX)
    x0 += LIST_ICONS
    grey = cv2.cvtColor(np.ascontiguousarray(rgb[y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY).astype(np.float32)
    if cursor:
        cx, cy = cursor[0] - x0, cursor[1] - y0
        bx0, by0, bx1, by1 = CURSOR_BOX
        grey[max(0, cy + by0):max(0, cy + by1), max(0, cx + bx0):max(0, cx + bx1)] = np.nan
    return grey


def list_changed(a, b):
    if a.shape != b.shape:  # the shop window moved off the screen's edge
        return True
    diff = np.abs(a - b)
    seen = ~np.isnan(diff)
    return not seen.any() or (diff[seen] > CHANGED_PX).mean() > CHANGED_SHARE


def locate_list(rgb):
    """(x0, y0, x1, y1) of the shop's item list in `rgb`, or None if no shop is open. Where the shop was last
    is looked at first (~1 ms, vs ~45 ms for the whole screen)."""
    global _last_anchor
    anchor = (find_shop(rgb, _last_anchor) if _last_anchor else None) or locate_buttons(rgb)
    if anchor:
        _last_anchor = anchor
    return _region(rgb, anchor, LIST_BOX) if anchor else None


MINIMAP_BOX = (0, 0, 300, 120)  # the minimap's default spot, top-left of the game
MINIMAP_PANEL = (153, 187, 204)  # the light blue behind the minimap's street and map name
ROOM_RE = re.compile(r"Free\s*Market\s*<?\s*(\d{1,2})", re.IGNORECASE)


def _on_panel(rgb, box):
    """Is this OCR'd text sitting on the minimap's title panel? Shop signs,
    the window title and our own overlay are all on other colors."""
    xs, ys = [int(p[0]) for p in box], [int(p[1]) for p in box]
    px = rgb[max(0, min(ys)):max(ys), max(0, min(xs)):max(xs)].reshape(-1, 3).astype(int)
    return len(px) > 0 and (np.abs(px - MINIMAP_PANEL).max(axis=1) <= 6).mean() >= 0.35


def clean_map_name(name):
    """OCR spacing varies ("Free Market<8>", "FreeMarket <2>"); one spelling per
    map keeps them comparable."""
    room = ROOM_RE.search(name)
    if room:
        return f"Free Market <{int(room.group(1))}>"
    return re.sub(r"\s*<\s*", " <", " ".join(name.split()))


def read_map(img):
    """Map name from the minimap title, e.g. "Free Market <2>" or "Henesys Market"
    (the line under the street name). None when the minimap is collapsed or hidden."""
    rgb = np.array(img.convert("RGB"))
    x0, y0, x1, y1 = MINIMAP_BOX
    crop = rgb[y0:min(y1, rgb.shape[0]), x0:min(x1, rgb.shape[1])]
    # Only OCR the title panel's rows: the map picture below is busy (Kerning
    # City's took 0.8s to "read"), and no panel at all means it's collapsed.
    panel_rows = np.flatnonzero((np.abs(crop.astype(int) - MINIMAP_PANEL).max(axis=2) <= 6).mean(axis=1) >= 0.15)
    if not len(panel_rows):
        return None
    crop = crop[max(0, panel_rows[0] - 8):panel_rows[-1] + 9]  # a margin: tight crops drop spaces
    result = _read(crop)
    lines = sorted((min(p[1] for p in box), text) for box, text, _ in (result or []) if _on_panel(crop, box))
    return clean_map_name(lines[1][1]) if len(lines) >= 2 else None  # [0] is the street


def fm_room_of(map_name):
    """Free Market room number of a map name ("Free Market <2>" -> 2), else None."""
    m = ROOM_RE.search(map_name or "")
    return int(m.group(1)) if m else None


FM_ROOMS = range(1, 21)  # Free Market <1> to <20> (map_names.FM_ROOMS)
_TAIL_ROOM = re.compile(r"^(.*?)\s*<?\s*(\d{1,2})\s*>?$")
_NUMBERED = re.compile(r"^(.*?)\s+(?:I{1,3}|IV|V|\d+)$")  # "Ant Tunnel I", "Ant Tunnel II": one of a series
_maps = None


def _key(name):
    return re.sub(r"[^a-z0-9]", "", name.replace("!", "I").replace("|", "I").lower())


def _map_names():
    """{map id: name} from the game's map list (assets/map-names.json)."""
    import json
    import sys
    from pathlib import Path
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    path = base / "mprice_decoder" / "assets" / "map-names.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    return {int(key): row["mapName"].strip() for key, row in catalog["maps"].items()}


def _catalog():
    """{key: name} of every map but the FM rooms, and the keys of those in a numbered series."""
    global _maps
    if _maps is None:
        MAP_NAMES = _map_names()
        names = {n for n in MAP_NAMES.values() if not ROOM_RE.search(n)}
        bases = [m.group(1) for n in names if (m := _NUMBERED.match(n))]
        _maps = ({_key(n): n for n in names},
                 {_key(n) for n in names if (m := _NUMBERED.match(n)) and bases.count(m.group(1)) > 1})
    return _maps


def known_map(name):
    """The map a minimap read is, by the game's own names (assets/map-names.json), or None when it can't
    be told: a Free Market room by the number read ("FeeMarket <3>" is FM 3; no number, no room), a shop sign read
    on the end cut off, a name cut short or a letter or two off taken when only one map fits. Within a numbered
    series ("Henesys Hunting Ground I", "... II") only a letter off, so the number isn't guessed."""
    if not name:
        return None
    maps, numbered = _catalog()
    key = _key(name)
    if key in maps:
        return maps[key]
    room = _TAIL_ROOM.match(name)
    if room:
        fm = _key(room.group(1))  # "FeeMarket", or its start cut off: "larket"
        if _edits(fm, "freemarket", 3) <= 3 or (len(fm) >= 5 and _edits(fm, "freemarket"[-len(fm):], 1) <= 1):
            return f"Free Market <{int(room.group(2))}>" if int(room.group(2)) in FM_ROOMS else None
    if _edits(key[:10], "freemarket", 3) <= 3:
        return None  # the Free Market, but which room?
    cut = [k for k in maps if len(key) >= 10 and k.startswith(key)]  # "Kerning City Const"
    if cut:
        return maps[cut[0]] if len(cut) == 1 else None
    most = max(2, len(key) // 6)
    near = sorted((_edits(key, k, most), k) for k in maps) if len(key) >= 6 else []
    if near:
        (d, best), second = near[0], near[1][0] if len(near) > 1 else most + 1
        if d < second and d <= (1 if best in numbered else most):
            return None if best in numbered and best.startswith(key) else maps[best]  # "Ant Tunnel": which one?
    # a shop sign read on the end ("Kerning City Construction Site BlackKitty"): long names only, as a short one
    # starts longer ones ("Henesys" a misread "HenesysHumting Ground")
    starts = [k for k in maps if len(k) >= 15 and key.startswith(k)]
    return maps[max(starts, key=len)] if starts else None


def _edits(a, b, most):
    """Edit distance between a and b, or most + 1 once it's sure to be over `most`."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > most:
            return most + 1
        prev = cur
    return prev[-1]


def read_header(img):
    """(shop title, seller name) of the open shop; either may be None."""
    rgb = np.array(img.convert("RGB"))
    anchor = locate_buttons(rgb)
    if not anchor:
        return None, None
    out = []
    for box in (TITLE_BOX, SELLER_BOX):
        x0, y0, x1, y1 = _region(rgb, anchor, box)
        if x1 - x0 < 8 or y1 - y0 < 8:
            out.append(None)
            continue
        result = _read(rgb[y0:y1, x0:x1])
        text = " ".join(t for _, t, score in (result or []) if score >= 0.6).strip()
        out.append(text or None)
    return tuple(out)


# Your character's name is on the status bar at the bottom, under your job, right of the
# "LV." label. The bar moves with the window size, so the label is found by template.
LEVEL_LABEL = ASSETS / "level_label.png"
PLAYER_BOX = (69, -4, 205, 28)  # job and name, from the label's top-left
STATUS_BAR = 120                # px at the bottom of the screen to look for the label in
NAME_RE = re.compile(r"[A-Za-z0-9]{2,13}")
_level_label = None


def _label():
    global _level_label
    if _level_label is None:
        _level_label = np.array(Image.open(LEVEL_LABEL).convert("RGB"))
    return _level_label


# The game draws itself at its own size (1366x768, also 1024x768 or 1280x720) and, in a bigger window or full screen,
# everything that much bigger: 1920x1080 is its 1366x768 at 1080 / 768 = 1.40625x, full screen at 1280x720 on a
# 2560x1440 screen is 2x. Everything here reads it at its own size, so a bigger picture is brought back to that first
# (game_view), as the website's scanner does (cozy-markets frontend scanner/worker.ts).
WINDOW_SCALES = (1080 / 768,)  # 1920x1080's, whatever the screen
SCALE_SCORE = 0.8   # the LV. label matches this well at the right scale (0.86-1.0 on every resolution; wrong ones <= 0.5)
SCALE_EVERY = 2.0   # s one size's scale is trusted before it's checked again (full screen can change resolution)
SCALE_SURE = 0.95   # a match this good is trusted that long; a worse one (0.91: a frame half-redrawn while the game
SCALE_UNSURE = 0.25  # changed resolution, 2026-10-21) only this long
_scales = {}        # (width, height) -> (scale, checked at, trusted for)


def _label_score(rgb, s):
    """How well the status bar's LV. label matches if `rgb` shows the game `s` times bigger."""
    h, w = rgb.shape[:2]
    rows = int(round(STATUS_BAR * s))
    label = _label()
    if h < rows or w / s < label.shape[1]:
        return 0.0
    strip = rgb[h - rows:]
    if s != 1:
        strip = cv2.resize(strip, (int(round(w / s)), STATUS_BAR), interpolation=cv2.INTER_AREA)
    return cv2.minMaxLoc(cv2.matchTemplate(strip, label, cv2.TM_CCOEFF_NORMED))[1]


def game_scale(rgb, clock=time.monotonic):
    """How much bigger than its own size the game is drawn in `rgb` (its window's client area): the scale the status
    bar's LV. label matches at. With no status bar to tell by (hidden, the Cash Shop), the last scale seen at this size,
    else a guess from the height."""
    h, w = rgb.shape[:2]
    known, at, trusted = _scales.get((w, h), (None, None, None))
    if known is not None and clock() - at < trusted:
        return known
    scores = {s: _label_score(rgb, s) for s in {1.0, h / 768, h / 720, *WINDOW_SCALES} if s >= 1}
    best = max(scores, key=scores.get)
    if scores[best] >= SCALE_SCORE:
        scale = best
    elif known is not None:
        scale = known
    else:
        scale = 1.0 if h < 768 * 1.1 else h / 768
    if scale != known:
        log.info("game drawn %.3fx at %dx%d (LV. label %.2f)", scale, w, h, scores[best])
    _scales[(w, h)] = scale, clock(), SCALE_EVERY if scores[best] >= SCALE_SURE else SCALE_UNSURE
    return scale


def game_view(img):
    """`img` (a screenshot of the game's client area) as the game draws itself at its own size, and how much bigger
    it is on screen: (image, scale). Positions read from the image are `scale` times as far on screen."""
    rgb = np.array(img.convert("RGB"))
    scale = game_scale(rgb)
    if scale == 1:
        return img, 1.0
    h, w = rgb.shape[:2]
    small = cv2.resize(rgb, (int(round(w / scale)), int(round(h / scale))), interpolation=cv2.INTER_AREA)
    return Image.fromarray(small), scale


def read_player(img):
    """Your character's name off the status bar, or None if it isn't on screen or reads oddly."""
    _label()
    rgb = np.array(img.convert("RGB"))
    top = max(0, rgb.shape[0] - STATUS_BAR)
    bar = rgb[top:]
    if bar.shape[0] < _level_label.shape[0] or bar.shape[1] < _level_label.shape[1]:
        return None
    _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(bar, _level_label, cv2.TM_CCOEFF_NORMED))
    if score < 0.8:
        return None
    x0, y0, x1, y1 = _region(rgb, (x, y + top), PLAYER_BOX)
    lines = sorted((min(p[1] for p in box), text.strip()) for box, text, score in (_read(rgb[y0:y1, x0:x1]) or [])
                   if score >= 0.8)
    if len(lines) < 2:  # job, then name: with only one line we can't tell which it is
        return None
    name = lines[-1][1]
    return name if NAME_RE.fullmatch(name) else None


def read_lines(img, attempt=0):
    """OCR text lines (image coordinates) from the shop list, or None when no
    shop window is found. (Never the whole screen: that took 5-15s and, upscaled
    on a retry, left the OCR engine holding ~800 MB.) Retries (attempt >= 1)
    upscale 2x to get a second, independent reading."""
    global last_list_box
    rgb = np.array(img.convert("RGB"))
    box = last_list_box = locate_list(rgb)
    if not box:
        return None
    ox, oy = box[0], box[1]
    region = rgb[box[1]:box[3], box[0]:box[2]]
    scale = 2 if attempt else 1
    if scale != 1:
        region = cv2.resize(region, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    result = _read(region)
    lines = []
    for quad, text, score in result or []:
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        lines.append(Line(
            text, round(min(xs) / scale) + ox, round(min(ys) / scale) + oy,
            round((max(xs) - min(xs)) / scale), round((max(ys) - min(ys)) / scale),
            score=float(score),
        ))
    return lines


# Item tooltips (inventory, NPC shops) start with a small cyan dot left of the
# item's name. It's opaque, unlike the see-through box around it, so it's the
# same colour on every map: an exact anchor for the name line.
TOOLTIP_DOT = (85, 204, 255)
TOOLTIP_DOT_DY = 35            # the dot sits this far below the cursor's tip
WORN_GAP = (220, 345)          # px the worn item's tooltip sits right of the hovered one's: 238 at 1x, 335 at 1080p
NAME_STRIP = (6, -9, 400, 15)  # the name line, relative to the dot's top-left
NAME_WHITE = 200               # the name is white; what shows through the box isn't
NAME_GAP = 16                  # empty columns that end the name


def find_tooltip_dot(rgb, cursor=None, anywhere=True):
    """Top-left (x, y) of an item tooltip's dot, or None. With several on
    screen, the one where the cursor's tooltip would be. Looks in the band
    where it normally is first: the whole screen takes ~20x longer (skipped
    unless `anywhere`)."""
    if cursor is not None:
        top = max(0, cursor[1] + TOOLTIP_DOT_DY - 12)
        dot = _find_dot(rgb[top:cursor[1] + TOOLTIP_DOT_DY + 20], (cursor[0], cursor[1] - top))
        if dot:
            return dot[0], dot[1] + top
    return _find_dot(rgb, cursor) if anywhere else None


def _find_dot(rgb, cursor):
    if not rgb.size:
        return None
    near = (np.abs(rgb.astype(np.int16) - TOOLTIP_DOT).max(axis=2) <= 10).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(near)
    dots = [(int(x), int(y)) for x, y, w, h, _ in stats[1:] if 3 <= w <= 8 and 3 <= h <= 8]
    if not dots:
        return None
    if cursor is None:
        return dots[0]
    cx, cy = cursor
    best = min(dots, key=lambda d: abs(d[1] - cy - TOOLTIP_DOT_DY) * 4 + abs(d[0] - cx))
    # An equip hovered in the inventory also shows the one you wear, level with it and a tooltip's width to its
    # right. Near the screen's right edge the game slides both left, which can put the worn one under the cursor:
    # the hovered one is the left of the pair.
    pair = [d for d in dots if abs(d[1] - best[1]) <= 3 and WORN_GAP[0] <= best[0] - d[0] <= WORN_GAP[1]]
    return pair[0] if pair else best


def _name_only(strip):
    """The name line as white text on black, cut at the first wide gap so
    nothing right of the tooltip comes along."""
    white = strip.min(axis=2) >= NAME_WHITE
    cols = np.flatnonzero(white.any(axis=0))
    if not len(cols):
        return None
    gaps = np.flatnonzero(np.diff(cols) > NAME_GAP)
    end = cols[gaps[0]] + 1 if len(gaps) else cols[-1] + 1
    out = np.where(white[:, :end, None], 255, 0).astype(np.uint8).repeat(3, axis=2)
    return cv2.copyMakeBorder(out, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=0)


def read_tooltip(img, cursor=None):
    """(readings of the item's name, best first; the dot's (x, y)) for the item
    tooltip on screen, or None when there isn't one. Two readings: the name
    cleaned up to white-on-black, then as is (OCR slips differ between them)."""
    rgb = np.array(img.convert("RGB"))
    dot = find_tooltip_dot(rgb, cursor)
    if not dot:
        return None
    x0, y0, x1, y1 = NAME_STRIP
    strip = rgb[max(0, dot[1] + y0):dot[1] + y1, dot[0] + x0:dot[0] + x1]
    names = []
    clean = _name_only(strip)
    if clean is not None:
        names.append(" ".join(t for _, t, _ in sorted(_read(clean) or [], key=lambda r: r[0][0][0])))
    raw = sorted(_read(np.ascontiguousarray(strip)) or [], key=lambda r: r[0][0][0])
    if raw:
        names.append(raw[0][1])  # the leftmost: anything further right shows through the box
    names = [strip_upgrades(n).strip() for n in names]  # an enhanced equip's "(+7)" isn't part of its name
    names = list(dict.fromkeys(n for n in names if n))
    return (names, dot) if names else None
