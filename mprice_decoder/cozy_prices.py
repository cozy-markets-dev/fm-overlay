"""Cozy Markets' own prices: what this week's scans say each item goes for (the usual price, from sales or else
listings), the cheapest one on offer now and where, and the last sale. The overlay's cards and hover cards
ask for an item's price here; the whole list (/api/cozy-prices, about a thousand items) is fetched from Cozy
Markets when the copy is over FRESH old, sending its ETag, so a check that finds nothing new is a 304.
Items Cozy Markets has no price for fall back to MeowDB's FM price (feed.py).

For an equip the decoder read the stats of, same_stats asks Cozy Markets what other shops listed it for with
exactly those stats (its item history, /api/items/{id}/sightings, filtered to those stats or better, then to
the same ones here)."""
import logging
import statistics
import threading
import time
from dataclasses import dataclass

import requests

from .sync import CLIENT, base_url, read

log = logging.getLogger("mprice_decoder")

FRESH = 60    # s a copy (or a same-stats answer) is used before Cozy Markets is asked again
RETRY = 30    # s before asking again after a failed request
TIMEOUT = 5   # a shop's cards wait on this, so not long
LOOKBACK = 14 * 86400  # s: same-stats listings seen this recently count


@dataclass
class CozyPrice:
    usual: float | None = None  # sold median once there are enough sales ("sold"), else listings' ("day"/"week")
    basis: str | None = None
    listings: int = 0           # this week's
    sales: int = 0
    low: float | None = None    # cheapest per-unit price on offer now, and where
    low_shop: str | None = None
    low_channel: int | None = None
    low_fm_room: int | None = None
    low_map: str | None = None
    last_sold: float | None = None
    trend: str | None = None    # "up" | "down" | "flat"
    trend_pct: float | None = None

    @classmethod
    def of(cls, row):
        return cls(**{k: row.get(k) for k in cls.__dataclass_fields__ if row.get(k) is not None})


def exact(stats):
    """Equipment stats as Cozy Markets keeps them (and the decoder reads them): the nonzero ones, and slots left."""
    return {k: v for k, v in (stats or {}).items() if v or k == "slots"}


@dataclass
class SameStats:
    """Other shops' listings of an equip with exactly the same stats, seen in the last LOOKBACK. A listing seen
    on several visits counts once (by shop and price)."""
    listings: int = 0
    low: float | None = None
    median: float | None = None   # what they typically ask: a troll's 10x price doesn't move it
    last_sold: float | None = None
    cheapest: dict | None = None  # the cheapest not seen sold: {price, shop, channel, fm_room, map}


def same_of(rows, want, seller):
    """SameStats from an item's sightings (rows), for stats `want`, leaving out `seller`'s own shop."""
    found = {}
    for r in rows:
        if exact(r.get("stats")) != want or r.get("shop") == seller or r.get("unit_price") is None:
            continue
        key = r.get("shop"), float(r["unit_price"])
        listing = found.setdefault(key, {"price": key[1], "sold": False, "at": 0.0, "shop": r.get("shop"),
                                         "channel": r.get("channel"), "fm_room": r.get("fm_room"),
                                         "map": r.get("map_name")})
        listing["sold"] = listing["sold"] or bool(r.get("sold"))
        listing["at"] = max(listing["at"], float(r.get("at") or 0))
    prices = [l["price"] for l in found.values()]
    sold = [l for l in found.values() if l["sold"]]
    unsold = [l for l in found.values() if not l["sold"]]
    cheapest = min(unsold, key=lambda l: l["price"], default=None)
    return SameStats(len(prices), min(prices, default=None), statistics.median(prices) if prices else None,
                     max(sold, key=lambda l: l["at"])["price"] if sold else None,
                     cheapest and {k: cheapest[k] for k in ("price", "shop", "channel", "fm_room", "map")})


class CozyPrices:
    """`get(item_id)` is called from lookup threads: the first to find the copy stale fetches it, the rest wait."""

    def __init__(self, settings, session=None, clock=time.monotonic, wall=time.time, reader=None):
        self.settings, self.clock, self.wall = settings, clock, wall
        self.reader = reader  # sync.Sync: the token to read with (sync.read)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = CLIENT
        self.prices, self.etag, self.next_at = {}, None, None
        self.same = {}  # (item id, stats, seller) -> (when asked, SameStats)
        self.lock = threading.Lock()

    def get(self, item_id):
        self.refresh()
        return self.prices.get(item_id)

    def refresh(self):
        with self.lock:
            if self.next_at is not None and self.clock() < self.next_at:
                return
            try:
                url = base_url(self.settings.cozy_url) + "/api/cozy-prices"
                r = read(self.session, url, self.reader, timeout=TIMEOUT,
                         headers={"If-None-Match": self.etag} if self.etag else {})
                if r.status_code != 304:
                    r.raise_for_status()
                    rows = r.json().get("cozy")
                    if not isinstance(rows, list):
                        raise ValueError("no 'cozy' list")
                    prices = {}
                    for row in rows:
                        try:
                            prices[int(row["item_id"])] = CozyPrice.of(row)
                        except (KeyError, TypeError, ValueError, AttributeError):
                            continue
                    self.prices, self.etag = prices, r.headers.get("ETag")
                    log.info("Cozy Markets prices: %d items", len(prices))
                self.next_at = self.clock() + FRESH
            except Exception as e:  # keep the copy we have; MeowDB's prices cover the rest
                log.warning("couldn't get Cozy Markets prices (%s): trying again in %ds", e, RETRY)
                self.next_at = self.clock() + RETRY

    def same_stats(self, item_id, stats, seller=None):
        """SameStats for an equip with these stats (the decoder's), leaving out `seller`'s own shop; None if
        there are no stats or Cozy Markets can't be asked."""
        want = exact(stats)
        if not want:
            return None
        key = item_id, tuple(sorted(want.items())), seller
        asked, answer = self.same.get(key, (None, None))
        if asked is not None and self.clock() - asked < FRESH:
            return answer
        try:
            url = base_url(self.settings.cozy_url) + f"/api/items/{int(item_id)}/sightings"
            params = {"stats": ",".join(f"{k}:{v}" for k, v in want.items()),
                      "since": int(self.wall() - LOOKBACK), "size": 100}
            r = read(self.session, url, self.reader, params=params, timeout=TIMEOUT)
            if r.status_code == 404:  # Cozy Markets has never seen the item
                rows = []
            else:
                r.raise_for_status()
                rows = r.json().get("rows") or []
        except Exception as e:
            log.warning("couldn't ask Cozy Markets about item %s with stats %s (%s)", item_id, want, e)
            return None
        answer = same_of(rows, want, seller)
        self.same[key] = self.clock(), answer
        return answer
