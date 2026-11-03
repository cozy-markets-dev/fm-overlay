"""Sharing scans with Cozy Markets, against a fake server."""
import base64
import json as jsonlib
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mprice_decoder import sync
from mprice_decoder.distribution import PrivateAccess
from mprice_decoder.settings import Settings
from mprice_decoder.sync import Sync

from test_shop import make_db

URL = Settings().cozy_url


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body or {}, headers or {}
        self.text = str(self.body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeServer:
    """Just enough of Cozy Markets: public registration, and ingest that remembers pushes."""

    def __init__(self):
        self.headers = {}
        self.tokens = {"guild-token": ("member", "Alice")}
        self.pushes = []        # (token, body)
        self.registered = 0
        self.down = False       # connection errors
        self.status = None      # answer every push with this status instead
        self.bad_scans = set()  # client_ids a push is refused (400) for
        self.client_keys = None  # {key id: public key}: registrations must be signed by one (403 if not)
        self.registrations = []  # (body, headers) of each registration
        self.rooms = []          # (token, body) of each FM room's shops sent
        self.room_status = None  # answer every room with this status instead

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        if self.down:
            raise ConnectionError("no route to host")
        if url.endswith("/api/tokens/public"):
            self.registrations.append((data, headers))
            if self.client_keys is not None:
                key = self.client_keys.get((headers or {}).get("X-Cozy-Key"))
                try:
                    key.verify(base64.b64decode(headers["X-Cozy-Signature"]), data)
                except Exception:
                    return Response(403, {"error": "only mprice_decoder from cozy.markets can share scans"})
            self.registered += 1
            token = f"public-{self.registered}"
            self.tokens[token] = ("public", None)
            return Response(201, {"token": token, "role": "public"})
        if url.endswith("/api/room-shops"):
            if self.room_status:
                return Response(self.room_status)
            self.rooms.append((headers["Authorization"].removeprefix("Bearer "), json))
            return Response(202, {"placed": len(json["shops"])})
        assert url.endswith("/api/ingest")
        if self.status:
            return Response(self.status, headers={"Retry-After": "120"})
        token = headers["Authorization"].removeprefix("Bearer ")
        if token not in self.tokens:
            return Response(401, {"error": "bad token"})
        if any(s["client_id"] in self.bad_scans for s in json.get("scans", [])):
            return Response(400, {"error": "bad scan"})
        self.pushes.append((token, json))
        role, contributor = self.tokens[token]
        return Response(202, {"queued": True, "role": role, "contributor": contributor})

    def scan_ids(self):
        return [s["client_id"] for _, body in self.pushes for s in body.get("scans", [])]


def log_scans(db, n, title="shop", scanned_by=None, source="decoder"):
    """n new visits of a shop: each a new price, so none is a repeat with nothing new (not shared)."""
    before = db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    return [db.record_scan(title, "Bob", [(355, "Pig's Head", 1, 500 + before + i, True, b"png", i == 1),
                                          (None, "Mystery", 2, 10, False, None)], channel=5, fm_room=2,
                           scanned_by=scanned_by, source=source)
            for i in range(n)]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def setup(tmp_path):
    db = make_db(tmp_path)
    db.lookup("Pig's Head")  # caches the item, its info and icon like a real scan does
    db.info(355)
    db.icon_png(355)
    db.market(355)
    settings, server = Settings(), FakeServer()
    # Small batches keep the authentication/error tests focused; threshold tests use the default.
    return db, settings, server, Sync(db, settings, session=server, startup_delay=0, key=False,
                                     clock=Clock(), batch_size=1)


def test_only_decoded_sellers_are_sent_as_exact(setup):
    db, settings, server, s = setup
    log_scans(db, 1)
    log_scans(db, 1, source="backup")   # OCR of a shop the decoder didn't have: its seller may be a letter off
    s.batch_size = 2
    s.step()
    assert [sc["seller_exact"] for _, body in server.pushes for sc in body["scans"]] == [True, False]


def test_new_scans_are_shared_in_batches(setup):
    db, settings, server, s = setup
    s.batch_size = 2
    log_scans(db, 5)
    assert sync.pending(db, URL) == 5
    assert s.step() == sync.INTERVAL
    assert server.scan_ids() == [1, 2, 3, 4] and len(server.pushes) == 2
    token, body = server.pushes[0]
    assert token == "public-1" and server.registered == 1   # no guild token: anonymous
    assert body["install_id"] == db.meta(sync.INSTALL_ID) and body["client"] == sync.CLIENT
    first = body["scans"][0]
    assert (first["seller"], first["channel"], first["fm_room"], first["scanned_by"]) == ("Bob", 5, 2, None)
    assert first["seller_exact"] is True
    assert [(r["item_id"], r["qty"], r["price"], r["confirmed"], r["sold"]) for r in first["sightings"]] == [
        (355, 1, 500, True, False), (None, 2, 10, False, False)]
    assert body["scans"][1]["sightings"][0]["sold"] is True
    assert [i["name"] for i in body["items"]] == ["Pig's Head"] and body["items"][0]["info"]["type"] == "Use"
    assert body["fm"][0]["usual"] == 21500
    assert [i["source"] for i in body["icons"]] == ["meowdb"]
    assert sync.pending(db, URL) == 1 and "1/2 shops saved locally anonymously" in s.status()

    s.step()  # nothing new: no push
    assert len(server.pushes) == 2
    log_scans(db, 1)
    s.step()
    assert server.scan_ids() == [1, 2, 3, 4, 5, 6] and server.registered == 1   # same token again
    assert len(server.pushes) == 3 and sync.pending(db, URL) == 0


def test_queued_ocr_scans_never_register_or_upload(setup):
    db, settings, server, s = setup
    log_scans(db, 30, source="ocr")
    assert db.conn.execute("SELECT COUNT(*) FROM scans").fetchone() == (30,)
    assert sync.pending(db, URL) == 0
    assert sync.payload(db, "pc", 0) == (None, None)
    s.step()
    assert server.registered == 0 and server.pushes == []


def test_batches_count_only_decoder_visits_among_local_ocr_history(setup):
    db, settings, server, s = setup
    s.batch_size = 2
    log_scans(db, 2, source="ocr")
    log_scans(db, 1, title="decoder one")
    log_scans(db, 2, title="local OCR", source="ocr")
    s.step()
    assert sync.pending(db, URL) == 1 and server.pushes == []
    log_scans(db, 1, title="decoder two")
    log_scans(db, 1, title="later OCR", source="ocr")
    s.step()
    assert server.scan_ids() == [3, 6]
    assert sync.pending(db, URL) == 0 and sync.pushed(db, URL) == 6
    assert db.conn.execute("SELECT COUNT(*) FROM scans WHERE source = 'ocr'").fetchone() == (5,)
    assert all(icon['source'] == 'meowdb' for _, body in server.pushes for icon in body['icons'])


def test_a_failed_push_is_retried_later(setup):
    db, settings, server, s = setup
    log_scans(db, 2)
    server.down = True
    assert s.step() == sync.RETRY[0]
    s.clock.advance(sync.RETRY[0])
    assert s.step() == 2 * sync.RETRY[0]                 # backs off
    assert "Can't reach cozy.markets" in s.status() and sync.pending(db, URL) == 2
    server.down = False
    s.clock.advance(2 * sync.RETRY[0])
    assert s.step() == sync.INTERVAL
    assert server.scan_ids() == [1, 2] and s.retry == 0 and "all caught up" in s.status()

    log_scans(db, 1)
    server.status = 429
    assert s.step() == 120 and "busy" in s.status()      # waits as long as it's told
    server.status = 503                                  # the server's queue is full
    s.clock.advance(120)
    assert s.step() == 240 and "busy" in s.status()      # busy again: waits longer, never less than told


def test_scans_go_up_under_the_name_you_pick_else_anonymously(setup):
    db, settings, server, s = setup
    s.batch_size = 2
    log_scans(db, 2, scanned_by="Cheng")       # the name read off the status bar stays on this PC
    assert settings.share_name == ""
    s.step()
    assert [sc["scanned_by"] for _, body in server.pushes for sc in body["scans"]] == [None, None]
    assert s.status() == "Sharing anonymously: all caught up."
    settings.share_name = "Cozy"                # picked in F10
    log_scans(db, 2, scanned_by="Cheng")
    assert "waiting to be shared as Cozy" in s.status()
    s.step()
    assert [sc["scanned_by"] for _, body in server.pushes[1:] for sc in body["scans"]] == ["Cozy", "Cozy"]
    assert s.status() == "Sharing as Cozy: all caught up."


class Signed:
    """auth.AuthService's surface for linking: who's signed in, and the routes it was asked for."""
    def __init__(self, user_id="7", refuse=False):
        from mprice_decoder.auth import Session
        self.session = Session(access_token="mp_" + "x" * 43, user={"id": user_id, "name": "Lotzz"},
                               expires_at=time.time() + 3600)
        self.calls, self.refuse = [], refuse

    def api(self, method, path, body=None):
        from mprice_decoder.auth import AuthError
        self.calls.append((method, path, body))
        if self.refuse:
            raise AuthError("server", "Cozy Markets can't link that right now")
        return {"linked": path == "/link", "scans": 3}


def test_a_signed_in_pcs_shared_shops_are_linked_to_its_discord_account_once(setup):
    db, settings, server, s = setup
    service = Signed()
    assert s.link_owner(service) is False and service.calls == []      # nothing registered yet
    log_scans(db, 1)
    s.step()                                                           # registers, pushes
    token = db.meta(f"{sync.PUBLIC_TOKEN} {sync.server(settings.cozy_url)}")
    assert s.link_owner(service) and service.calls == [("POST", "/link", {"token": token})]
    assert s.link_owner(service) and len(service.calls) == 1            # once per token and account
    assert s.link_owner(Signed(user_id="8")) and db.meta(sync.OWNER).startswith("8 ")  # another account: again
    s.unlink_owner(service)
    assert service.calls[-1] == ("POST", "/unlink", {"token": token}) and db.meta(sync.OWNER) is None


def test_linking_never_sends_a_guild_token_and_waits_out_a_refusal(setup):
    db, settings, server, s = setup
    log_scans(db, 1)
    s.step()
    refusing = Signed(refuse=True)
    assert s.link_owner(refusing) is False and db.meta(sync.OWNER) is None      # tried again after the next push
    s.owner = Signed()
    log_scans(db, 1, title="another")
    clock = s.clock
    clock.advance(sync.MAX_WAIT)
    s.step()
    assert s.owner.calls and db.meta(sync.OWNER) is not None                    # the push linked it
    settings.cozy_token = "guild-token"
    guild = Signed()
    db.set_meta(sync.OWNER, None)
    assert s.link_owner(guild) is False and guild.calls == []
    s.private = object()
    settings.cozy_token = ""
    assert s.link_owner(guild) is False and guild.calls == []


def test_a_light_scanners_shops_go_up_almost_at_once(setup, caplog):
    db, settings, server, _ = setup
    clock = Clock()
    s = Sync(db, settings, session=server, startup_delay=0, key=False, clock=clock)
    db.on_scan = s.notify
    caplog.set_level("INFO", logger="mprice_decoder")
    assert s.batch_size == sync.MIN_BATCH == 1             # nothing scanned yet: a shop is a batch
    s.step()  # no registration or upload for an empty queue
    log_scans(db, 1)
    s.step()
    assert server.scan_ids() == [1]                        # one shop: up at once
    clock.advance(60)
    log_scans(db, 1, title="a minute later")
    s.step()
    assert server.scan_ids() == [1, 2] and sync.pending(db, URL) == 0
    assert "shop batch uploaded: 1/1 visits accepted; 0 waiting locally" in caplog.text


def test_a_half_filled_batch_still_goes_within_a_minute(setup):
    db, settings, server, _ = setup
    clock = Clock()
    s = Sync(db, settings, session=server, startup_delay=0, key=False, clock=clock)
    for _ in range(80):                                    # 80 shops in 40 s: about 60 a minute
        clock.advance(0.5)
        s.notify(1)
    size = s.batch_size
    assert size in (60, 61)                                # 30 s worth at 2 a second
    log_scans(db, 7)                                       # then the scanning stops, 7 short of nothing
    wait = s.step()
    assert server.pushes == [] and 0 < wait <= sync.MAX_WAIT
    assert f"7/{size} shops saved locally; uploads at {size} or within 1 minute." in s.status()
    clock.advance(sync.MAX_WAIT)
    s.step()
    assert server.scan_ids() == list(range(1, 8))          # waited a minute: as it is


def test_the_batch_follows_the_scanning_up_and_back_down(setup):
    db, settings, server, _ = setup
    clock = Clock()
    s = Sync(db, settings, session=server, startup_delay=0, key=False, clock=clock)
    for _ in range(600):                                   # 5 shops a second (fast walking, the decoder)
        clock.advance(0.2)
        s.notify(1)
    assert s.batch_size == sync.MAX_BATCH                  # 150 at that pace: capped
    clock.advance(sync.RATE_WINDOW)                        # two quiet minutes
    assert s.batch_size == sync.MIN_BATCH
    for _ in range(20):                                    # a shop every 6 s
        clock.advance(6)
        s.notify(1)
    size = s.batch_size
    assert size in (5, 6)                                  # 20 in 2 minutes: 5 every 30 s
    log_scans(db, size)
    s.step()
    assert len(server.pushes) == 1 and len(server.pushes[0][1]["scans"]) == size  # a batch's worth: at once


def test_starting_to_scan_fast_long_after_starting_doesnt_push_every_few_shops(setup):
    """2026-10-21: MaplePrice ran 13 minutes before the first shop, then a shop or two a second went up in 12
    pushes in a minute: the rate was taken over the whole 2 minutes, though the scanning had just started."""
    db, settings, server, _ = setup
    clock = Clock()
    s = Sync(db, settings, session=server, startup_delay=0, key=False, clock=clock)
    db.on_scan = s.notify
    clock.advance(13 * 60)
    for _ in range(120):                                   # a shop a second, for 2 minutes
        clock.advance(1)
        log_scans(db, 1)
        s.step()
    assert len(server.pushes) <= 1 + 120 // sync.PUSH_EVERY  # the first shop, then about every 30 s


def test_backlog_sends_full_batches_and_keeps_the_remainder(setup):
    db, settings, server, _ = setup
    s = Sync(db, settings, session=server, startup_delay=0, key=False, batch_size=sync.BATCH)
    log_scans(db, 63)
    s.step()
    assert [len(body["scans"]) for _, body in server.pushes] == [25, 25]
    assert server.scan_ids() == list(range(1, 51)) and sync.pending(db, URL) == 13
    s.step()
    assert len(server.pushes) == 2
    log_scans(db, 12, title="next shops")
    s.step()
    assert [len(body["scans"]) for _, body in server.pushes] == [25, 25, 25]
    assert server.scan_ids() == list(range(1, 76)) and sync.pending(db, URL) == 0


def test_partial_batches_and_upload_progress_survive_restarting(setup, tmp_path):
    db, settings, server, _ = setup
    s = Sync(db, settings, session=server, startup_delay=0, key=False, batch_size=sync.BATCH)
    log_scans(db, 24)
    s.step()
    db.conn.close()

    db = make_db(tmp_path)
    s = Sync(db, settings, session=server, startup_delay=0, key=False, batch_size=sync.BATCH)
    s.step()
    assert sync.pending(db, URL) == 24 and server.registered == 0
    log_scans(db, 1, title="shop after restart")
    s.step()
    assert server.scan_ids() == list(range(1, 26))
    log_scans(db, 3, title="next batch")
    db.conn.close()

    db = make_db(tmp_path)
    s = Sync(db, settings, session=server, startup_delay=0, key=False, batch_size=sync.BATCH)
    s.step()
    assert sync.pending(db, URL) == 3 and len(server.pushes) == 1
    log_scans(db, 22, title="after second restart")
    s.step()
    assert server.scan_ids() == list(range(1, 51)) and server.registered == 1
    assert sync.pending(db, URL) == 0
    db.conn.close()


@pytest.mark.parametrize("failure", [429, 503, "offline"])
def test_new_visits_cannot_bypass_the_upload_retry_delay(setup, failure):
    db, settings, server, _ = setup
    settings.cozy_token = "guild-token"
    clock = Clock()
    s = Sync(db, settings, session=server, startup_delay=0, key=False, clock=clock, batch_size=sync.BATCH)
    db.on_scan = s.notify
    attempts, post = [], server.post

    def record_attempt(*args, **kwargs):
        attempts.append(kwargs["json"])
        return post(*args, **kwargs)

    server.post = record_attempt
    server.status = failure if isinstance(failure, int) else None
    server.down = failure == "offline"
    log_scans(db, 25)
    wait = s.step()
    assert wait == (30 if failure == "offline" else 120)
    assert len(attempts) == 1 and sync.pending(db, URL) == 25
    server.status, server.down = None, False
    for elapsed in range(1, 4):
        clock.advance(1)
        log_scans(db, 1, title=f"arrived during backoff {elapsed}")
        assert s.wake.is_set()
        s.wake.clear()
        assert s.step() == wait - elapsed and len(attempts) == 1
    clock.advance(wait - 3)
    s.step()
    assert len(attempts) == 2 and server.scan_ids() == list(range(1, 26))
    assert sync.pending(db, URL) == 3 and s.retry == 0


def test_failed_split_upload_keeps_the_whole_batch_for_retry_after_restart(setup, tmp_path):
    db, settings, server, s = setup
    s.batch_size = 4
    log_scans(db, 4)
    post = server.post

    def interrupted(url, **kwargs):
        scans = kwargs.get("json", {}).get("scans", [])
        if len(scans) > 2:
            return Response(413)
        if scans and scans[0]["client_id"] == 3:
            raise ConnectionError("disconnected after first half")
        return post(url, **kwargs)

    server.post = interrupted
    s.step()
    assert server.scan_ids() == [1, 2]  # server took half, but the batch isn't complete
    assert sync.pending(db, URL) == 4 and sync.pushed(db, URL) == 0
    db.conn.close()

    server.post = post
    db = make_db(tmp_path)
    s = Sync(db, settings, session=server, startup_delay=0, key=False, batch_size=4)
    s.step()
    # Replays use the same install/client ids so the server can deduplicate them.
    assert server.scan_ids() == [1, 2, 1, 2, 3, 4] and sync.pending(db, URL) == 0
    assert server.pushes[0][1]["install_id"] == server.pushes[-1][1]["install_id"]
    db.conn.close()


def test_oversized_batches_split_with_only_their_own_item_metadata(setup):
    db, settings, server, s = setup
    s.batch_size = 4
    for item_id in (95, 96):
        db.info(item_id)
        db.icon_png(item_id)
        db.market(item_id)
    for index, item_id in enumerate((355, 355, 95, 96)):
        db.record_scan(f"shop {index}", f"Bob{index}", [(item_id, str(item_id), 1, 500, True, b"png")], source="decoder")
    post = server.post

    def size_limit(url, **kwargs):
        if len(kwargs.get("json", {}).get("scans", [])) > 2:
            return Response(413)
        return post(url, **kwargs)

    server.post = size_limit
    s.step()
    assert [len(body["scans"]) for _, body in server.pushes] == [2, 2]
    assert server.scan_ids() == [1, 2, 3, 4] and sync.pending(db, URL) == 0
    for (_, body), expected_ids in zip(server.pushes, ({355}, {95, 96})):
        assert {item["id"] for item in body["items"]} == expected_ids
        assert {item["item_id"] for item in body["fm"]} == expected_ids
        assert {item["item_id"] for item in body["icons"]} == expected_ids


def test_disabling_sharing_during_a_split_stops_the_remaining_requests(setup):
    db, settings, server, s = setup
    s.batch_size = 4
    log_scans(db, 4)
    post = server.post

    def switched_off(url, **kwargs):
        scans = kwargs.get("json", {}).get("scans", [])
        if len(scans) > 2:
            return Response(413)
        response = post(url, **kwargs)
        if scans:
            settings.share_scans = False
            s.notify()
        return response

    server.post = switched_off
    s.step()
    assert server.scan_ids() == [1, 2] and sync.pending(db, URL) == 0
    settings.share_scans = True
    s.step()
    assert server.scan_ids() == [1, 2]


def test_a_name_has_to_be_one_the_board_takes():
    from mprice_decoder.settings import share_name
    assert share_name("Cheng") == "Cheng" and share_name("  Ab12  ") == "Ab12"
    assert share_name("") == "" and share_name("anonymous") == "" and share_name("Anonymous") == ""
    assert share_name("x") is None and share_name("has space") is None and share_name("ほげ") is None
    assert share_name("a" * 14) is None and share_name("a" * 13) == "a" * 13


def test_a_guild_token_shares_as_you(setup):
    db, settings, server, s = setup
    settings.cozy_token = " guild-token "
    log_scans(db, 1)
    s.step()
    assert server.pushes[0][0] == "guild-token" and server.registered == 0
    assert "as Alice" in s.status()           # no name picked: the token's
    settings.share_name = "Cheng"
    log_scans(db, 1, scanned_by="Other")
    s.step()
    assert "as Cheng" in s.status()           # the name picked in F10 wins
    assert server.pushes[-1][1]["scans"][0]["scanned_by"] == "Cheng"


def test_a_refused_guild_token_is_not_tried_again_until_changed(setup):
    db, settings, server, s = setup
    settings.cozy_token = "typo"
    log_scans(db, 1)
    s.step()
    assert "didn't accept the cozy_token" in s.status()
    server.post = lambda *a, **k: pytest.fail("tried the refused token again")
    s.step()
    assert "didn't accept the cozy_token" in s.status()
    del server.post
    settings.cozy_token = "guild-token"
    s.clock.advance(s.retry)
    s.step()
    assert server.scan_ids() == [1] and "as Alice" in s.status()


def test_a_revoked_public_token_is_replaced(setup):
    db, settings, server, s = setup
    log_scans(db, 1)
    s.step()
    del server.tokens["public-1"]          # revoked on the server
    log_scans(db, 1)
    s.step()
    assert db.meta(f"{sync.PUBLIC_TOKEN} {URL}") is None
    s.clock.advance(s.retry)
    s.step()
    assert server.registered == 2 and server.pushes[-1][0] == "public-2" and server.scan_ids() == [1, 2]


def test_scans_logged_while_sharing_is_off_are_never_sent(setup):
    db, settings, server, s = setup
    db.on_scan = s.notify
    settings.share_scans = False
    log_scans(db, 3)                       # marked as handled as they're logged
    assert sync.pending(db, URL) == 0
    assert s.step() == sync.INTERVAL and server.pushes == [] and server.registered == 0
    assert s.status().startswith("Off")
    settings.share_scans = True
    log_scans(db, 1)
    s.step()
    assert server.scan_ids() == [4]


def test_nothing_is_sent_before_the_startup_delay(setup):
    db, settings, server, _ = setup
    now = [100.0]
    s = Sync(db, settings, session=server, clock=lambda: now[0], startup_delay=30)
    log_scans(db, 25)
    assert s.step() == 30 and server.pushes == []
    assert "25 shops waiting" in s.status()
    now[0] = 131
    s.step()
    assert server.scan_ids() == list(range(1, 26))


def test_a_bad_scan_is_skipped_instead_of_blocking_the_rest(setup):
    db, settings, server, s = setup
    s.batch_size = 5
    log_scans(db, 5)
    server.bad_scans = {3}
    s.step()
    assert server.scan_ids() == [1, 2, 4, 5] and sync.pending(db, URL) == 0


def test_a_server_that_refuses_everything_skips_nothing(setup):
    db, settings, server, s = setup
    s.batch_size = 3
    log_scans(db, 3)
    server.status = 400                    # e.g. this mprice_decoder is too old for it
    s.step()
    assert sync.pending(db, URL) == 3 and "may need an update" in s.status()
    server.status = None
    s.clock.advance(s.retry)
    s.step()
    assert server.scan_ids() == [1, 2, 3]


def test_clearing_the_database_starts_a_new_install(setup):
    db, settings, server, s = setup
    log_scans(db, 2)
    s.step()
    first = db.meta(sync.INSTALL_ID)
    with db.lock:  # the database cleared (what MapleDash's "clear everything" did): its Cozy Markets token stays
        tables = [r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'meta'")]
        for t in tables:
            db.conn.execute(f'DELETE FROM "{t}"')
        db.conn.execute("DELETE FROM sqlite_sequence")
        db.conn.execute("DELETE FROM meta WHERE key NOT LIKE 'cozy\\_public\\_token %' ESCAPE '\\'")
        db.conn.commit()
    assert db.meta(f"{sync.PUBLIC_TOKEN} {URL}") == "public-1"   # who you are survives a clear
    db.lookup("Pig's Head")
    log_scans(db, 1)                                  # scan ids start again at 1
    s.step()
    _, body = server.pushes[-1]
    assert body["install_id"] != first and [x["client_id"] for x in body["scans"]] == [1]


def test_a_push_never_moves_the_mark_of_a_database_cleared_meanwhile(setup):
    db, *_ = setup
    log_scans(db, 3)
    install = sync.install_id(db)
    db.set_meta(sync.INSTALL_ID, "someone-cleared-it")
    sync.mark_pushed(db, install, 3, URL)
    assert sync.pushed(db, URL) == 0


def test_a_test_server_doesnt_change_what_the_real_one_is_sent(setup):
    db, settings, server, s = setup
    log_scans(db, 2)
    settings.cozy_url = "http://localhost:8080/"
    s.step()
    assert server.scan_ids() == [1, 2] and sync.pending(db, settings.cozy_url) == 0
    settings.cozy_url = URL
    assert sync.pending(db, URL) == 2
    s.step()
    assert server.scan_ids() == [1, 2, 1, 2] and server.registered == 2   # its own token there too


def test_a_push_finishing_after_sharing_was_switched_off_never_moves_the_mark_back(setup):
    db, settings, server, s = setup
    log_scans(db, 5)
    install = sync.install_id(db)
    settings.share_scans = False
    s.skip_pending()                         # switched off while scans 1-3 were still uploading
    sync.mark_pushed(db, install, 3, URL)    # ...and then that upload finished
    assert sync.pushed(db, URL) == 5
    settings.share_scans = True
    s.step()
    assert server.pushes == []               # 4 and 5 were logged before it was switched off: never sent


def test_tokens_only_go_over_https():
    assert sync.base_url("https://cozy.markets/") == "https://cozy.markets"
    assert sync.base_url("http://localhost:8080") == "http://127.0.0.1:8080"   # not ~21s of IPv6 first
    assert sync.base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    for bad in ("http://cozy.markets", "ftp://x", "cozy.markets", ""):
        with pytest.raises(ValueError):
            sync.base_url(bad)


def test_an_insecure_url_stops_sharing(setup):
    db, settings, server, s = setup
    settings.cozy_url = "http://cozy.markets"
    log_scans(db, 1)
    s.step()
    assert server.pushes == [] and server.registered == 0 and "https" in s.status()


def test_the_token_stays_out_of_the_log():
    assert "secret" not in repr(Settings(cozy_token="secret"))


def test_an_item_that_cant_be_traded_has_no_fm_price_instead_of_an_error():
    from mprice_decoder.data import Market, MeowDB

    class Answer:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def json(self):
            return self.body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    meow = MeowDB()
    meow.session.get = lambda url, params=None, timeout=None: Answer(
        400, {"error": "This item can't be traded on the Free Market", "code": "fm_not_tradeable"})
    assert meow.market(217) == Market(None, 0)   # so ItemDB caches it, rather than asking on every scan
    meow.session.get = lambda url, params=None, timeout=None: Answer(500, {})
    with pytest.raises(RuntimeError):
        meow.market(217)                         # a real failure still is one


def make_client_key(tmp_path):
    """A client key as release.sh bundles it, and its public half."""
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path = tmp_path / "client.key"
    path.write_text(base64.b64encode(seed).decode() + "\n")
    return path, private.public_key()


def test_an_official_build_signs_its_registration(setup, tmp_path):
    db, settings, server, _ = setup
    path, public = make_client_key(tmp_path)
    key = sync.client_key(path)
    assert key is not None
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    server.client_keys = {key[0]: public}
    assert key[0] == __import__("hashlib").sha256(raw).hexdigest()[:8]  # the id the server knows it by
    s = Sync(db, settings, session=server, startup_delay=0, key=key, batch_size=1)
    log_scans(db, 1)
    s.step()
    assert server.registered == 1 and server.scan_ids() == [1] and s.problem is None
    body, headers = server.registrations[0]
    sent = jsonlib.loads(body)
    assert sent["client"] == sync.CLIENT and sent["install"] == sync.install_id(db)
    assert abs(sent["ts"] - time.time()) < 60 and len(sent["nonce"]) >= 16
    _, again = sync.registration("pc", key)
    assert jsonlib.loads(_)["nonce"] != sent["nonce"]  # every registration is new: a copied one won't work twice


def test_an_unofficial_copy_says_so_and_waits(setup, tmp_path):
    db, settings, server, s = setup  # no client key: registrations go unsigned
    _, public = make_client_key(tmp_path)
    server.client_keys = {"a1b2c3d4": public}  # the server only takes official builds
    log_scans(db, 1)
    wait = s.step()
    assert s.problem == sync.UNOFFICIAL and wait == sync.RETRY[1]
    assert server.registered == 0 and server.pushes == [] and db.meta(f"{sync.PUBLIC_TOKEN} {URL}") is None
    assert "X-Cozy-Signature" not in server.registrations[0][1]


def test_a_build_without_a_client_key_registers_unsigned(tmp_path):
    assert sync.client_key(tmp_path / "missing.key") is None
    (tmp_path / "bad.key").write_text("not base64!")
    assert sync.client_key(tmp_path / "bad.key") is None
    body, headers = sync.registration("pc", None)
    assert "X-Cozy-Key" not in headers and jsonlib.loads(body)["install"] == "pc"


def private_sync(db, settings, server, monkeypatch, *, token="cozy_private_friends_token_for_testing", clock=None):
    access = PrivateAccess(token=token, token_id=42, label="friends test build")
    monkeypatch.setattr(sync, "private_access", lambda: access)
    monkeypatch.setattr(sync, "client_key", lambda: pytest.fail("private build loaded a signing key"))
    return Sync(db, settings, session=server, startup_delay=0, clock=clock or Clock(), batch_size=sync.BATCH)


def test_private_build_uses_its_shared_credential_on_two_fresh_pcs(tmp_path, monkeypatch):
    settings, server = Settings(), FakeServer()
    token = "cozy_private_friends_token_for_testing"
    server.tokens[token] = ("public", None)
    first, second = tmp_path / "first-pc", tmp_path / "second-pc"
    first.mkdir()
    second.mkdir()
    dbs = [make_db(path) for path in (first, second)]
    for db in dbs:
        db.lookup("Pig's Head")
        s = private_sync(db, settings, server, monkeypatch)
        log_scans(db, 25)
        s.step()
        assert sync.pending(db, URL) == 0 and s.key is None
    assert server.registered == 0 and len(server.pushes) == 2
    assert {used for used, _ in server.pushes} == {token}
    assert len({body["install_id"] for _, body in server.pushes}) == 2
    assert [len(body["scans"]) for _, body in server.pushes] == [25, 25]
    for db in dbs:
        db.conn.close()


def test_private_credential_ignores_old_settings_and_anonymous_tokens(setup, monkeypatch):
    db, settings, server, _ = setup
    settings.cozy_token = "guild-token"
    settings.cozy_url = "https://elsewhere.example"
    db.set_meta(f"{sync.PUBLIC_TOKEN} {URL}", "old-anonymous-token")
    s = private_sync(db, settings, server, monkeypatch)
    server.tokens[s.private.token] = ("public", None)
    post = server.post

    def pinned_url(url, **kwargs):
        assert url == f"{URL}/api/ingest"
        return post(url, **kwargs)

    server.post = pinned_url
    log_scans(db, 25)
    s.step()
    assert server.registered == 0 and server.pushes[0][0] == s.private.token
    assert sync.pending(db, URL) == 0
    assert db.meta(f"{sync.PUBLIC_TOKEN} {URL}") == "old-anonymous-token"


@pytest.mark.parametrize("status", [401, 403])
def test_private_revocation_stops_uploads_without_registering_or_using_another_token(setup, monkeypatch, caplog, status):
    db, settings, server, _ = setup
    s = private_sync(db, settings, server, monkeypatch)
    server.tokens[s.private.token] = ("public", None)
    log_scans(db, 25)
    s.step()
    assert sync.pending(db, URL) == 0
    log_scans(db, 25, title="after revocation")
    server.status = status
    s.step()
    assert s.status() == sync.PRIVATE_REJECTED and sync.pending(db, URL) == 25
    assert s.private.token not in caplog.text
    server.post = lambda *args, **kwargs: pytest.fail("revoked private build contacted the server again")
    settings.cozy_token = "guild-token"
    settings.cozy_url = "https://elsewhere.example"
    for _ in range(3):
        s.clock.advance(sync.INTERVAL)
        log_scans(db, 1, title="another shop")
        s.notify()
        s.step()
    assert s.status() == sync.PRIVATE_REJECTED and sync.pending(db, URL) == 28
    assert server.registered == 0 and len(server.pushes) == 1
    with pytest.raises(sync.Rejected):
        s.public_token(URL)


def test_private_revocation_after_restart_never_replaces_the_token(setup, monkeypatch, tmp_path):
    db, settings, server, _ = setup
    s = private_sync(db, settings, server, monkeypatch)
    log_scans(db, 25)
    s.step()  # unknown/revoked token
    assert s.status() == sync.PRIVATE_REJECTED and server.registered == 0
    db.conn.close()
    db = make_db(tmp_path)
    s = private_sync(db, settings, server, monkeypatch)
    s.step()
    assert sync.pending(db, URL) == 25 and s.status() == sync.PRIVATE_REJECTED
    assert server.registered == 0 and server.pushes == []
    db.conn.close()


def test_missing_private_credential_keeps_capture_local_and_never_registers(setup, monkeypatch):
    db, settings, server, _ = setup
    s = private_sync(db, settings, server, monkeypatch, token="")
    server.post = lambda *args, **kwargs: pytest.fail("unconfigured private build contacted the server")
    log_scans(db, 25)
    s.step()
    assert s.status() == sync.PRIVATE_MISSING and sync.pending(db, URL) == 25


def test_private_batch_network_failure_retries_with_the_same_credential(setup, monkeypatch):
    db, settings, server, _ = setup
    s = private_sync(db, settings, server, monkeypatch)
    server.tokens[s.private.token] = ("public", None)
    log_scans(db, 25)
    server.down = True
    assert s.step() == sync.RETRY[0] and sync.pending(db, URL) == 25
    server.down = False
    s.clock.advance(sync.RETRY[0])
    s.step()
    assert len(server.pushes) == 1 and server.pushes[0][0] == s.private.token
    assert sync.pending(db, URL) == 0 and server.registered == 0


# ---- reading the board with the token (sync.read, Sync.read_token) ----

class ReadSession:
    """Answers GETs with `status`, noting the Authorization each one went with."""

    def __init__(self, *statuses):
        self.statuses, self.auth = list(statuses), []

    def get(self, url, **kwargs):
        self.auth.append((kwargs.get("headers") or {}).get("Authorization"))
        import requests
        r = requests.Response()
        r.status_code = self.statuses.pop(0)
        return r


def test_reads_go_with_the_public_token_and_a_refused_one_is_dropped(tmp_path):
    from mprice_decoder.sync import PUBLIC_TOKEN, read
    db = make_db(tmp_path)
    sync = Sync(db, Settings(cozy_url="https://cozy.markets"), key=False)
    db.set_meta(f"{PUBLIC_TOKEN} https://cozy.markets", "tok")
    session = ReadSession(200)
    assert read(session, "https://cozy.markets/api/fm-prices", sync, timeout=5).status_code == 200
    assert session.auth == ["Bearer tok"]
    session = ReadSession(401, 200)  # turned down: dropped, and asked again without it
    assert read(session, "https://cozy.markets/api/fm-prices", sync, timeout=5).status_code == 200
    assert session.auth == ["Bearer tok", None]
    assert db.meta(f"{PUBLIC_TOKEN} https://cozy.markets") is None


def test_no_token_goes_to_another_server(tmp_path):
    from mprice_decoder.sync import PUBLIC_TOKEN
    db = make_db(tmp_path)
    sync = Sync(db, Settings(cozy_url="https://cozy.markets"), key=False)
    db.set_meta(f"{PUBLIC_TOKEN} https://cozy.markets", "tok")
    assert sync.read_token("https://cozy.markets/api/items/5/sightings") == "tok"
    assert sync.read_token("https://evil.example/api/fm-prices") is None


def room(channel=6, number=4, shops=1, age=0):
    return {"channel": channel, "room": number, "map_name": f"Free Market <{number}>", "unplaced": [],
            "shops": [{"seller": f"Seller{i}", "title": "Cheap", "x": 100 * i, "y": 34} for i in range(shops)],
            "at": time.time() - age}


def test_an_fm_rooms_shops_go_with_the_next_round_the_latest_only(setup):
    db, settings, server, s = setup
    s.room(room(shops=1))
    s.room(room(shops=3))     # the same room again before it went: only this one goes
    s.room(room(number=5))
    s.step()
    assert [(b["room"], len(b["shops"])) for _, b in server.rooms] == [(4, 3), (5, 1)]
    token, body = server.rooms[0]
    assert token.startswith("public-") and body["install_id"] == sync.install_id(db) and body["channel"] == 6
    assert s.rooms == {}


def test_rooms_arent_sent_while_sharing_is_off_nor_when_old(setup):
    db, settings, server, s = setup
    s.room(room(age=sync.ROOM_STALE + 5))
    s.step()
    assert server.rooms == [] and s.rooms == {}
    settings.share_scans = False
    s.room(room())
    s.step()
    assert server.rooms == []


def test_a_server_without_rooms_drops_them_and_shares_scans_as_ever(setup):
    db, settings, server, s = setup
    server.room_status = 404
    s.room(room())
    log_scans(db, 30)
    s.step()
    assert s.rooms == {} and server.pushes


def test_a_busy_server_keeps_the_room_for_later(setup):
    db, settings, server, s = setup
    server.room_status = 503
    s.room(room())
    s.step()
    assert len(s.rooms) == 1
    server.room_status = None
    s.not_before = 0
    s.step()
    assert len(server.rooms) == 1
