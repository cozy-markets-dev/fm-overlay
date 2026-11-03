"""MaplePrice's clickable windows (F10's options, adding a price alert), drawn by the Cozy Markets website's own
React code and styles (cozy-markets/frontend/src/mapleprice, built into assets/ui) in pywebview windows: Edge
WebView2, part of Windows 10 and 11. pywebview needs the main thread, as Tk does for the overlay, so this is its
own process (run.py --ui), started and told what to show by ui_host.py.

Lines of JSON both ways. From MaplePrice: {"state": ...} (what the pages show), {"show"|"toggle"|"hide": window},
{"reply": id, "result": ...}, {"quit": true}. To it: {"call": name, "args": ..., "id": n} (a page asking),
{"event": "ready"}, {"event": "hidden", "window": name}."""
import itertools
import json
import os
import sys
import threading
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "assets" / "ui" / "mapleprice.html"
ICON = Path(__file__).resolve().parent / "assets" / "mapleprice.ico"  # Cozy, the pet
WIDTH = {"options": 360, "alert": 400}  # the page's width; the window is as much wider as its frame
HEIGHT = 420  # until the page says how tall it is
TITLE = {"options": "MaplePrice", "alert": "MaplePrice: add monitor"}
ASK_TIMEOUT = 60  # s a page waits on MaplePrice (adding an alert waits on Cozy Markets)
APP_ID = "CozyMarkets.MaplePrice"


def taskbar_id():
    """MaplePrice's own taskbar button, with its windows' icon (Cozy). Without it Windows takes them for Python's
    (pythonw.exe runs them) and shows Python's icon there, whatever the windows' own. Both processes call this, so
    their windows share one button."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass


class Bridge:
    """What a window's page may ask (window.pywebview.api)."""

    def __init__(self, child, name):
        self._child, self._name = child, name

    def state(self):
        return self._child.state

    def call(self, name, args=None):
        return self._child.ask(name, args or {})

    def fit(self, height, inner_width, inner_height):
        """The page's height, and the room it has now (its viewport): the window keeps to the page."""
        self._child.fit(self._name, int(height), int(inner_width), int(inner_height))

    def close(self):
        self._child.hide(self._name)


class Child:
    def __init__(self, inp, out, webview, exit=os._exit):
        self.inp, self.out, self.webview, self.exit = inp, out, webview, exit
        self.state = None
        self.windows, self.shown = {}, set()
        self.size = {}   # window -> the (width, height) it was made with, frame and all
        self.frame = {}  # window -> (width, height) of its frame: what it has that its page's viewport doesn't
        self.wanted = {}  # window -> the size its page wants, given it when it's next shown
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.waiting = {}  # id -> [threading.Event, result]

    def send(self, msg):
        with self.lock:
            self.out.write((json.dumps(msg) + "\n").encode())
            self.out.flush()

    def ask(self, name, args):
        n = next(self.ids)
        slot = self.waiting[n] = [threading.Event(), None]
        self.send({"call": name, "args": args, "id": n})
        slot[0].wait(ASK_TIMEOUT)
        return self.waiting.pop(n)[1]

    def window(self, name):
        w = self.windows.get(name)
        if w is None:
            w = self.windows[name] = self.webview.create_window(
                TITLE[name], f"{PAGE}#{name}", js_api=Bridge(self, name), width=WIDTH[name], height=HEIGHT,
                resizable=False, on_top=True, hidden=True, background_color="#FFFFFF")
            self.size[name] = WIDTH[name], HEIGHT
            w.events.closing += lambda: self.closing(name)
        return w

    def closing(self, name):
        """Its X: hidden instead, and kept for next time (False stops the close)."""
        self.hide(name)
        return False

    def fit(self, name, height, inner_width, inner_height):
        """Size the window so its page has WIDTH and `height`. Its frame (title bar, edges) is measured once, from
        the first fit, while it's still the size it was made: a later one could cross a resize on its way."""
        w = self.windows.get(name)
        if w is None or inner_width <= 0 or inner_height <= 0:
            return
        if name not in self.frame:
            width, tall = self.size[name]
            self.frame[name] = width - inner_width, tall - inner_height
        frame_w, frame_h = self.frame[name]
        self.wanted[name] = WIDTH[name] + frame_w, height + frame_h
        if name in self.shown:  # pywebview's resize shows a hidden window too: a hidden one waits for show()
            w.resize(*self.wanted[name])

    def hide(self, name):
        w = self.windows.get(name)
        if w is not None and name in self.shown:
            self.shown.discard(name)
            w.hide()
            self.send({"event": "hidden", "window": name})

    def show(self, name):
        w = self.window(name)
        w.show()
        w.restore()
        self.shown.add(name)
        if name in self.wanted:
            w.resize(*self.wanted[name])

    def push(self):
        script = f"window.__maple && window.__maple.update({json.dumps(self.state)})"
        for w in list(self.windows.values()):
            if w.events.loaded.is_set():
                w.run_js(script)

    def read(self):
        for line in self.inp:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "reply" in msg:
                slot = self.waiting.get(msg["reply"])
                if slot:
                    slot[1] = msg.get("result")
                    slot[0].set()
            elif "state" in msg:
                self.state = msg["state"]
                self.push()
            elif "show" in msg:
                self.show(msg["show"])
            elif "toggle" in msg:
                name = msg["toggle"]
                if name in self.shown:
                    self.hide(name)
                else:
                    self.show(name)
            elif "hide" in msg:
                self.hide(msg["hide"])
            elif msg.get("quit"):
                break
        threading.Timer(3, self.exit, (0,)).start()  # MaplePrice is closing (or gone): so is this, one way or another
        for w in list(self.windows.values()):
            w.destroy()


def run():
    import webview
    taskbar_id()
    inp, out = os.fdopen(0, "rb", 0), os.fdopen(1, "wb", 0)
    child = Child(inp, out, webview)
    for name in WIDTH:  # made now, hidden until asked for and kept (one made later opens minimized)
        child.window(name)
    webview.start(lambda: (child.send({"event": "ready"}), child.read()), gui="edgechromium", http_server=True,
                  private_mode=True, icon=str(ICON))
    sys.exit(0)
