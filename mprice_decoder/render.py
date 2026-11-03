"""Draws the detail panel, the labels beside each shop row and the status badges.
Layout lives here once; a "painter" does the actual drawing, so the live overlay
(Tk canvas) and --image mode (PIL) look the same. The look itself (colours,
fonts, the website's white-and-lilac panels) is in theme.py.

The panel must fit the space right of the shop window (Panel.left/right,
Panel.min_top/bottom). Text that's too long is cut with "…", tags that don't
fit are dropped, and if full cards are too tall they switch to compact ones.

A painter provides (in its own units: a painter at overlay size `scale` draws everything that much bigger,
from the same spots, so the layout here never changes; size() is the area in those units):
    size() -> (width, height)                      drawable area
    text_width(text, font) / line_height(font)     font is a theme.FONTS name
    text(x, y, text, color, font)                  top-left anchored
    round_rect(x0, y0, x1, y1, r, fill, outline=None, width=1)
    image(x, y, png_bytes)                         top-left anchored, native size
"""
import functools
import io

from PIL import Image, ImageDraw

from . import theme

TITLE = theme.INK_DEEP
SUBTITLE = theme.MUTED

PAD = 8           # inside the panel (its border is 2px)
CARD_PAD = 7      # inside each card
CARD_GAP = 5
ACCENT_W = 4      # coloured bar on the left of each card: the verdict at a glance
ICON_BOX = 40     # shop icons are ~36px
TEXT_GAP = 8
PILL_PAD_X, PILL_PAD_Y, PILL_GAP = 7, 2, 4
SEG_PAD = 6       # either side of a price on the gold FM pill
MAX_WIDTH = 460
MIN_WIDTH = 240
ELLIPSIS = "…"

# status badge tones: (fill, text)
TONES = {
    "info": (theme.PANEL, theme.INK_STRONG),
    "warn": ("#fff7d1", theme.WARN),     # --gold-tint
    "error": ("#ffe0ef", theme.DOWN),    # --pink-tint
}


def window(p, x0, y0, x1, y1, fill=theme.PANEL, border=theme.BORDER, width=2, r=theme.RADIUS, shadow=theme.SHADOW):
    """A panel like the website's: white, a lilac rim, a soft purple shadow straight down."""
    if shadow:
        p.round_rect(x0, y0 + shadow, x1, y1 + shadow, r, theme.SHADOW_INK)
    p.round_rect(x0, y0, x1, y1, r, fill, border, width)


def _seg_w(p, text, color, font):
    return p.text_width(text, font) + (2 * SEG_PAD if color == theme.FM_PILL else 0)


def _fit(p, segs, font, width):
    """Cut a line of (text, color) segments to `width`, ending with "…"."""
    out, used = [], 0
    for text, color in segs:
        w = _seg_w(p, text, color, font)
        if used + w <= width:
            out.append((text, color))
            used += w
            continue
        while text and used + _seg_w(p, text + ELLIPSIS, color, font) > width:
            text = text[:-1]
        out.append((text.rstrip() + ELLIPSIS, color))
        break
    return out


def _segs(p, x, y, segs, font):
    """Draw a line of segments from (x, y); one coloured theme.FM_PILL sits on the gold FM pill."""
    for text, color in segs:
        if color == theme.FM_PILL:
            w, h = _seg_w(p, text, color, font), p.line_height(font)
            p.round_rect(x, y, x + w, y + h, h // 2, theme.GOLD, theme.GOLD_3, 1)
            p.text(x + SEG_PAD, y, text, theme.GOLD_DEEP, font)
            x += w
        else:
            p.text(x, y, text, color, font)
            x += p.text_width(text, font)


def _pill_w(p, pill):
    return p.text_width(pill.text, "pill") + 2 * PILL_PAD_X


def _fit_pills(p, pills, width):
    """The tags that fit on one line, most important first."""
    out, used = [], 0
    for pill in pills:
        w = _pill_w(p, pill) + (PILL_GAP if out else 0)
        if used + w > width:
            break
        out.append(pill)
        used += w
    return out


def _rows(p, card, text_w, compact):
    """The card's text rows, fitted: [(kind, payload, font)]."""
    title = [(card.title, TITLE)] + ([(f"  ×{card.count}", SUBTITLE)] if card.count > 1 else [])
    rows = [("segs", _fit(p, title, "title", text_w), "title")]
    if compact:
        rows.append(("segs", _fit(p, card.price, "small", text_w), "small"))
        return rows
    if card.subtitle:
        rows.append(("segs", _fit(p, [(card.subtitle, SUBTITLE)], "small", text_w), "small"))
    rows.append(("segs", _fit(p, card.price, "body", text_w), "body"))
    rows += [("segs", _fit(p, line, "small", text_w), "small") for line in card.extra]
    pills = _fit_pills(p, card.pills, text_w)
    if pills:
        rows.append(("pills", pills, "pill"))
    return rows


def _row_h(p, kind, font):
    return p.line_height(font) + (2 * PILL_PAD_Y + 3 if kind == "pills" else 0)


def _layout(p, cards, width, compact):
    icon_w = 0 if compact else ICON_BOX + TEXT_GAP
    text_w = width - 2 * PAD - ACCENT_W - 3 - 2 * CARD_PAD - icon_w
    out = []
    for card in cards:
        rows = _rows(p, card, text_w, compact)
        text_h = sum(_row_h(p, k, f) for k, _, f in rows)
        out.append((card, rows, max(0 if compact else ICON_BOX, text_h) + 2 * CARD_PAD))
    height = 2 * PAD + sum(h for *_, h in out) + CARD_GAP * (len(out) - 1)
    return out, height


def draw_panel(p, panel):
    if not panel.cards:
        return
    area_w, area_h = p.size()
    right = min(panel.right, area_w - 4)
    width = max(MIN_WIDTH, min(MAX_WIDTH, right - panel.left))
    left = max(4, right - width) if right - panel.left < MIN_WIDTH else panel.left
    bottom = min(panel.bottom, area_h - 4 - theme.SHADOW)

    compact = False
    layout, height = _layout(p, panel.cards, width, compact)
    if height > bottom - panel.min_top:
        compact = True
        layout, height = _layout(p, panel.cards, width, compact)
    while height > bottom - panel.min_top and len(layout) > 1:  # still too tall: show what fits
        height -= layout.pop()[2] + CARD_GAP
    top = max(panel.min_top, min(panel.top, bottom - height))

    window(p, left, top, left + width, top + height)
    y = top + PAD
    for card, rows, h in layout:
        x0, x1 = left + PAD, left + width - PAD
        p.round_rect(x0, y, x1, y + h, theme.CARD_RADIUS, theme.CARD, theme.CARD_LINE, 2)
        p.round_rect(x0 + 3, y + 4, x0 + 3 + ACCENT_W, y + h - 4, 2, card.accent)
        tx = x0 + 3 + ACCENT_W + CARD_PAD
        if not compact:
            if card.icon:  # centred in its box: MeowDB's icons are ~26-32px, the screenshot's crops ~40
                iw, ih = Image.open(io.BytesIO(card.icon)).size
                p.image(tx + (ICON_BOX - iw) // 2, y + (h - ICON_BOX) // 2 + (ICON_BOX - ih) // 2, card.icon)
            tx += ICON_BOX + TEXT_GAP
        ty = y + (h - sum(_row_h(p, k, f) for k, _, f in rows)) // 2
        for kind, payload, font in rows:
            if kind == "pills":
                px, ph = tx, _row_h(p, kind, font) - 2
                for pill in payload:  # the site's tags: solid with white words; a quiet one is a lilac chip
                    pw = _pill_w(p, pill)
                    fill, ink = (theme.FIELD, pill.color) if pill.quiet else (pill.color, "#ffffff")
                    p.round_rect(px, ty + 1, px + pw, ty + 1 + ph, ph // 2, fill)
                    p.text(px + PILL_PAD_X, ty + PILL_PAD_Y + 1, pill.text, ink, "pill")
                    px += pw + PILL_GAP
            else:
                _segs(p, tx, ty, payload, font)
            ty += _row_h(p, kind, font)
        y += h + CARD_GAP


# ---- labels beside the shop rows ---------------------------------------------

LABEL_PAD_X, LABEL_PAD_Y = 7, 2


def _label_block(p, right, center_y, lines, font):
    """Right-aligned block of coloured text lines in a small window."""
    line_h = p.line_height(font)
    widths = [sum(p.text_width(t, font) for t, _ in line) for line in lines]
    top = center_y - line_h * len(lines) // 2
    left = right - max(widths)
    x0, x1 = left - LABEL_PAD_X, right + LABEL_PAD_X
    window(p, x0, top - LABEL_PAD_Y, x1, top + line_h * len(lines) + LABEL_PAD_Y, r=8, shadow=2)
    for i, line in enumerate(lines):
        x = right - widths[i]
        for text, color in line:
            p.text(x, top + i * line_h, text, color, font)
            x += p.text_width(text, font)


def draw_labels(p, labels):
    for lb in labels:
        if lb.lines:  # nothing worth saying about this row: no box at all
            _label_block(p, lb.right, lb.center_y, lb.lines, "label" if len(lb.lines) == 1 else "label_small")


def draw_badge(p, cx, y, text, tone="info", font="label", center=False):
    """A pill-shaped window centred on x = `cx`, its top at `y` (or its middle,
    with `center`): the status line, and the big "expand the minimap" message."""
    fill, color = TONES[tone]
    pad_x, pad_y = (22, 8) if font == "big" else (14, 4)
    dot = 10 if tone == "info" else 0  # little "live" light, like a server-status pill
    w = p.text_width(text, font) + 2 * pad_x + (dot + 6 if dot else 0)
    h = p.line_height(font) + 2 * pad_y
    x0 = cx - w // 2
    y0 = y - h // 2 if center else y
    window(p, x0, y0, x0 + w, y0 + h, fill=fill, r=h // 2)
    tx = x0 + pad_x
    if dot:
        dy = y0 + (h - dot) // 2
        p.round_rect(tx, dy, tx + dot, dy + dot, dot // 2, theme.GOOD_1, theme.UP, 1)
        tx += dot + 6
    p.text(tx, y0 + pad_y, text, color, font)


@functools.lru_cache(maxsize=256)
def scaled_png(png, scale):
    """An icon `scale` times its size, as PNG bytes (the same bytes at 1)."""
    if scale == 1:
        return png
    icon = Image.open(io.BytesIO(png)).convert("RGBA")
    icon = icon.resize((max(1, round(icon.width * scale)), max(1, round(icon.height * scale))), Image.LANCZOS)
    out = io.BytesIO()
    icon.save(out, "PNG")
    return out.getvalue()


class Scaling:
    """A painter's units to its pixels: `scale` times (settings.overlay_scale). At 1, nothing changes."""

    scale = 1

    def px(self, v):
        return v if self.scale == 1 else v * self.scale

    def units(self, v):
        return v if self.scale == 1 else v / self.scale


class PilPainter(Scaling):
    """Draws onto a PIL image (used by --image mode and tests), `scale` times the overlay's own size."""

    def __init__(self, img, scale=1):
        self.img, self.scale = img, scale
        self.draw = ImageDraw.Draw(img)
        self.fonts = {name: theme.pil_font(name, scale) for name in theme.FONTS}

    def size(self):
        return tuple(self.units(v) for v in self.img.size)

    def text_width(self, text, font):
        return self.units(self.draw.textlength(text, font=self.fonts[font]))

    def line_height(self, font):
        return self.units(int(self.fonts[font].size * 1.3))

    def text(self, x, y, text, color, font):
        self.draw.text((self.px(x), self.px(y)), text, fill=color, font=self.fonts[font])

    def round_rect(self, x0, y0, x1, y1, r, fill, outline=None, width=1):
        x0, y0, x1, y1, r = (self.px(v) for v in (x0, y0, x1, y1, r))
        r = max(0, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
        self.draw.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=fill, outline=outline,
                                    width=max(1, round(self.px(width))) if outline else 0)

    def image(self, x, y, png):
        icon = Image.open(io.BytesIO(scaled_png(png, self.scale))).convert("RGBA")
        self.img.paste(icon, (int(self.px(x)), int(self.px(y))), icon)
