"""MaplePrice's clickable windows in React (ui_child.py, in its own process): started from here, told what to
show, and their pages' requests handed to the overlay's Tk thread. WebOptions and WebAlert stand in for
overlay.py's OptionsWindow and AlertDialog, which stay for a PC where WebView2 can't start (Fallback picks)."""
import base64
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

from .alerts import mesos
from .data import DEFAULT_DB
from .i18n import t
from .settings import LANGUAGES, OVERLAY_SCALES, volume

log = logging.getLogger("mprice_decoder")

READY = 15          # s to wait for the windows' process to start (WebView2's first start is the slow one)
SWITCHES = ("shop_prices", "hover_prices", "price_alerts", "alert_sound", "bazaar_sync", "bazaar_public")
LOG = DEFAULT_DB.parent / "ui-log.txt"  # the windows' process's own errors


def command():
    """How to start the windows' process: this exe again, or run.py from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ui"]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "run.py"), "--ui"]


class UiHost:
    """The windows' process. `on_message` gets what it says (a page's call, a window hidden, or
    {"event": "gone"} if it ends), on the reader thread."""

    def __init__(self, on_message, cmd=None):
        self.on_message, self.cmd = on_message, cmd or command()
        self.proc = None
        self.ready = threading.Event()
        self.lock = threading.Lock()

    def start(self, wait=READY):
        """Start it and wait until its windows can open: False if it can't (no WebView2, no pywebview)."""
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            errors = open(LOG, "ab")
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            log.exception("couldn't start the windows' process")
            return False
        threading.Thread(target=self._read, daemon=True, name="ui-read").start()
        for _ in range(int(wait * 10)):
            if self.ready.wait(0.1) or self.proc.poll() is not None:  # ready, or it ended (no WebView2, ...)
                break
        if not self.ready.is_set():
            log.warning("the windows' process didn't start: the Tk windows are used (see %s)", LOG)
            self.close()
            return False
        log.info("React windows ready (pid %s)", self.proc.pid)
        return True

    def alive(self):
        return self.ready.is_set() and self.proc is not None and self.proc.poll() is None

    def send(self, msg):
        if self.proc is None:
            return False
        try:
            with self.lock:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                self.proc.stdin.flush()
            return True
        except (OSError, ValueError):  # it's gone
            return False

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.send({"quit": True})
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "ready":
                self.ready.set()
            else:
                self.on_message(msg)
        if self.ready.is_set():
            log.warning("the windows' process ended (see %s)", LOG)
            self.on_message({"event": "gone"})


def avatar_url(user):
    """A Discord picture's address, for an avatar hash as the server keeps it; else None."""
    hash_, who = user.get("avatar"), user.get("discordId")
    if (isinstance(hash_, str) and isinstance(who, str) and who.isdigit()
            and len(hash_.removeprefix("a_")) == 32 and all(c in "0123456789abcdef" for c in hash_.removeprefix("a_"))):
        return f"https://cdn.discordapp.com/avatars/{who}/{hash_}.png?size=64"
    return None


class WebOptions:
    """F10's window in React: overlay.OptionsWindow's switches and actions, and the add-an-alert window's form
    (WebAlert). handle() runs a page's request on the Tk thread; every change pushes the whole state."""

    capturing = False  # no hotkey rebinding here (overlay.OptionsWindow has none either now)

    def __init__(self, host, settings, on_change, open_site=None, on_quit=None, signin=None, open_alerts=None,
                 later=None, bazaar_status=None, unwatch=None, set_max=None, test_sound=None):
        self.host, self.settings, self.on_change = host, settings, on_change
        self.bazaar_status = bazaar_status  # bazaar.BazaarSync's line, when sign-in (so the sync) is there
        self.open_site, self.on_quit, self.signin, self.open_alerts = open_site, on_quit, signin, open_alerts
        self.later = later  # Tk's root.after
        self.alert = None   # the WebAlert being filled in
        self.unwatch = unwatch  # stop watching an item (its watch id): MaplePrice asks the server, then says so
        self.set_max = set_max  # (watch id, mesos): the most you'd pay for it, changed (likewise)
        self.test_sound = test_sound  # plays the alert's chime at the saved volume
        self.watch_note = ""    # why the last change to the watch list didn't go through
        self.watching = None    # {"items": [{id, itemId, name, maxPrice, icon}], "most": n}: signed in, once asked

    def state(self):
        s, login = self.settings, self.signin
        signin = None
        if login is not None:
            user = login.user
            signin = {"user": {"name": user.get("name") or user.get("username") or "Discord user",
                               "avatar": avatar_url(user)} if user else None,
                      "working": login.working, "message": login.message,
                      "code": login.attempt.user_code if login.attempt else None}
        return {"settings": {"shopPrices": s.shop_prices, "hoverPrices": s.hover_prices,
                             "priceAlerts": s.price_alerts, "alertSound": s.alert_sound, "hoverKey": s.hover_key,
                             "alertKey": s.alert_key, "bazaarSync": s.bazaar_sync, "bazaarPublic": s.bazaar_public,
                             "alertVolume": s.alert_volume, "language": s.language, "overlayScale": s.overlay_scale,
                             "overlayScales": list(OVERLAY_SCALES)},
                "bazaar": self.bazaar_status() if self.bazaar_status else None,
                "site": self.open_site is not None, "signin": signin,
                "alert": self.alert.form if self.alert else None,
                "watching": {**self.watching, "note": self.watch_note} if self.watching and login is not None
                            and login.user else None}

    def push(self):
        self.host.send({"state": self.state()})

    def toggle(self):
        self.push()
        self.host.send({"toggle": "options"})

    def refresh(self):
        self.push()

    show_settings = refresh

    def handle(self, msg):
        """One message from the windows' process, on the Tk thread."""
        if msg.get("event") == "hidden" and msg.get("window") == "alert" and self.alert:
            self.alert.opened = False
            return
        if "call" not in msg:
            return
        name, args = msg["call"], msg.get("args") or {}
        result = None
        try:
            result = self._do(name, args)
        except Exception:
            log.exception("window request %s failed", name)
        finally:
            self.host.send({"reply": msg.get("id"), "result": result})
            self.push()

    def _do(self, name, args):
        login = self.signin
        if name == "switch" and args.get("field") in SWITCHES:
            setattr(self.settings, args["field"], bool(args.get("on")))
            self.on_change()
        elif name == "volume" and volume(args.get("value")) is not None:
            self.settings.alert_volume = volume(args["value"])
            self.on_change()
        elif name == "test_sound" and self.test_sound:
            self.test_sound()
        elif name == "language" and args.get("code") in LANGUAGES:
            self.settings.language = args["code"]
            self.on_change()
        elif name == "overlay_scale" and args.get("value") in OVERLAY_SCALES:
            self.settings.overlay_scale = float(args["value"])
            self.on_change()
        elif name == "site" and self.open_site:
            self.open_site()
        elif name == "quit" and self.on_quit:
            self.on_quit()
        elif name == "my_alerts" and self.open_alerts:
            self.open_alerts()
        elif name == "unwatch" and self.unwatch and isinstance(args.get("id"), int):
            self.unwatch(args["id"])
        elif name == "set_max" and self.set_max and isinstance(args.get("id"), int):
            price = mesos(args.get("price") if isinstance(args.get("price"), str) else "")
            if price is None:
                return {"ok": False, "text": t("Type a price, like 2,500 or 2.5k.")}
            for item in (self.watching or {}).get("items", []):  # shown at once; the server's list follows
                if item["id"] == args["id"]:
                    item["maxPrice"] = price
            self.watch_note = ""
            self.set_max(args["id"], price)
            return {"ok": True}
        elif name == "add_alert" and self.alert:
            self.alert.add(args.get("price"))
        elif login is not None and name in ("sign_in", "sign_in_again", "cancel_sign_in", "sign_out"):
            {"sign_in": login.sign_in, "sign_in_again": login.reopen, "cancel_sign_in": login.cancel,
             "sign_out": login.sign_out}[name]()


class WebAlert:
    """The add-an-alert window in React: overlay.AlertDialog's part (submit, done, is_open, close) for its form."""

    serial = 0

    def __init__(self, options, item_name, suggested="", usual=None, icon=None, submit=None):
        WebAlert.serial += 1
        self.options, self.submit = options, submit
        self.form = {"id": WebAlert.serial, "name": item_name, "suggested": suggested, "usual": usual,
                     "icon": "data:image/png;base64," + base64.b64encode(icon).decode() if icon else None,
                     "busy": False, "result": None}
        self.opened = True
        options.alert = self
        options.push()
        options.host.send({"show": "alert"})

    def add(self, typed):
        if self.form["busy"]:
            return
        price = mesos(typed or "")
        if price is None:
            self.form["result"] = {"ok": False, "text": t("Type a price, like 2,500 or 2.5k.")}
            return
        self.form.update(busy=True, result=None)
        self.submit(price)

    def is_open(self):
        return self.opened and self.options.alert is self

    def done(self, ok, text):
        """The answer: added (the window closes shortly), or why not (try again)."""
        if self.options.alert is not self:
            return
        self.form.update(busy=False, result={"ok": ok, "text": text})
        self.options.push()
        if ok and self.options.later:
            self.options.later(1500, self.close)

    def close(self):
        if self.is_open():
            self.opened = False
            self.options.host.send({"hide": "alert"})


class Fallback:
    """F10's window: the React one while its process runs, else overlay.py's Tk one."""

    def __init__(self, web, tk):
        self.web, self.tk = web, tk

    @property
    def current(self):
        return self.web if self.web is not None and self.web.host.alive() else self.tk

    @property
    def capturing(self):
        return self.tk.capturing

    def toggle(self):
        self.current.toggle()

    def refresh(self):
        self.current.refresh()

    def show_settings(self):
        self.current.show_settings()
