"""Item lookup: OCR'd item name -> NPC sell price, Free Market price, and the
lowest/highest prices we've seen it listed for in player shops.

Data comes from MeowDB's MapleStory Classic API and is cached in a local SQLite
file. NPC prices never change, so they're kept forever; FM prices and seen shop
prices expire after the TTL (24h unless the database's meta says otherwise).
Names are matched with fuzzy string scoring (handles OCR typos and names the
shop UI truncates with "...").

Most of it arrives in bulk from Cozy Markets (feed.py): MeowDB's whole item list and
every FM price, which it keeps fresh. Then names are matched against the whole list
right here, and MeowDB is only asked about what the downloads don't cover.
"""
import io
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from PIL import Image
from rapidfuzz import fuzz, process

from .iconpack import BUNDLED
from .icons import feature
from .version import VERSION
from .identity import APP_ID

API = "https://meowdb.com/msclassic/api"
USER_AGENT = f"{APP_ID}-overlay/{VERSION} (personal shop price helper)"
# Kept on a local drive: SQLite locking fails on network shares like \\wsl$.
from .identity import data_directory

DEFAULT_DB = data_directory() / "items.sqlite"

MIN_SCORE = 85      # candidates scoring below this are not considered a match
EXACT_SCORE = 95    # a full-name match this good beats any prefix matches
TIE_MARGIN = 3      # keep every candidate this close to the best score
MIN_PREFIX = 8      # shortest OCR text we'll treat as a possibly-cut-off name
TTL = 24 * 3600     # default expiry of FM prices and seen shop prices (see ItemDB.ttl)
CATALOG_AT = "catalog_at"
# Where a scan came from: the game's packets (the decoder, exact), or OCR. A "backup" is OCR of a shop the decoder
# didn't have (it's down, or missed it): shared with Cozy Markets as decoded ones are. Plain "ocr" scans (from
# before the backups) stay on this PC.
SOURCES = ("decoder", "backup", "ocr")
SHARED = ("decoder", "backup")  # meta: when MeowDB's whole item list was last downloaded (names are matched locally)


def normalize(name):
    name = name.replace("’", "'").replace("`", "'").lower()
    return re.sub(r"\s+", " ", name).strip()


# An upgraded equip's tooltip adds how many times it was enhanced to its name: "Korean Fan (+7)". No item's named
# so (the only names with a "+" are 'Red " + "' and 'Green " + "'), and gendered ones ("... (M)") don't match this.
UPGRADES = re.compile(r"\s*\(?\s*\+\s*\d{1,2}\s*\)?\s*$")


def strip_upgrades(name):
    """'Korean Fan (+7)', 'KoreanFan(+7)' (OCR) -> the item's name, without the enhancement count."""
    return UPGRADES.sub("", name)


def strip_truncation(name):
    """'Hat Accuracy Scroll: Inter...' -> ('Hat Accuracy Scroll: Inter', True)."""
    stripped = re.sub(r"\s*(\.{2,}|…)\s*$", "", name)
    return stripped, stripped != name


@dataclass
class Item:
    id: int
    name: str
    price: int


@dataclass
class Market:
    usual: int | None   # 14-day median of player reports
    reports: int        # all reports behind `usual`
    sold: int = 0       # of those, reported sales and finished trades
    for_sale: int = 0   # live MeowDB listings
    cheapest_sell: int | None = None
    best_buy: int | None = None


@dataclass
class Seen:
    low: float          # per-unit prices seen in shops within TTL
    high: float
    count: int          # distinct prices seen


@dataclass
class Match:
    query: str
    items: list = field(default_factory=list)  # every plausible Item

    @property
    def found(self):
        return bool(self.items)


class MeowDB:
    def __init__(self, timeout=10):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # rows are looked up in parallel; let them share connections
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.timeout = timeout

    def _get(self, path, **params):
        r = self.session.get(f"{API}/{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def search(self, query):
        return [(i["id"], i["name"]) for i in self._get("search", q=query).get("items", [])]

    def item(self, item_id):
        """(Item, info): one request gives the NPC price, stats and icon key."""
        d = self._get(f"items/{item_id}")
        item = Item(d["id"], d["name"], int((d.get("stats") or {}).get("price") or 0))
        info = {k: d.get(k) for k in ("type", "subtype", "levelReq", "reqJob", "stats", "iconKey")}
        return item, info

    def icon_png(self, icon_key):
        r = self.session.get(f"{API}/assets/icons/{icon_key}", timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def market(self, item_id):
        r = self.session.get(f"{API}/item-market-summary", params={"itemId": item_id, "condition": "clean"},
                             timeout=self.timeout)
        if r.status_code == 400:  # "This item can't be traded on the Free Market": no price, and none coming
            return Market(None, 0)
        r.raise_for_status()
        d = r.json()
        sold = (d.get("soldChecks") or 0) + (d.get("finishedTrades") or 0)
        return Market(d.get("usual"), (d.get("priceChecks") or 0) + sold, sold,
                      d.get("forSale") or 0, d.get("cheapestSell"), d.get("bestBuy"))


class ItemDB:
    """Local cache in front of MeowDB. Safe to call from several threads: SQLite
    access is serialised, network requests are not (they run outside the lock)."""

    def __init__(self, path=DEFAULT_DB, source=None, clock=time.time, mapper=map, icons=None):
        self.source = source or MeowDB()
        self.icons = icons if icons is not None else BUNDLED  # every item's icon, shipped with mprice_decoder
        self.mapper = mapper  # e.g. a thread pool's map, to fetch candidates in parallel
        self.clock = clock
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL,
                fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS searches (
                query TEXT PRIMARY KEY, results TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS market_v2 (
                item_id INTEGER PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS icons (
                item_id INTEGER PRIMARY KEY, png BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS item_info (
                item_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS icon_colors (
                item_id INTEGER PRIMARY KEY, r REAL, g REAL, b REAL);
            CREATE TABLE IF NOT EXISTS seen (
                item_id INTEGER NOT NULL, unit_price REAL NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY (item_id, unit_price));

            -- Permanent market log (never expires): one row per F8 scan, one per
            -- listing in it. This is what's shared with Cozy Markets (sync.py).
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                shop_title TEXT, seller TEXT, listings INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, at REAL NOT NULL,
                item_id INTEGER, ocr_name TEXT NOT NULL, qty INTEGER NOT NULL,
                price INTEGER, unit_price REAL, confirmed INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS sightings_item ON sightings(item_id, at);
            CREATE INDEX IF NOT EXISTS sightings_scan ON sightings(scan_id);
            CREATE TABLE IF NOT EXISTS shop_icons (       -- icon as the shop shows it
                item_id INTEGER PRIMARY KEY, png BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        added_source = False
        for table, column in (("scans", "channel INTEGER"), ("scans", "fm_room INTEGER"), ("scans", "map_name TEXT"),
                              ("sightings", "sold INTEGER NOT NULL DEFAULT 0"),
                              ("scans", "scanned_by TEXT"),
                              # shops read from the game's packets (packets.py): how many it had left, an equipment
                              # item's stats (JSON), and whether the scan logged the shop's whole list
                              ("sightings", "stock INTEGER"), ("sightings", "stats TEXT"),
                              ("scans", "full_list INTEGER NOT NULL DEFAULT 0"),
                              # a scan that told Cozy Markets nothing it hadn't just been told: kept here, not shared
                              ("scans", "nothing_new INTEGER NOT NULL DEFAULT 0"),
                              ("scans", "source TEXT NOT NULL DEFAULT 'ocr'")):  # added after the first release
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                if table == "scans" and column.startswith("source "):
                    added_source = True
            except sqlite3.OperationalError:
                pass  # already there
        if added_source:
            # Earlier decoder builds already saved these packet-only fields. Preserve their
            # queued visits while keeping old OCR history local, including partial inventories.
            self.conn.execute("""UPDATE scans SET source = 'decoder'
                                 WHERE full_list = 1 OR EXISTS (
                                     SELECT 1 FROM sightings s WHERE s.scan_id = scans.id
                                       AND (s.stock IS NOT NULL OR s.stats IS NOT NULL))""")
            self.conn.commit()
        self.conn.execute("CREATE INDEX IF NOT EXISTS scans_source_id ON scans(source, id)")
        self.conn.execute("PRAGMA journal_mode=WAL")  # the sync and feed threads read while a scan writes
        self._import_old_sightings()
        self.on_scan = lambda scan_id: None  # called after each logged scan (Cozy Markets sharing)
        self._names = None  # the downloaded item list, for matching names (see _catalog)

    def _import_old_sightings(self):
        """One-off: copy prices seen before the permanent log existed. Those only
        kept a per-piece price, not the stack size, so qty is 0 ("unknown")."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM meta WHERE key='imported_seen'").fetchone():
                return
            self.conn.execute(
                """INSERT INTO sightings (scan_id, at, item_id, ocr_name, qty, price, unit_price, confirmed)
                   SELECT NULL, s.seen_at, s.item_id, COALESCE(i.name, ''), 0, NULL, s.unit_price, 1
                   FROM seen s LEFT JOIN items i ON i.id = s.item_id""")
            self.conn.execute("INSERT INTO meta VALUES ('imported_seen', '1')")
            self.conn.commit()

    REPEAT_WINDOW = 30 * 60  # rescanning the same shop within this doesn't log its listings again
    HEARTBEAT = 5 * 60  # a scan with nothing new is shared at most this often a shop (so it still shows as open)

    def record_scan(self, title, seller, listings, channel=None, fm_room=None, map_name=None, scanned_by=None,
                    full_list=False, source="ocr"):
        """Log one scan, with where it happened (channel, FM room or map) so you can find
        the shop again, and who scanned it (your character's name off the status bar).
        `listings` is [(item_id or None, ocr_name, qty, price, confirmed, icon_png[, sold[, stock[, stats]]])]:
        stock is how many it had left, stats an equipment item's ({"watk": 60, "slots": 7}), when known.
        Listings already logged for this shop (in the same spot) in the last REPEAT_WINDOW are skipped,
        unless they've sold since: that's logged once, as a sale. full_list: `listings` is the shop's
        whole list (read from its packets); the scan says so only if none of it was skipped, since
        Cozy Markets takes what a whole list leaves out as taken down. A scan that logs nothing new (no
        listing, or a shop found empty again) is kept here but not shared (nothing_new) if the shop's had
        one shared in the last HEARTBEAT: a shop left open, or sat in, is otherwise sent every few seconds.
        The shop is its seller, else its title, as Cozy Markets keys it (an OCR'd title reads a little
        differently each time)."""
        if source not in SOURCES:
            raise ValueError(f"scan source must be one of {SOURCES}")
        now = self.clock()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO scans (at, shop_title, seller, listings, channel, fm_room, map_name, scanned_by, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, title, seller, len(listings), channel, fm_room, map_name, scanned_by, source))
            scan_id = cur.lastrowid
            # this shop's scans: by its seller, else (no seller read) its title
            shop, same_shop = (seller, "c.seller = ?") if seller else (title, "c.seller IS NULL AND c.shop_title IS ?")
            # ...in the same spot: a shop put up again on another channel or room (after a disconnect, say) is
            # news, so its listings are logged again there. A scan that doesn't know its channel matches any.
            same_spot, spot = ("AND c.channel IS ? AND c.fm_room IS ? AND c.map_name IS ?",
                               (channel, fm_room, map_name)) if channel is not None else ("", ())
            skipped = 0
            for item_id, name, qty, price, confirmed, icon, *more in listings:
                sold, stock, stats = list(more) + [False, None, None][len(more):]
                sold = int(bool(sold))
                repeat = (seller or title) and self.conn.execute(
                    f"""SELECT 1 FROM sightings s JOIN scans c ON c.id = s.scan_id
                        WHERE s.at > ? AND s.ocr_name = ? AND s.qty = ? AND s.price IS ? AND s.sold = ?
                          AND {same_shop} AND c.source = ? {same_spot} LIMIT 1""",
                    (now - self.REPEAT_WINDOW, name, qty, price, sold, shop, source, *spot)).fetchone()
                if repeat:
                    skipped += 1
                    continue
                self.conn.execute(
                    """INSERT INTO sightings (scan_id, at, item_id, ocr_name, qty, price, unit_price, confirmed, sold,
                                              stock, stats)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (scan_id, now, item_id, name, qty, price,
                     price / qty if price is not None and qty else None, int(bool(confirmed)), sold,
                     stock, json.dumps(stats, separators=(",", ":")) if stats else None))
                if item_id is not None and icon:
                    self.conn.execute("INSERT OR IGNORE INTO shop_icons VALUES (?,?)", (item_id, icon))
            if full_list and not skipped:
                self.conn.execute("UPDATE scans SET full_list = 1 WHERE id = ?", (scan_id,))
            if (seller or title) and skipped == len(listings):
                last = self.conn.execute(
                    f"""SELECT listings, full_list FROM scans c WHERE id < ? AND at > ? AND {same_shop} AND source = ?
                        ORDER BY id DESC LIMIT 1""", (scan_id, now - self.REPEAT_WINDOW, shop, source)).fetchone()
                emptied = full_list and not listings and tuple(last or ()) != (0, 1)  # news: it's sold out
                if not emptied and self.conn.execute(
                        f"""SELECT 1 FROM scans c WHERE id < ? AND at > ? AND {same_shop} AND source = ?
                              AND nothing_new = 0 LIMIT 1""",
                        (scan_id, now - self.HEARTBEAT, shop, source)).fetchone():
                    self.conn.execute("UPDATE scans SET nothing_new = 1 WHERE id = ?", (scan_id,))
            self.conn.commit()
        self.on_scan(scan_id)
        return scan_id

    def ttl(self):
        """Seconds FM prices and seen shop prices stay fresh: meta 'ttl' if set (MapleDash, before 2.5, could
        change it), else TTL."""
        value = self.meta("ttl")
        return float(value) if value is not None else TTL

    def meta(self, key):
        """A value from the meta table (settings and bookkeeping), or None."""
        row = self._row("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else None

    def set_meta(self, key, value):
        """Store a meta value; None removes it."""
        if value is None:
            self._write(("DELETE FROM meta WHERE key=?", (key,)))
        else:
            self._write(("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value))))

    def _row(self, sql, args):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def _write(self, *statements):
        with self.lock:
            for sql, args in statements:
                self.conn.execute(sql, args)
            self.conn.commit()

    def _search(self, query):
        key = normalize(query)
        row = self._row("SELECT results FROM searches WHERE query=?", (key,))
        if row:
            return [tuple(r[:2]) for r in json.loads(row[0])]
        results = self.source.search(query)
        self._write(("INSERT OR REPLACE INTO searches VALUES (?,?,?)", (key, json.dumps(results), self.clock())))
        return results

    def _fetch_item(self, item_id):
        item, info = self.source.item(item_id)
        self._write(
            ("INSERT OR REPLACE INTO items VALUES (?,?,?,?)", (item.id, item.name, item.price, self.clock())),
            ("INSERT OR REPLACE INTO item_info VALUES (?,?)", (item.id, json.dumps(info))),
        )
        return item, info

    def _item(self, item_id):
        row = self._row("SELECT id, name, price FROM items WHERE id=?", (item_id,))
        return Item(*row) if row else self._fetch_item(item_id)[0]

    def item(self, item_id):
        """The item with this (MeowDB) id: from the downloaded list, else asked of MeowDB once."""
        return self._item(item_id)

    def info(self, item_id):
        """Item type, requirements, base stats and icon key. These don't change: cached forever."""
        row = self._row("SELECT data FROM item_info WHERE item_id=?", (item_id,))
        info = json.loads(row[0]) if row else None
        if info is None or "iconKey" not in info:  # also upgrades entries cached before icon keys
            info = self._fetch_item(item_id)[1]
        return info

    def market(self, item_id):
        row = self._row("SELECT data FROM market_v2 WHERE item_id=? AND fetched_at>?",
                        (item_id, self.clock() - self.ttl()))
        if row:
            return Market(**json.loads(row[0]))
        m = self.source.market(item_id)
        self._write(("INSERT OR REPLACE INTO market_v2 VALUES (?,?,?)", (item_id, json.dumps(asdict(m)), self.clock())))
        return m

    def icon_png(self, item_id):
        """The item's icon from MeowDB as PNG bytes: from the pack bundled with mprice_decoder, or
        (an item newer than the build) downloaded once and cached forever."""
        row = self._row("SELECT png FROM icons WHERE item_id=?", (item_id,))
        if row:
            return row[0]
        key = self.info(item_id)["iconKey"]
        png = self.icons.png(key)
        if png:
            return png
        png = self.source.icon_png(key)
        self._write(("INSERT OR REPLACE INTO icons VALUES (?,?)", (item_id, png)))
        return png

    def stored_icon(self, item_id):
        """The item's icon if it's on this PC (bundled, or downloaded before), else None. Never
        downloads anything: for the shop panel, which mustn't wait."""
        row = self._row("SELECT png FROM icons WHERE item_id=?", (item_id,))
        if row:
            return row[0]
        row = self._row("SELECT data FROM item_info WHERE item_id=?", (item_id,))
        info = json.loads(row[0]) if row else None
        return self.icons.png(info.get("iconKey")) if isinstance(info, dict) else None

    def icon_color(self, item_id):
        """Average icon colour (see icons.feature). Cached forever."""
        row = self._row("SELECT r, g, b FROM icon_colors WHERE item_id=?", (item_id,))
        if row:
            return None if row[0] is None else row
        color = feature(Image.open(io.BytesIO(self.icon_png(item_id))))
        self._write(("INSERT OR REPLACE INTO icon_colors VALUES (?,?,?,?)", (item_id, *(color or (None,) * 3))))
        return color

    def record_seen(self, item_id, unit_price):
        """Remember a shop listing price. Re-scanning the same price just refreshes it."""
        now = self.clock()
        self._write(("DELETE FROM seen WHERE seen_at<=?", (now - self.ttl(),)),
                    ("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (item_id, unit_price, now)))

    def seen(self, item_id):
        row = self._row("SELECT MIN(unit_price), MAX(unit_price), COUNT(*) FROM seen WHERE item_id=? AND seen_at>?",
                        (item_id, self.clock() - self.ttl()))
        return Seen(*row) if row[2] else None

    # ---- bulk downloads (feed.py) ---------------------------------------------

    def import_catalog(self, items):
        """Store MeowDB's whole item list ([{id, name, npc_price, info}]), and match names against
        it from now on. Item info already cached is kept: it came from the item's own page.
        Returns how many items were stored."""
        now = self.clock()
        rows, infos = [], []
        for i in items:
            try:
                item_id, name, price = int(i["id"]), str(i["name"]).strip(), int(i.get("npc_price") or 0)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # not an item: skip it, keep the rest
            if item_id > 0 and name:
                rows.append((item_id, name, price, now))
                if isinstance(i.get("info"), dict):
                    infos.append((item_id, json.dumps(i["info"])))
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?,?)", rows)
            self.conn.executemany("INSERT OR IGNORE INTO item_info VALUES (?,?)", infos)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (CATALOG_AT, str(now)))
            self.conn.commit()
        return len(rows)

    def import_fm(self, summaries):
        """Store FM summaries ([{item_id, usual, reports, sold, for_sale, cheapest_sell, best_buy,
        fetched_at}]). One older than the copy we have is skipped. Returns how many were read."""
        now = self.clock()
        rows = []
        for f in summaries:
            try:
                m = Market(f.get("usual"), int(f.get("reports") or 0), int(f.get("sold") or 0),
                           int(f.get("for_sale") or 0), f.get("cheapest_sell"), f.get("best_buy"))
                # Never later than now: a server clock ahead of ours mustn't keep a price fresh for longer.
                rows.append((int(f["item_id"]), json.dumps(asdict(m)), min(float(f["fetched_at"]), now)))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        with self.lock:
            self.conn.executemany(
                """INSERT INTO market_v2 VALUES (?,?,?) ON CONFLICT (item_id) DO UPDATE
                   SET data = excluded.data, fetched_at = excluded.fetched_at
                   WHERE excluded.fetched_at > market_v2.fetched_at""", rows)
            self.conn.commit()
        return len(rows)

    def _catalog(self):
        """(ids, names, normalized names) of every item, once the whole list has been downloaded;
        else None. Rebuilt after each download (and gone after the database is cleared)."""
        with self.lock:
            stamp = self.meta(CATALOG_AT)
            if stamp is None:
                return None
            if self._names is None or self._names[0] != stamp:
                rows = self.conn.execute("SELECT id, name FROM items ORDER BY id").fetchall()
                self._names = (stamp, [r[0] for r in rows], [r[1] for r in rows], [normalize(r[1]) for r in rows])
            return self._names[1:]

    def _local_candidates(self, q):
        """[(id, name)] from the downloaded list whose names could be the normalized OCR text `q`:
        every name lookup() would score as a match, found by scoring them all at once. None if
        the list hasn't been downloaded."""
        catalog = self._catalog()
        if catalog is None:
            return None
        ids, names, norm = catalog
        hits = {i for _, _, i in process.extract(q, norm, scorer=fuzz.ratio, processor=None,
                                                 score_cutoff=MIN_SCORE, limit=None)}
        if len(q) >= MIN_PREFIX:  # names the OCR text is the start of (cut off by the shop)
            longer = [i for i, n in enumerate(norm) if len(n) > len(q)]
            heads = [norm[i][: len(q)] for i in longer]
            hits.update(longer[j] for _, _, j in process.extract(q, heads, scorer=fuzz.ratio, processor=None,
                                                                 score_cutoff=MIN_SCORE, limit=None))
        return [(ids[i], names[i]) for i in sorted(hits)]

    def lookup(self, ocr_name):
        base, truncated = strip_truncation(strip_upgrades(ocr_name.strip()))  # any OCR'd name: tooltip or shop row
        match = Match(ocr_name)
        if len(base) < 3:
            return match

        q = normalize(base)
        candidates = self._local_candidates(q)
        if not candidates:  # no list downloaded yet, or an item newer than it: ask MeowDB
            candidates = self._search(base)
        if not candidates:
            # OCR typo can break the server-side search; retry on the first words.
            words = base.split()
            if len(words) > 2:
                candidates = self._search(" ".join(words[:2]))

        exact, scored = [], []
        for item_id, name in candidates:
            n = normalize(name)
            full = fuzz.ratio(q, n)
            # The shop cuts long names off, and OCR sometimes drops the "...",
            # so a name the OCR text is a prefix of counts as a match too.
            prefix = fuzz.ratio(q, n[: len(q)]) if len(q) >= MIN_PREFIX and len(n) > len(q) else 0
            if not truncated and full >= EXACT_SCORE:
                exact.append(item_id)
            score = max(full, prefix)
            if score >= MIN_SCORE:
                scored.append((score, item_id))
        if not exact and scored:
            best = max(s for s, _ in scored)
            scored = [i for s, i in scored if s >= best - TIE_MARGIN]

        match.items = list(self.mapper(self._item, exact or scored))
        match.items.sort(key=lambda i: i.name)
        return match
