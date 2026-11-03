"""Price alerts in MaplePrice: Cozy Markets' Alerts (items you watch, each with the most you'd pay; a scanner
finding one at or under that price is an alert), for the Discord account MaplePrice is signed in with (auth.py),
through MaplePrice's own routes (/trade-api/desktop). While settings.price_alerts is on, new ones pop up over the
game; settings.alert_key adds the hovered item. What's unread stays unread on the website: MaplePrice only shows
each new one once."""
import logging
import re
import threading

from .auth import AuthError

log = logging.getLogger("mprice_decoder")

POLL = 30    # s between looks for new alerts (Cozy Markets looks for them every 45 s at most)
FIRST = 5    # s after starting (or switching alerts on, or signing in) before the first look
SHOWN = 3    # alerts popped up at once; the rest are counted


def mesos(text):
    """A price as typed: "2499", "2,499", "2.5k", "1.2m", "3b" -> whole mesos, or None."""
    m = re.fullmatch(r"\s*([\d,]*\.?\d+)\s*([kmb]?)\s*(mesos?)?\s*", str(text).lower())
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2)]
    except ValueError:
        return None
    return int(round(value)) if value >= 1 else None


class Alerts:
    """`run()` is the thread: new alerts go to `events` as ("alerts", [hit, ...]) (the server's JSON, oldest first),
    and the first look's unread count as ("alerts_unread", n)."""

    def __init__(self, service, settings, events):
        self.service, self.settings, self.events = service, settings, events
        self.last = None  # the newest alert already seen here; None: not looked yet since signing in or switching on
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(POLL if self.last is not None else FIRST):
            self.step()

    def step(self):
        if not self.settings.price_alerts or self.service.session is None:
            self.last = None  # back on: start from what's new then, not from everything since
            return
        try:
            body = self.service.api("GET", "/watch-hits?limit=20")
        except AuthError as e:
            log.warning("couldn't look for price alerts: %s", e)
            return
        hits = [h for h in body.get("hits") or [] if isinstance(h, dict) and isinstance(h.get("id"), int)]
        newest = max((h["id"] for h in hits), default=0)
        if self.last is None:
            self.last = newest
            if isinstance(body.get("unread"), int) and body["unread"] > 0:
                self.events.put(("alerts_unread", body["unread"]))
            return
        new = sorted((h for h in hits if h["id"] > self.last), key=lambda h: h["id"])
        self.last = max(self.last, newest)
        if new:
            log.info("price alerts: %s", [(h.get("itemName"), h.get("price")) for h in new])
            self.events.put(("alerts", new))

    def watching(self):
        """The items you watch, newest first ([{id, itemId, itemName, maxPrice}]), and how many you may: (list, most)."""
        body = self.service.api("GET", "/watches")
        watches = [w for w in body.get("watches") or [] if isinstance(w, dict) and isinstance(w.get("id"), int)]
        return watches, body.get("most") if isinstance(body.get("most"), int) else None

    def change(self, watch_id, max_price):
        """The most you'd pay for one you watch, changed: the watch as the server has it now."""
        return self.service.api("PATCH", f"/watches/{int(watch_id)}", {"maxPrice": int(max_price)})["watch"]

    def remove(self, watch_id):
        """Stop watching one (what it found stays among your alerts)."""
        self.service.api("DELETE", f"/watches/{int(watch_id)}")

    def add(self, item_id, name, max_price):
        """Watch an item (watched already: the most you'd pay, changed). The watch, as the server has it; an
        AuthError says why not (signed out, five items watched already, ...)."""
        body = self.service.api("POST", "/watches", {"itemId": int(item_id), "itemName": name[:80],
                                                     "maxPrice": int(max_price)})
        return body["watch"]
