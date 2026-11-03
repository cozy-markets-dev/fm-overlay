"""Bulk downloads from Cozy Markets (the item list and every FM price), against a fake server."""
import pytest

from mprice_decoder import feed
from mprice_decoder.data import CATALOG_AT
from mprice_decoder.feed import Feed
from mprice_decoder.settings import Settings

from test_shop import Clock, FakeSource, make_db, names

NOW = Clock.t


def catalog(items=FakeSource.ITEMS.values()):
    return [{"id": i.id, "name": i.name, "npc_price": i.price,
             "info": {"type": "Use", "subtype": "Scroll", "levelReq": 0, "reqJob": None,
                      "stats": {"price": i.price}, "iconKey": f"k{i.id}"}} for i in items]


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.content = b"x" * 2048

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCozy:
    """Cozy Markets' two downloads, with ETags."""

    def __init__(self):
        self.headers = {}
        self.items = catalog()
        self.fm = [{"item_id": 355, "usual": 9000, "reports": 6, "sold": 2, "for_sale": 1,
                    "cheapest_sell": 8500, "best_buy": 8000, "fetched_at": NOW - 3600}]
        self.asked = []  # (path, If-None-Match)
        self.down = False

    def get(self, url, timeout=None, headers=None):
        if self.down:
            raise ConnectionError("no route to host")
        path = url.removeprefix(Settings().cozy_url)
        body = {"/api/catalog": {"items": self.items}, "/api/fm-prices": {"fm": self.fm}}[path]
        etag = f'W/"{hash(repr(body))}"'
        sent = (headers or {}).get("If-None-Match")
        self.asked.append((path, sent))
        if sent == etag:
            return Response(304, headers={"ETag": etag})
        return Response(200, body, {"ETag": etag})


@pytest.fixture
def setup(tmp_path):
    db, server = make_db(tmp_path), FakeCozy()
    return db, server, Feed(db, Settings(), session=server)


def test_after_the_downloads_lookups_dont_wait_on_meowdb(setup):
    db, server, f = setup
    assert f.step() == feed.POLL
    match = db.lookup("Pig's Head")
    assert names(match) == ["Pig's Head"]
    assert match.items[0].price == 7
    assert db.market(355).usual == 9000
    assert db.market(355).cheapest_sell == 8500
    assert db.info(355)["iconKey"] == "k355"
    assert names(db.lookup("Earring DEX Scroll: Inter...")) == ["Earring DEX Scroll: Intermediate"]
    assert db.source.calls == 0  # all from the downloads


def test_polls_only_download_what_changed(setup):
    db, server, f = setup
    f.step()
    server.asked.clear()
    f.step()
    assert [sent is not None for _, sent in server.asked] == [True, True]  # both 304s
    server.fm[0]["usual"] = 9500
    server.fm[0]["fetched_at"] = NOW - 60
    f.step()
    assert db.market(355).usual == 9500


def test_an_older_price_never_replaces_a_newer_one(setup):
    db, server, f = setup
    db.market(355)  # fetched from MeowDB just now: 21500
    f.step()        # the download's is an hour old
    assert db.market(355).usual == 21500


def test_a_price_older_than_the_ttl_is_fetched_from_meowdb(setup):
    db, server, f = setup
    server.fm[0]["fetched_at"] = NOW - 25 * 3600
    f.step()
    assert db.market(355).usual == 21500
    assert db.source.calls == 1


def test_a_cleared_database_downloads_it_all_again(setup):
    db, server, f = setup
    f.step()
    with db.lock:  # the database cleared
        for t in ("items", "item_info", "market_v2"):
            db.conn.execute(f"DELETE FROM {t}")
        db.conn.execute("DELETE FROM meta WHERE key NOT IN ('ttl', 'imported_seen')")
        db.conn.commit()
    server.asked.clear()
    f.step()
    assert [sent for _, sent in server.asked] == [None, None]
    assert db.market(355).usual == 9000


def test_a_failed_download_backs_off_and_meowdb_is_asked_meanwhile(setup):
    db, server, f = setup
    server.down = True
    assert f.step() == feed.RETRY[0]
    assert f.step() == feed.RETRY[0] * 2
    assert names(db.lookup("Pig's Head")) == ["Pig's Head"]
    assert db.source.calls > 0
    server.down = False
    assert f.step() == feed.POLL


def test_an_item_missing_from_the_list_is_still_found_on_meowdb(setup):
    db, server, f = setup
    server.items = [i for i in server.items if i["id"] != 355]
    f.step()
    assert names(db.lookup("Pig's Head")) == ["Pig's Head"]
    assert db.source.calls > 0


def test_bad_rows_are_skipped(setup):
    db, server, f = setup
    server.items += [{"id": "x", "name": "Bad"}, {"name": "No id"}, {"id": 7, "name": ""}, "junk"]
    server.fm += [{"item_id": 95}, {"usual": 5}, None]
    f.step()
    assert db.import_catalog([]) == 0
    assert names(db.lookup("Pig's Head")) == ["Pig's Head"]


def test_downloads_only_go_over_https(setup):
    db, server, f = setup
    f.settings.cozy_url = "http://cozy.markets"
    assert f.step() == feed.POLL
    assert server.asked == []


@pytest.mark.parametrize("ocr", ["Pig's Head", "Pigs Head", "Earring DEX Scroll: Inter...", "Earring DEX Scroll: ...",
                                 "Earring DEX Scroll: Intermediate", "Earring DEX Scrol: Lesser", "Earing DEX Scroll",
                                 "Arrows for Bows", "xx"])
def test_names_match_the_same_from_the_list_as_from_meowdb(tmp_path, ocr):
    remote = make_db(tmp_path / "remote")
    local = make_db(tmp_path / "local")
    local.import_catalog(catalog())
    assert local.meta(CATALOG_AT) is not None
    assert names(local.lookup(ocr)) == names(remote.lookup(ocr))
    assert local.source.calls == 0 or not local.lookup(ocr).found
