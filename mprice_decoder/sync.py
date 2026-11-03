"""Shares decoder-read shop visits with Cozy Markets (cozy.markets), the guild's Free Market
board. On unless it's switched off in F10, and scans logged while it's off are
never sent, even if it's switched back on later.

A background thread sends batches of shop visits, as big as how fast you're scanning
makes them (Sync.batch_size): about one push every PUSH_EVERY while you browse, so a heavy
scanner's go in big batches and a light one's in small ones, and no visit waits more than
MAX_WAIT for its batch to fill. Every visit is saved in the local database first, so what's
waiting survives a restart.
Failed batches are retried with backoff. The server ignores scans it already has,
so retrying a batch after an interrupted upload does no harm.

Who you are there: shared scans go under the name you pick in F10 (Settings.share_name),
anonymous unless you pick one. Anonymous scans count on the board just the same, with no
name on the leaderboard. The character name read off the status bar stays on this PC. Pushes are signed with a token mprice_decoder registers for itself
(a guild token put in settings.json by hand is used instead, but isn't needed).
Official builds sign that registration with the client key built into them, so the
server hands tokens to mprice_decoder only, not to scripts. A new token's scans wait on the
server until they look honest (its prices near the FM price) before they go on the board.
"""
import base64
import collections
import hashlib
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

from .version import VERSION
from .distribution import private_access
from .identity import APP_ID

log = logging.getLogger("mprice_decoder")

CLIENT = f"{APP_ID} {VERSION}"
BATCH = 25            # shop visits a push holds at most by default (payload); the size in use follows the scanning
MIN_BATCH, MAX_BATCH = 1, 100  # shop visits per push, each with its full list of item sightings: the range it follows in
PUSH_EVERY = 30       # s: about how often a push goes while scanning (the server takes 60 in 10 minutes per token,
                      # one every 10 s: this keeps to about 2 a minute however fast the scanning)
MAX_WAIT = 60         # s the oldest waiting visit waits for its batch to fill: then what's waiting goes as it is
RATE_WINDOW = 120     # s of recent visits the scanning rate is taken over (short: it follows a speed-up or a stop fast)
MIN_SPAN = 30         # s: the least time the rate's taken over, so a quick start isn't taken for a flood
INTERVAL = 300        # s between looks when nothing happens
STARTUP_DELAY = 30    # s before the first push: time to switch sharing off in F10 first
RETRY = (30, 1800)    # s to wait after a failed push: at first, and at most
TIMEOUT = 30
REJECTED = "Cozy Markets didn't accept the cozy_token in settings.json. Fix it, or remove it."
PRIVATE_REJECTED = "This private build's upload access was revoked. Shop visits stay saved on this PC."
PRIVATE_MISSING = "This private build has no usable upload credential. Shop visits stay saved on this PC."

# Bookkeeping in the database's meta table. The last two are per server (the key ends
# with its URL), so trying a test server doesn't change what cozy.markets has been sent.
# Clearing the database removes the first two (scan ids start again at 1, so
# this is a new install as far as a server knows) but keeps the tokens.
INSTALL_ID = "cozy_install_id"       # names this database to the server
PUSHED = "cozy_pushed_scan"          # id of the last scan the server took (or skipped)
PUBLIC_TOKEN = "cozy_public_token"   # the token registered for anonymous sharing
# "<account id> <server> <token's hash>": the Discord account the anonymous token was last tied to (link_owner), so
# the shops it shares show in that account's My scans on the website
OWNER = "cozy_owner_link"


# The client key official builds sign their token registrations with: release.sh puts it in the
# build (never in the source), as the base64 of its 32 private bytes. Without it (running from
# source, or a home-made build) registrations go unsigned, which only a dev server takes.
CLIENT_KEY = Path(__file__).resolve().parent / "assets" / "client.key"
UNOFFICIAL = "This copy can't share scans: download the official app from cozy.markets."
READ_RETRY = 600      # s before trying again to get a token to read the board with, after it couldn't be had
ROOM_STALE = 600      # s: an FM room's shops (room) older than this aren't sent: they may have moved since


def read(session, url, reader, **kwargs):
    """GET `url` from Cozy Markets with `reader`'s token (Sync.read_token) when there's one: the board's data asks for
    a token, or the pass a browser gets. A token the server turns down is dropped (Sync.bad_read_token) and the request
    made again without one, which the server still takes from MaplePrice for what it reads, for now."""
    token = reader.read_token(url) if reader is not None else None
    if token:
        headers = {**(kwargs.get("headers") or {}), "Authorization": f"Bearer {token}"}
        r = session.get(url, **{**kwargs, "headers": headers})
        if r.status_code != 401:
            return r
        reader.bad_read_token(token)
    return session.get(url, **kwargs)


def client_key(path=CLIENT_KEY):
    """(key id, private key) from the bundled client key, or None if this build has none."""
    try:
        seed = base64.b64decode(path.read_text().strip(), validate=True)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.from_private_bytes(seed)
    except (OSError, ValueError):
        return None
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(public).hexdigest()[:8], key  # the id the server knows the key by


def registration(install, key=None, now=time.time):
    """A token registration's body (bytes, exactly as signed) and headers, signed with `key`
    (key id, private key) when there is one. ts and nonce make each one good once, briefly."""
    body = json.dumps({"client": CLIENT, "install": install, "ts": now(), "nonce": secrets.token_urlsafe(18)}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        key_id, private = key
        headers["X-Cozy-Key"] = key_id
        headers["X-Cozy-Signature"] = base64.b64encode(private.sign(body)).decode()
    return body, headers


class Unofficial(Exception):
    """The server only gives tokens to official builds, and this one has no client key (403)."""


class Rejected(Exception):
    """The server won't take this token (unknown, revoked, or not allowed to push)."""


class Outdated(Exception):
    """The server refuses even an empty push: this mprice_decoder is probably too old for it."""


class Busy(Exception):
    """The server asked us to slow down (429); `wait` is how long, in seconds."""

    def __init__(self, wait):
        super().__init__(f"server busy, retry in {wait}s")
        self.wait = wait


def server(url):
    """A server's URL as mprice_decoder connects to it and files what it has sent there under:
    no trailing slash, and "localhost" as 127.0.0.1. Windows tries localhost over IPv6 first,
    and a test server that only listens on IPv4 (one in WSL) made every new connection wait
    ~21s for that to time out."""
    clean = str(url).strip().rstrip("/")
    return clean.replace("//localhost", "//127.0.0.1", 1) if urlparse(clean).hostname == "localhost" else clean


def base_url(url):
    """The server's URL (as `server` writes it), if a token may be sent there: HTTPS, or plain HTTP to this PC."""
    u = urlparse(str(url).strip())
    if (u.scheme == "https" and u.hostname) or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")):
        return server(url)
    raise ValueError(f"won't send a token to {url!r}: it has to be https://")


def install_id(db):
    """This database's id on the server, made the first time it's needed."""
    with db.lock:
        value = db.meta(INSTALL_ID)
        if not value:
            value = str(uuid.uuid4())
            db.set_meta(INSTALL_ID, value)
        return value


def pushed(db, url):
    return int(db.meta(f"{PUSHED} {server(url)}") or 0)


def mark_pushed(db, install, scan_id, url):
    """Remember the server has everything up to `scan_id`. Only ever forward: a push that
    finishes after sharing was switched off mustn't move the mark back over scans it skipped.
    And not at all if the database was cleared since the push started (new install id): its
    scans with those ids are new ones."""
    key = f"{PUSHED} {server(url)}"
    with db.lock:
        db.conn.execute("""INSERT OR REPLACE INTO meta SELECT ?, ?
                           WHERE EXISTS (SELECT 1 FROM meta WHERE key = ? AND value = ?)
                             AND COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key = ?), 0) < ?""",
                        (key, str(scan_id), INSTALL_ID, install, key, scan_id))
        db.conn.commit()


def pending(db, url):
    """Shop visits logged since the last push that are shared: decoded ones, and OCR's backups of shops the decoder
    didn't have (data.SHARED). Older OCR history stays local."""
    with db.lock:
        return db.conn.execute("""SELECT COUNT(*) FROM scans
                                  WHERE source IN ('decoder', 'backup') AND nothing_new = 0 AND id > ?""",
                               (pushed(db, url),)).fetchone()[0]


def payload(db, install, after, limit=BATCH, name=None):
    """The next push: up to `limit` decoder visits after scan `after`, and the MeowDB data
    behind them (item names and NPC prices, FM summaries, icons), each scan under `name`
    (None: anonymous). Returns (body, id of its last scan), or (None, None) when there's nothing new."""
    with db.lock:
        cur = db.conn.cursor()
        cur.row_factory = sqlite3.Row
        scans = cur.execute("""SELECT id, at, shop_title, seller, channel, fm_room, map_name, scanned_by, full_list, source
                               FROM scans WHERE source IN ('decoder', 'backup') AND nothing_new = 0 AND id > ?
                               ORDER BY id LIMIT ?""",
                            (after, limit)).fetchall()
        if not scans:
            return None, None
        ids = [s["id"] for s in scans]
        marks = ",".join("?" * len(ids))
        rows = cur.execute(f"""SELECT scan_id, at, item_id, ocr_name, qty, price, confirmed, sold, stock, stats
                               FROM sightings WHERE scan_id IN ({marks}) ORDER BY id""", ids).fetchall()
        item_ids = sorted({r["item_id"] for r in rows if r["item_id"] is not None})
        marks = ",".join("?" * len(item_ids))
        info = {r["item_id"]: json.loads(r["data"]) for r in cur.execute(
            f"SELECT item_id, data FROM item_info WHERE item_id IN ({marks})", item_ids)}
        items = [{"id": r["id"], "name": r["name"], "npc_price": r["price"], "info": info.get(r["id"])}
                 for r in cur.execute(f"SELECT id, name, price FROM items WHERE id IN ({marks})", item_ids)]
        fm = []
        for r in cur.execute(f"SELECT item_id, data, fetched_at FROM market_v2 WHERE item_id IN ({marks})", item_ids):
            d = json.loads(r["data"])
            fm.append({"item_id": r["item_id"], "usual": d.get("usual"), "reports": d.get("reports"),
                       "sold": d.get("sold"), "for_sale": d.get("for_sale"), "cheapest_sell": d.get("cheapest_sell"),
                       "best_buy": d.get("best_buy"), "fetched_at": r["fetched_at"]})
        # Canonical item icons only; screenshot crops remain in the local shop_icons table.
        icons = [{"item_id": r["item_id"], "source": "meowdb", "png": base64.b64encode(r["png"]).decode()}
                 for r in cur.execute(f"SELECT item_id, png FROM icons WHERE item_id IN ({marks})", item_ids)]
    by_scan = {}
    for r in rows:
        by_scan.setdefault(r["scan_id"], []).append(
            {"at": r["at"], "item_id": r["item_id"], "ocr_name": r["ocr_name"], "qty": r["qty"], "price": r["price"],
             "confirmed": bool(r["confirmed"]), "sold": bool(r["sold"]),
             # read from the game's packets only (packets.py): how many it had left, an equipment item's stats
             **({"stock": r["stock"]} if r["stock"] is not None else {}),
             **({"stats": json.loads(r["stats"])} if r["stats"] else {})})
    body = {"install_id": install, "client": CLIENT, "items": items, "fm": fm, "icons": icons,
            "scans": [{"client_id": s["id"], "at": s["at"], "shop_title": s["shop_title"], "seller": s["seller"],
                       "channel": s["channel"], "fm_room": s["fm_room"], "map_name": s["map_name"],
                       "scanned_by": name or None, "sightings": by_scan.get(s["id"], []),
                       # the decoder read the seller's name from the shop's packets: spelt right (OCR's backups aren't)
                       "seller_exact": s["source"] == "decoder",
                       **({"full_list": True} if s["full_list"] else {})} for s in scans]}
    return body, ids[-1]


def minutes(seconds):
    m = max(1, round(seconds / 60))
    return f"{m} minute{'s' if m != 1 else ''}"


class Sync:
    """The background sharer. `run()` is the thread; `notify()` wakes it (after a scan or a
    settings change); `status()` is the line the F10 window shows."""

    def __init__(self, db, settings, session=None, clock=time.monotonic, startup_delay=STARTUP_DELAY, key=None,
                 batch_size=None):
        self.db, self.settings = db, settings
        self.private = private_access()
        self.fixed = None        # a batch size set by hand (the tests): else it follows the scanning
        self.visits = collections.deque()  # when each recent visit was logged (clock), for the scanning rate
        self.waiting_since = None  # when what's waiting started to wait (clock)
        self.started = clock()
        if batch_size is not None:
            self.batch_size = batch_size
        self.key = None if self.private is not None else key if key is not None else client_key()
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = CLIENT
        self.clock = clock
        self.not_before = clock() + startup_delay
        self.wake = threading.Event()
        self.retry = 0           # s we last waited after a failure (doubles each time)
        self.problem = None      # why the last round failed, for F10
        self.rejected = None     # the guild token the server refused: not tried again until it changes
        self.role = None         # "member" or "public", once a push has gone through
        self.contributor = None  # the member's name, for a member token: credited when a scan has none
        self.owner = None        # auth.AuthService while signed in with Discord: link_owner after each push
        self.registering = threading.RLock()  # one public_token registration at a time: the reads' threads ask too
        self.read_refused = set()  # tokens the server turned down for reading: not sent again
        self.read_after = 0.0    # clock: no registering for reading before this, after a failed try
        self.rooms = collections.OrderedDict()  # (channel, room) -> its shops waiting to go (room): the latest of each
        self.rooms_lock = threading.Lock()

    @property
    def batch_size(self):
        """Shop visits per push: enough for about one push every PUSH_EVERY at the rate visits came in over the last
        RATE_WINDOW (at least MIN_SPAN's worth of time), between MIN_BATCH and MAX_BATCH."""
        if self.fixed is not None:
            return self.fixed
        now = self.clock()
        while self.visits and now - self.visits[0] > RATE_WINDOW:
            self.visits.popleft()
        span = max(float(MIN_SPAN), min(RATE_WINDOW, now - self.started))
        return max(MIN_BATCH, min(MAX_BATCH, math.ceil(len(self.visits) / span * PUSH_EVERY)))

    @batch_size.setter
    def batch_size(self, size):
        if not isinstance(size, int) or size < 1:
            raise ValueError("batch_size must be a positive number of shop visits")
        self.fixed = size

    def due(self, waiting):
        """Whether to push now: a batch's worth is waiting, or what's waiting has waited MAX_WAIT."""
        if not waiting:
            self.waiting_since = None
            return False
        if self.waiting_since is None:
            self.waiting_since = self.clock()
        return waiting >= self.batch_size or self.clock() - self.waiting_since >= MAX_WAIT

    @property
    def url(self):
        return self.private.url if self.private is not None else self.settings.cozy_url

    def notify(self, scan_id=None):
        """After a scan (`scan_id`) or a settings change. A scan logged while sharing is off
        is marked handled at once, so switching sharing on a moment later can't send it."""
        try:
            if not self.settings.share_scans:
                self.skip_pending()
            elif scan_id is not None:
                now = self.clock()
                self.batch_size  # drops visits older than RATE_WINDOW
                if not self.visits:  # scanning (again) after a quiet spell: its rate is taken from now, not from
                    self.started = now  # MaplePrice's start (else a fast start reads as slow: tiny pushes, 12 a minute)
                self.visits.append(now)
                waiting = pending(self.db, self.url)
                if waiting >= self.batch_size:
                    log.info("shop batch ready: %d visits queued (%d per upload)", waiting, self.batch_size)
                else:
                    log.info("shop batch: %d/%d visits saved locally", waiting, self.batch_size)
        except Exception:
            log.exception("couldn't update the shop upload queue after scan %s", scan_id)
        self.wake.set()

    def room(self, room):
        """An FM room's shops and where each stands (PacketShops.on_room), sent with the next round while sharing."""
        if not self.settings.share_scans:
            return
        with self.rooms_lock:
            self.rooms.pop((room["channel"], room["room"]), None)
            self.rooms[(room["channel"], room["room"])] = room
        self.wake.set()

    def push_rooms(self, url, token):
        """Send the rooms waiting (room), oldest first: one the server won't take (or a server without them) is
        dropped; a failure (raised) leaves the rest for the next round."""
        while self.settings.share_scans:
            with self.rooms_lock:
                if not self.rooms:
                    return
                key, room = next(iter(self.rooms.items()))
            if time.time() - room["at"] <= ROOM_STALE:
                body = {"install_id": install_id(self.db), "client": CLIENT, **room}
                r = self.session.post(f"{url}/api/room-shops", json=body, timeout=TIMEOUT,
                                      headers={"Authorization": f"Bearer {token}"})
                if r.status_code in (400, 404, 413):
                    log.warning("cozy markets didn't take FM %s ch %s's shops (%s): %s", room["room"], room["channel"],
                                r.status_code, r.text[:200])
                else:
                    self.check(r)
                    log.info("sent FM %s ch %s's shops: %d placed", room["room"], room["channel"], len(room["shops"]))
            with self.rooms_lock:
                if self.rooms.get(key) is room:  # (not if a newer one came in meanwhile)
                    del self.rooms[key]

    def run(self):
        wait = 0
        while True:
            if self.wake.wait(wait):
                self.wake.clear()
            wait = self.step()

    def step(self):
        """One round: share what's new if sharing is on. Returns seconds to wait before the next."""
        if not self.settings.share_scans:
            self.skip_pending()
            self.problem = None
            return INTERVAL
        if self.clock() < self.not_before:
            return self.not_before - self.clock()
        if self.private is not None and not self.private.token:
            self.problem = PRIVATE_MISSING
            return INTERVAL
        entered = self.private.token if self.private is not None else (self.settings.cozy_token or "").strip()
        if entered and entered == self.rejected:  # until it's changed in F10
            self.problem = PRIVATE_REJECTED if self.private is not None else REJECTED
            return INTERVAL
        try:
            url = base_url(self.url)
            if self.rooms:
                self.push_rooms(url, entered or self.public_token(url))
            if not self.due(pending(self.db, url)):
                self.problem = None
                if self.waiting_since is None:
                    return INTERVAL
                return max(1.0, min(INTERVAL, self.waiting_since + MAX_WAIT - self.clock()))
            token = entered or self.public_token(url)
            sent = self.push_pending(url, token)
        except ValueError as e:
            self.problem = f"Not sharing: {e}"
            log.warning("cozy markets: %s", e)
            return INTERVAL
        except Rejected:
            if self.private is not None:
                self.rejected = entered
                self.problem = PRIVATE_REJECTED
                log.warning("private build upload access (%s) was refused; uploads stopped, visits remain saved locally",
                            self.private.label)
                return INTERVAL
            elif entered:
                self.rejected = entered
                self.problem = REJECTED
                log.warning("cozy markets refused the guild token")
            else:  # revoked, or the server lost it: get a new one next time
                self.db.set_meta(f"{PUBLIC_TOKEN} {server(self.url)}", None)
                self.problem = "Cozy Markets didn't accept this PC's token: getting a new one."
                log.warning("cozy markets refused the public token")
            return self.backoff()
        except Unofficial:
            self.problem = UNOFFICIAL
            log.warning("cozy markets only gives tokens to official builds, and this one has no client key")
            return self.backoff(RETRY[1])
        except Busy as e:
            wait = self.backoff(e.wait)
            self.problem = f"Cozy Markets is busy: trying again in {minutes(wait)}."
            log.warning("cozy markets is busy: retrying in %s", minutes(wait))
            return wait
        except Outdated as e:
            self.problem = "Cozy Markets won't take scans from this version: it may need an update."
            log.warning("cozy markets refused an empty push: %s", e)
            return self.backoff()
        except Exception as e:
            self.problem = f"Can't reach {urlparse(self.url).hostname or 'Cozy Markets'} " \
                           f"right now: trying again in {minutes(self.next_retry())}."
            log.warning("cozy markets push failed: %s", e)
            return self.backoff()
        self.problem, self.retry = None, 0
        if sent:
            log.info("shared %d scans with cozy markets", sent)
            if self.owner is not None:  # a token registered since signing in is tied to the account too
                self.link_owner(self.owner)
        return INTERVAL

    def _owned(self, service):
        """(anonymous token, its OWNER key) to tie to `service`'s Discord account, or (None, None): only the
        anonymous token MaplePrice registered itself, never a guild token or a private build's."""
        session = getattr(service, "session", None)
        if session is None or self.private is not None or (self.settings.cozy_token or "").strip():
            return None, None
        try:
            url = base_url(self.url)
        except ValueError:
            return None, None
        token = self.db.meta(f"{PUBLIC_TOKEN} {server(url)}")
        user = (session.user or {}).get("id")
        if not token or user is None:
            return None, None
        return token, f"{user} {server(url)} {hashlib.sha256(token.encode()).hexdigest()[:16]}"

    def link_owner(self, service):
        """Tie this PC's anonymous token to the Discord account it's signed in with (`service`, auth.AuthService),
        so every shop it has shared, from before too, shows in that account's My scans on the website, for that
        account only. Once per token and account (OWNER); False when it couldn't (signed out, nothing registered
        yet, a guild token, the server said no): asked again after the next push."""
        from .auth import AuthError
        token, key = self._owned(service)
        if token is None:
            return False
        if self.db.meta(OWNER) == key:
            return True
        try:
            service.api("POST", "/link", {"token": token})
        except AuthError as e:
            log.warning("couldn't link this PC's shared shops to the Discord account: %s", e)
            return False
        self.db.set_meta(OWNER, key)
        log.info("this PC's shared shops are linked to the Discord account (My scans on the website)")
        return True

    def unlink_owner(self, service):
        """Signing out: this PC's shops stop showing in that account's My scans (before the sign-in is dropped)."""
        from .auth import AuthError
        token, _ = self._owned(service)
        try:
            if token is not None:
                service.api("POST", "/unlink", {"token": token})
                log.info("this PC's shared shops are no longer linked to the Discord account")
        except AuthError as e:
            log.warning("couldn't unlink this PC's shared shops from the Discord account: %s", e)
        finally:
            self.db.set_meta(OWNER, None)

    def read_token(self, url):
        """The token to read the board at `url` with, from any thread: the one scans are shared with (a guild token,
        a private build's, else this PC's public one, registered now if there's none yet, even with sharing off: it
        names nothing but this install). None for another server than the one shared with, or when there's none to be
        had (a build without the client key, the server unreachable: tried again after READ_RETRY)."""
        try:
            if server(url).rsplit("/api/", 1)[0] != base_url(self.url):
                return None
            entered = self.private.token if self.private is not None else (self.settings.cozy_token or "").strip()
            if entered:
                return None if entered in self.read_refused or entered == self.rejected else entered
            token = self.db.meta(f"{PUBLIC_TOKEN} {server(self.url)}")
            if token or self.private is not None or self.clock() < self.read_after:
                return token if token not in self.read_refused else None
            return self.public_token(base_url(self.url))
        except (Unofficial, Rejected, ValueError):
            self.read_after = self.clock() + READ_RETRY
        except Exception as e:
            log.warning("couldn't get a token to read cozy markets with (%s): reading without one", e)
            self.read_after = self.clock() + READ_RETRY
        return None

    def bad_read_token(self, token):
        """The server turned `token` down for reading: a public one is forgotten (the next read or push registers a new
        one), any other just isn't sent for reading again."""
        key = f"{PUBLIC_TOKEN} {server(self.url)}"
        if token == self.db.meta(key):
            self.db.set_meta(key, None)
            log.warning("cozy markets refused the public token for reading: getting a new one")
        else:
            self.read_refused.add(token)

    def next_retry(self):
        return min(self.retry * 2, RETRY[1]) if self.retry else RETRY[0]

    def backoff(self, at_least=0):
        self.retry = max(self.next_retry(), at_least)
        self.not_before = self.clock() + self.retry
        return self.retry

    def skip_pending(self):
        """Sharing is off: count everything logged so far as handled, so it's never sent (rooms waiting too)."""
        with self.rooms_lock:
            self.rooms.clear()
        url = self.url
        with self.db.lock:
            last = self.db.conn.execute("SELECT MAX(id) FROM scans").fetchone()[0]
            if last and last > pushed(self.db, url):
                mark_pushed(self.db, install_id(self.db), last, url)

    def public_token(self, url):
        if self.private is not None:
            raise Rejected("private builds cannot register replacement upload tokens")
        with self.registering:
            return self._public_token(url)

    def _public_token(self, url):
        key = f"{PUBLIC_TOKEN} {server(url)}"
        token = self.db.meta(key)
        if token:
            return token
        body, headers = registration(install_id(self.db), self.key)
        r = self.session.post(f"{url}/api/tokens/public", data=body, headers=headers, timeout=TIMEOUT)
        if r.status_code == 403:  # unsigned, or signed with a key the server doesn't take (any more)
            raise Unofficial()
        self.check(r)
        token = r.json()["token"]
        self.db.set_meta(key, token)
        log.info("registered for anonymous sharing with cozy markets")
        return token

    def check(self, r):
        if r.status_code in (401, 403):
            raise Rejected()
        if r.status_code in (429, 503):  # slow down, or the server's queue is full
            raise Busy(int(r.headers.get("Retry-After") or RETRY[0]))
        r.raise_for_status()

    def push_pending(self, url, token):
        """Send batches while one's due (due), leaving the rest on disk to wait from now. Returns scans accepted.

        Commit the watermark only after an entire batch is handled, including any
        smaller requests needed to isolate an invalid scan or fit the server's cap.
        If a request fails partway through, the whole batch remains queued for retry.
        """
        sent = 0
        while self.settings.share_scans:
            install = install_id(self.db)
            waiting = pending(self.db, url)
            if not self.due(waiting):
                break
            size = min(waiting, self.batch_size)
            body, last = payload(self.db, install, pushed(self.db, url), size, self.settings.share_name or None)
            if body is None:
                break
            accepted = self.push_batch(url, token, body)
            if accepted is None:  # sharing was switched off during this batch
                break
            mark_pushed(self.db, install, last, url)
            sent += accepted
            left = pending(self.db, url)
            self.waiting_since = self.clock() if left else None  # what's left waits from now
            log.info("shop batch uploaded: %d/%d visits accepted; %d waiting locally", accepted, size, left)
        return sent

    def push_batch(self, url, token, body):
        """Send an admitted batch, splitting only if the server rejects its size/data.

        None means sharing was switched off; otherwise return the accepted count.
        The caller owns the durable watermark for the entire batch.
        """
        if not self.settings.share_scans:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        r = self.session.post(f"{url}/api/ingest", json=body, timeout=TIMEOUT, headers=headers)
        if r.status_code in (400, 413):
            scans = body["scans"]
            if len(scans) > 1:
                middle = len(scans) // 2
                accepted = 0
                for part in (scans[:middle], scans[middle:]):
                    item_ids = {row["item_id"] for scan in part for row in scan["sightings"]
                                if row["item_id"] is not None}
                    smaller = {**body, "scans": part,
                               "items": [item for item in body["items"] if item["id"] in item_ids],
                               "fm": [item for item in body["fm"] if item["item_id"] in item_ids],
                               "icons": [item for item in body["icons"] if item["item_id"] in item_ids]}
                    result = self.push_batch(url, token, smaller)
                    if result is None:
                        return None
                    accepted += result
                return accepted
            # Skip a permanently invalid scan only if an empty push is accepted.
            if not self.settings.share_scans:
                return None
            probe = self.session.post(f"{url}/api/ingest",
                                      json={"install_id": body["install_id"], "client": CLIENT},
                                      timeout=TIMEOUT, headers=headers)
            if probe.status_code in (400, 413):
                raise Outdated(f"HTTP {probe.status_code} {probe.text[:300]}")
            self.check(probe)
            log.warning("cozy markets refused scan %s (%s): skipped. %s",
                        scans[0]["client_id"], r.status_code, r.text[:300])
            return 0
        self.check(r)
        result = r.json()
        self.role, self.contributor = result.get("role"), result.get("contributor")
        return len(body["scans"])

    def status(self):
        """One line for the F10 window."""
        if not self.settings.share_scans:
            return "Off: your scans stay on this PC."
        if self.problem:
            return self.problem
        try:
            waiting = pending(self.db, self.url)
        except Exception:
            waiting = 0
        name = self.settings.share_name or (self.contributor if self.role == "member" else None)
        who = f" as {name}" if name else " anonymously" if self.role else ""
        size = self.batch_size
        if waiting:
            if waiting < size:
                return (f"{waiting}/{size} shops saved locally{who}; uploads at {size}"
                        f"{'' if self.fixed else f' or within {minutes(MAX_WAIT)}'}.")
            return f"{waiting} shops waiting to be shared{who} ({size} per batch)."
        return f"Sharing{who}: all caught up." if self.role else f"On: uploads every {size} shops."
