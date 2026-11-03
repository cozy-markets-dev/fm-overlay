"""Downloads MeowDB's whole item list (names, NPC prices, item info) and every Free Market
price from Cozy Markets, which keeps them fresh from MeowDB, so shop lookups are answered
from the local database instead of waiting on MeowDB item by item. MeowDB is still asked
directly about anything the downloads don't cover (an item newer than the list, or a price
older than the TTL).

Both come at startup, then every POLL. A poll sends the ETag of the copy we have, so one
that finds nothing new is a 304 and costs next to nothing. The ETags live in the database's
meta table: clearing the database removes them along with the data, so the next
poll downloads it all again. They go with this PC's anonymous token (sync.read), which names
nothing but this install: nothing about you or your scans is sent (that's sync.py, and it's opt-out).
"""
import logging
import time
from urllib.parse import urlparse

import requests

from .sync import CLIENT, base_url, read, server

log = logging.getLogger("mprice_decoder")

POLL = 15 * 60          # s between polls
RETRY = (60, 1800)      # s to wait after a failed download: at first, and at most
TIMEOUT = 30
ETAG = "cozy_feed_etag"  # meta key prefix: "<prefix> <download> <server>" -> ETag of the copy we have

# (name, path, the list in its JSON, ItemDB method that stores it)
DOWNLOADS = (("catalog", "/api/catalog", "items", "import_catalog"),
             ("fm-prices", "/api/fm-prices", "fm", "import_fm"))


class Feed:
    """The background downloader. `run()` is the thread; `step()` is one round."""

    def __init__(self, db, settings, session=None, reader=None):
        self.db, self.settings = db, settings
        self.reader = reader  # sync.Sync: the token to read with (sync.read)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = CLIENT
        self.retry = 0  # s we last waited after a failure (doubles each time)

    def run(self):
        while True:
            time.sleep(self.step())

    def step(self):
        """Download whatever's changed. Returns seconds to wait before the next round."""
        try:
            url = base_url(self.settings.cozy_url)
        except ValueError:
            log.warning("price feed off: cozy_url %r isn't https", self.settings.cozy_url)
            return POLL
        try:
            for download in DOWNLOADS:
                self.download(url, *download)
        except Exception as e:
            self.retry = min(self.retry * 2, RETRY[1]) if self.retry else RETRY[0]
            log.warning("price feed: can't download from %s (%s): trying again in %ds",
                        urlparse(url).hostname, e, self.retry)
            return self.retry
        self.retry = 0
        return POLL

    def download(self, url, name, path, field, store):
        key = f"{ETAG} {name} {server(url)}"
        etag = self.db.meta(key)
        t = time.perf_counter()
        r = read(self.session, url + path, self.reader, timeout=TIMEOUT, headers={"If-None-Match": etag} if etag else {})
        if r.status_code == 304:
            return
        r.raise_for_status()
        rows = r.json().get(field)
        if not isinstance(rows, list):
            raise ValueError(f"{path} has no {field!r} list")
        stored = getattr(self.db, store)(rows)
        self.db.set_meta(key, r.headers.get("ETag"))  # none sent: always download it whole
        log.info("price feed: %s, %d of %d rows stored (%d KB) in %.2fs",
                 name, stored, len(rows), len(r.content) // 1024, time.perf_counter() - t)
