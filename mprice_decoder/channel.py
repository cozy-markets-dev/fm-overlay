"""Your channel, from the game's own connection: every channel is a separate
server port (ch 3 = 8542, ch 4 = 8543, ...). Only asks Windows which
connections the game has open; the game itself is never touched."""
import logging

import psutil

log = logging.getLogger("mprice_decoder")

GAME_EXE = "MapleStory.exe"
PORT_BASE = 8539   # channel = port - PORT_BASE
MAX_CHANNEL = 40   # anything past this is another server (chat on 8913, login, ...)


def channel_from_port(port):
    ch = port - PORT_BASE
    return ch if 1 <= ch <= MAX_CHANNEL else None


def channel_from_ports(ports):
    """The one channel among the game's remote ports; None when there's none
    (changing channel, cash shop) or it's ambiguous."""
    found = {channel_from_port(p) for p in ports} - {None}
    return found.pop() if len(found) == 1 else None


def read_channel():
    try:
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] == GAME_EXE:
                return channel_from_ports(c.raddr.port for c in proc.net_connections(kind="tcp")
                                          if c.raddr and c.status == psutil.CONN_ESTABLISHED)
    except psutil.Error:  # e.g. access denied: we're not admin and the game is
        log.exception("couldn't read the game's connections")
    return None


# Off the screen, for when neither the packets nor the connection tell (a VPN or ping reducer hides both): the game's
# Change Channel window, as the web Scanner reads it (cozy-markets frontend scanner/engine/channel.ts). A grid of
# buttons, 8 to a row, CH.1 at the top left, so a button's place in the grid is its number (no OCR). When the window
# opens, your channel is the blue button. Once you pick another, that one is blue and yours turns dark grey with a ◄.
# The button under the mouse is light grey.
CELL = (66, 18)    # a button, border to border, in the game's own pixels (capture.grab_game)
PITCH = (70, 22)   # from one button to the next
COLS = 8
MAX_ROWS = 6
KINDS = {
    "plain": (177, 177, 177),
    "picked": (54, 170, 221),
    "current": (106, 106, 106),
    "hovered": (201, 201, 201),
}
BACKGROUND = (245, 246, 247)  # the window behind the buttons
TOL = 14        # per colour channel: the capture may shift colours a little
PLAIN_TOL = 10  # tighter when hunting for buttons: the 165 grey of their top and bottom rows doesn't count
SWITCH_WAIT = 8.0  # s after the window closes with another channel picked for its loading screen to start


def _near(px, colour, tol):
    import numpy as np
    return (np.abs(px.astype(np.int16) - np.array(colour, dtype=np.int16)) <= tol).all(axis=-1)


def read_channel_window(rgb):
    """The Change Channel window on `rgb` (the game's frame, H x W x 3), or None when it isn't open:
    {current: the dark button's number (only there once another is picked), picked: the blue one's, count, box}."""
    import numpy as np
    h, w = rgb.shape[:2]
    for y in range(2, h - CELL[1], 4):
        # A plain button's rows above and below its label are one 66 px run of its grey: every 4th row of the frame
        # crosses one of them on each button.
        grey = np.concatenate(([False], _near(rgb[y, :, :3], KINDS["plain"], PLAIN_TOL), [False]))
        edges = np.flatnonzero(np.diff(grey.astype(np.int8)))
        for start, end in zip(edges[::2], edges[1::2]):
            if CELL[0] - 4 <= end - start <= CELL[0] + 2:
                found = _grid_from(rgb, int(start), y)
                if found:
                    return found
    return None


def _grid_from(rgb, x, y):
    """The grid around a plain button whose run starts at (x, y), checked against the window's layout."""
    h, w = rgb.shape[:2]
    top = y
    while top > 0 and y - top < CELL[1] and _near(rgb[top - 1, x + 52, :3], KINDS["plain"], PLAIN_TOL):
        top -= 1
    if y - top >= CELL[1]:
        return None
    x0, y0 = x, top - 1  # its 165 grey top row
    if _kind_at(rgb, x0, y0) != "plain":
        return None
    # Back to CH.1: left along the row, then up the first column.
    for _ in range(COLS):
        if not _kind_at(rgb, x0 - PITCH[0], y0):
            break
        x0 -= PITCH[0]
    for _ in range(MAX_ROWS):
        if not _kind_at(rgb, x0, y0 - PITCH[1]):
            break
        y0 -= PITCH[1]

    def bg(px, py):
        return 0 <= px < w and 0 <= py < h and bool(_near(rgb[py, px, :3], BACKGROUND, TOL))
    # The window's off-white around it: above the first row, and in the 4 px gap after one of the first three buttons
    # (a picture scaled by an odd amount blurs some gaps into the buttons beside them).
    above = any(bg(x0 + 10, y0 - dy) for dy in (2, 3, 4, 5))
    gap = any(bg(x0 + b * PITCH[0] + CELL[0] + dx, y0 + 9) for b in range(3) for dx in range(4))
    if not above or not gap:
        return None
    kinds = []
    for i in range(COLS * MAX_ROWS):
        k = _kind_at(rgb, x0 + (i % COLS) * PITCH[0], y0 + (i // COLS) * PITCH[1])
        if not k:
            break
        kinds.append(k)
    if len(kinds) < 2:
        return None
    number = lambda kind: kinds.index(kind) + 1 if kind in kinds else None
    rows = -(-len(kinds) // COLS)
    return {"current": number("current"), "picked": number("picked"), "count": len(kinds),
            "box": (max(0, x0 - 10), max(0, y0 - 70), min(w, x0 + COLS * PITCH[0] + 10), min(h, y0 + rows * PITCH[1] + 44))}


def _kind_at(rgb, x, y):
    """What kind of button has its top-left corner at (x, y): the colour most of it is, if any. Its label and border
    take about a tenth; the mouse can cover another third."""
    h, w = rgb.shape[:2]
    if x < 0 or y < 0 or x + CELL[0] > w or y + CELL[1] > h:
        return None
    patch = rgb[y + 2:y + CELL[1] - 2:2, x + 2:x + CELL[0] - 2:3, :3]
    n = patch.shape[0] * patch.shape[1]
    counts = {k: int(_near(patch, c, TOL).sum()) for k, c in KINDS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] / n >= 0.4 else None


def is_loading(rgb, skip=None):
    """A loading screen: nearly all of the frame black (`skip`, a box drawn over it, left out)."""
    import numpy as np
    h, w = rgb.shape[:2]
    ys = np.arange(int(h * 0.1) + 4, int(np.ceil(h * 0.98)), 8)
    xs = np.arange(int(w * 0.02) + 4, int(np.ceil(w * 0.98)), 8)
    if not len(ys) or not len(xs):
        return False
    dark = rgb[np.ix_(ys, xs)][..., :3].max(axis=-1) < 16
    if skip:
        keep = ~(((xs >= skip[0]) & (xs < skip[2]))[None, :] & ((ys >= skip[1]) & (ys < skip[3]))[:, None])
        return keep.any() and dark[keep].mean() >= 0.95
    return dark.mean() >= 0.95


class ScreenChannel:
    """Your channel as the Change Channel window last showed it: while it's open, yours; once it closes with another
    picked, that one if a loading screen follows (you moved), else still yours. `channel` is None until it's been
    seen. follow() takes each frame the shop watch looks at."""

    def __init__(self):
        self.channel = None
        self.window = None     # (current, picked) while it's open
        self.switching = None  # (from, to, until) once it closed with another picked

    def follow(self, rgb, now):
        win = read_channel_window(rgb)
        if is_loading(rgb, win["box"] if win else None):
            sw = (self.window if self.window and self.window[0] != self.window[1]
                  else self.switching[:2] if self.switching else None)
            if sw:
                log.info("channel from the Change Channel window: %s -> %s", sw[0], sw[1])
                self.channel = sw[1]
            self.window = self.switching = None
            return self.channel
        if win:
            self.switching = None
            # Just opened, yours is the blue button; once another is picked, the dark one. Under the mouse, either
            # turns light grey: then what it was the last look.
            current = win["current"] or (self.window and self.window[0]) or win["picked"]
            if current:
                picked = win["picked"] or (self.window and self.window[1]) or current
                self.window = (current, picked)
                if self.channel != current:
                    log.info("channel %s from the Change Channel window", current)
                self.channel = current
        elif self.window:
            current, picked = self.window
            self.window = None
            self.channel = current
            if picked != current:
                self.switching = (current, picked, now + SWITCH_WAIT)
        elif self.switching and now > self.switching[2]:
            self.switching = None  # no loading screen: you stayed
        return self.channel
