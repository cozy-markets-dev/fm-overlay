"""mprice_decoder's look, after the Cozy Markets website's (cozy-markets/frontend/src/styles.css, whose names are
in the comments): white panels with a lilac rim, cream cards, deep purple words, the FM price on a gold pill and
verdicts as solid chips. Fredoka for titles, Nunito for everything else (both SIL Open Font License, bundled in
assets/fonts with their licences). Buttons take the site's call-to-action colour, pumpkin in October like the site
(palette.ts season), else pink.

Colours are picked to read on the panels, not on the game behind it."""
import datetime as dt
import sys
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

# ---- palette -----------------------------------------------------------------
PANEL = "#ffffff"      # windows and panels (--surface)
FIELD = "#f1eaff"      # inputs, quiet buttons, location chips (--lilac)
CARD = "#fff8ec"       # cards inside a panel (--cream)
CARD_LINE = "#f0d6b8"  # card borders (--line-strong)
BORDER = "#c9b3ff"     # a panel's rim: the site's lilac, a shade stronger to stand off the game
SHADOW_INK = "#b9a5ec" # its drop shadow (--shadow, made solid)
INK = "#40295a"        # main text (--ink)
INK_STRONG = "#4e2a9e" # headings, numbers (--ink-strong / --purple-deep)
INK_DEEP = "#3a1868"   # item names (--ink-deep)
MUTED = "#6c5a80"      # captions ("Asking", "under your") (--ink-soft)
FAINT = "#a597b5"      # greyed out (--faint)
LABEL_INK = "#7c4ddb"  # small labels and links (--ink-link)
GOLD_INK = "#9c6500"   # FM prices as words (--ink-fm)
GOLD_1, GOLD, GOLD_3, GOLD_DEEP = "#fff3b8", "#ffcc3d", "#f0a812", "#6b4200"  # the FM price's pill (.mesos.m-gold)
UP = "#2c7d18"         # good for you: profit, cheapest (--good)
DOWN = "#d63a63"       # bad for you: overpriced (--bad)
BLUE = "#2a64b0"       # sold (--sold)
PLUM = "#7c4ddb"       # deals: well under the FM price (--deal)
WARN = "#6b4200"       # notes and warnings, on a gold tint (--ink-gold)
PURPLE = "#7c4ddb"     # Cozy Markets' own finds (--purple)
NEUTRAL = "#cbc3d6"    # no opinion (--neutral)
GOOD_1 = "#5cc93a"     # the "live" light (--green)


def _halloween(today=None):
    """The site's October season (palette.ts): October 1st to November 1st."""
    today = today or dt.date.today()
    return today.month == 10 or (today.month == 11 and today.day == 1)


# buttons (--cta, --cta-2, --cta-deep): pumpkin through October, else pink
CTA, CTA_2, CTA_DEEP = ("#ef7214", "#ffa04a", "#a8460a") if _halloween() else ("#ff5fa6", "#ff9ccb", "#c42c74")

FM_PILL = "fm-pill"    # not a colour: render.py draws a segment "coloured" this on the FM price's gold pill

SHADOW = 3             # px a panel's shadow drops
RADIUS = 14            # panel corners
CARD_RADIUS = 10


def tint(color, amount=0.86, base=CARD):
    """`color` washed out towards `base`: the fill behind a chip's coloured text."""
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(c, b))


# ---- fonts -------------------------------------------------------------------
# name -> (Tk font, PIL file, PIL pixel size). Tk sizes are points; PIL's are the
# same size in pixels at 96 DPI (pt * 4/3), so --image mode matches the overlay.
FONTS = {
    "title": (("Fredoka SemiBold", 10), "Fredoka-SemiBold.ttf", 13),
    "body": (("Nunito ExtraBold", 9), "Nunito-ExtraBold.ttf", 12),
    "small": (("Nunito", 9, "bold"), "Nunito-Bold.ttf", 12),
    "pill": (("Nunito ExtraBold", 7), "Nunito-ExtraBold.ttf", 10),
    "label": (("Nunito ExtraBold", 11), "Nunito-ExtraBold.ttf", 15),
    "label_small": (("Nunito ExtraBold", 9), "Nunito-ExtraBold.ttf", 12),
    "big": (("Fredoka SemiBold", 18), "Fredoka-SemiBold.ttf", 24),
}

_loaded = False


def load_fonts():
    """Make the bundled fonts usable by Tk, for this process only (nothing is
    installed). Windows only; elsewhere Tk falls back to a default font.
    They're loaded from where they're bundled: in the exe, the folder it unpacks
    itself to, as trusted as the code it unpacks there. Windows won't load a font
    from a network path (e.g. running from \\\\wsl.localhost), so then they're
    copied to a local temp folder first, and copied again unless the copy is
    exactly the bundled file: any program can write there, and this one (admin)
    parses whatever font it loads."""
    global _loaded
    if _loaded or sys.platform != "win32":
        return
    import ctypes
    import logging
    import tempfile
    FR_PRIVATE = 0x10
    local = Path(tempfile.gettempdir()) / "mprice_decoder-fonts"
    for f in sorted(FONT_DIR.glob("*.ttf")):
        try:
            font = f
            if str(f).startswith("\\\\"):  # a network path
                local.mkdir(exist_ok=True)
                font, data = local / f.name, f.read_bytes()
                if not font.is_file() or font.read_bytes() != data:
                    font.write_bytes(data)
            if not ctypes.windll.gdi32.AddFontResourceExW(str(font), FR_PRIVATE, 0):
                logging.getLogger("mprice_decoder").warning("couldn't load font %s", font)
        except OSError:
            logging.getLogger("mprice_decoder").exception("couldn't load font %s", f)
    _loaded = True


def pil_font(name, scale=1):
    from PIL import ImageFont
    _, file, size = FONTS[name]
    return ImageFont.truetype(str(FONT_DIR / file), max(1, round(size * scale)))


def tk_font(name, scale=1):
    """FONTS' Tk font `scale` times its size (settings.overlay_scale)."""
    family, size, *rest = FONTS[name][0]
    return (family, max(1, round(size * scale)), *rest)
