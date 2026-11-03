"""The overlay's prices: shop rows read by OCR, Cozy Markets' prices first
(MeowDB's for the rest), and the F10 switches for shop and hover prices."""
import io
import json

import pytest

from mprice_decoder import __main__ as app
from mprice_decoder.annotate import Version, make_card, make_price_card
from mprice_decoder.cozy_prices import FRESH, RETRY, CozyPrice, CozyPrices
from mprice_decoder.data import Item, Market, Match
from mprice_decoder.settings import Settings
from mprice_decoder.shop import Line, Listing, Row

from test_shop import make_db


def row(name, qty, price, y=339, sold=False, confirmed=True):
    return Row([name], None, None, confirmed, False, qty, price, 468, y, y + 30, sold)


def text(line):
    return "".join(t for t, _ in line)


def test_without_the_minimap_the_shop_is_still_read(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "read_map", lambda grab: None)
    monkeypatch.setattr(app, "read_shop", lambda grab, attempts: None)  # it got as far as reading the shop
    assert app.scan(lambda: None, make_db(tmp_path), Settings(channel=5))[3] == "No shop window found"


class Response:
    def __init__(self, status, rows=None, etag=None):
        self.status_code, self.headers = status, {"ETag": etag} if etag else {}
        self._json = {"cozy": rows}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, *responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, timeout, headers):
        self.calls.append((url, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


PIG = {"item_id": 355, "usual": 450.0, "basis": "sold", "listings": 30, "sales": 6, "low": 400.0, "low_shop": "Al",
       "low_channel": 3, "low_fm_room": 7, "low_map": "Free Market", "last_sold": 480.0, "trend": "down",
       "trend_pct": -5, "unknown_field": 1}


def test_cozy_prices_are_fetched_when_stale_and_kept_when_cozy_markets_cant_be_reached():
    t = [0.0]
    session = Session(Response(200, [PIG, {"item_id": "bad"}], etag='"v1"'), Response(304),
                      OSError("offline"), Response(200, [], etag='"v2"'))
    prices = CozyPrices(Settings(cozy_url="https://cozy.markets"), session, clock=lambda: t[0])
    pig = prices.get(355)
    assert (pig.usual, pig.low, pig.low_fm_room, pig.sales) == (450.0, 400.0, 7, 6) and prices.get(1) is None
    assert session.calls == [("https://cozy.markets/api/cozy-prices", {})]
    t[0] += FRESH + 1
    assert prices.get(355) == pig and session.calls[-1][1] == {"If-None-Match": '"v1"'}   # 304: nothing new
    t[0] += FRESH + 1
    assert prices.get(355) == pig and len(session.calls) == 3                            # offline: keep it
    t[0] += RETRY - 1
    prices.get(355)
    assert len(session.calls) == 3                                                        # not asked again yet
    t[0] += 2
    assert prices.get(355) is None and len(session.calls) == 4


def test_cards_show_cozy_markets_price_first_and_where_it_saw_a_cheaper_one():
    pig, cozy = Item(355, "Pig's Head", 7), CozyPrice.of(PIG)
    listing = Listing("Pig's Head", 1, 600, 468, 339, 369)
    meow = Market(21500, 3, cheapest_sell=20)
    card = make_card(listing, Match("Pig's Head", [pig]), [Version(pig, meow, cozy)])
    assert text(card.price) == "Asking 600    Cozy ~450 (6 sold)    NPC 7"
    assert [p.text for p in card.pills] == ["33% OVER FM", "COZY SAW ONE FOR 400  CH 3 · FM 7"]
    card = make_card(listing, Match("Pig's Head", [pig]), [Version(pig, meow)])         # no Cozy price: MeowDB's
    assert text(card.price) == "Asking 600    FM ~21.5k (3)    NPC 7" and card.pills[0].text == "DEAL  97% UNDER FM"
    listed = CozyPrice(usual=450.0, basis="week", listings=12)
    hover = make_price_card("Pig's Head", Match("Pig's Head", [pig]), [Version(pig, meow, cozy)])
    assert text(hover.price) == "Cozy ~450 (6 sold)    NPC 7"
    assert [text(l) for l in hover.extra] == ["Cozy  cheapest 400  Ch 3 · FM 7", "Cozy  last sold 480"]
    assert text(make_price_card("Pig's Head", Match("x", [pig]), [Version(pig, None, listed)]).price) == \
        "Cozy ~450 (12 listed)    NPC 7"


def test_lookups_carry_cozy_markets_prices(tmp_path, monkeypatch):
    class Prices:
        def get(self, item_id):
            return CozyPrice(usual=1.0) if item_id == 355 else None
    monkeypatch.setattr(app, "cozy_prices", Prices())
    db = make_db(tmp_path)
    assert app.versions_of(db, [db.item(355)])[0].cozy == CozyPrice(usual=1.0)
    monkeypatch.setattr(app, "cozy_prices", None)
    assert app.versions_of(db, [db.item(355)])[0].cozy is None


def test_f10_switches_set_the_shop_and_hover_settings():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    from mprice_decoder.overlay import OptionsWindow
    settings, changes = Settings(), []
    try:
        win = OptionsWindow(root, settings, lambda: changes.append((settings.shop_prices, settings.hover_prices)),
                            None, None, open_site=lambda: None, on_quit=lambda: None)
        win.toggle()
        boxes = [w for w in win.win.winfo_children()[0].winfo_children() if isinstance(w, tk.Checkbutton)]
        assert [b.cget("text") for b in boxes] == ["Prices on shops", "Prices when hovering an item (F7)",
                                                   "Price alerts from Cozy Markets", "Alert sound",
                                                   "Sync my equips to the Bazaar", "Show synced equips in the Bazaar"]
        assert (win.switches["shop_prices"].get(), win.switches["hover_prices"].get()) == (True, False)
        boxes[0].invoke()
        boxes[1].invoke()
        assert changes == [(False, False), (False, True)]
        settings.hover_prices = False   # its hotkey switched it off
        win.show_settings()
        assert win.switches["hover_prices"].get() is False
        win.volume.set(40)              # the alert sound's volume, under its switch
        win._volume()
        assert settings.alert_volume == 40 and len(changes) == 3
    finally:
        root.destroy()


HAT = {"mp": 34, "wdef": 29, "slots": 7}


def sighting(shop, price, stats=HAT, sold=False, at=100.0):
    return {"at": at, "qty": 1, "price": price, "unit_price": float(price), "sold": sold, "stats": stats, "shop": shop,
            "channel": 7, "fm_room": 8, "map_name": "Free Market <8>"}


class StatsSession:
    def __init__(self, *responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Rows(Response):
    def __init__(self, status, rows=()):
        super().__init__(status)
        self._json = {"rows": list(rows)}


def test_same_stats_are_other_shops_listings_of_exactly_these_stats():
    from mprice_decoder.cozy_prices import LOOKBACK, SameStats
    rows = [sighting("Al", 3500), sighting("Al", 3500, at=200.0),                  # one listing, seen twice
            sighting("Bo", 5000, sold=True, at=300.0), sighting("Di", 9999),
            sighting("Cy", 3000, {"mp": 35, "wdef": 29, "slots": 7}),               # better stats: not the same
            sighting("Bob", 4000)]                                                  # the shop you're in
    t = [0.0]
    session = StatsSession(Rows(200, rows), Rows(404), OSError("offline"))
    prices = CozyPrices(Settings(), session, clock=lambda: t[0], wall=lambda: 2_000_000.0)
    same = prices.same_stats(834, {**HAT, "str": 0}, seller="Bob")
    assert same == SameStats(3, 3500.0, 5000.0, 5000.0, {"price": 3500.0, "shop": "Al", "channel": 7, "fm_room": 8,
                                                          "map": "Free Market <8>"})
    assert session.calls == [("https://cozy.markets/api/items/834/sightings",
                              {"stats": "mp:34,wdef:29,slots:7", "since": int(2_000_000 - LOOKBACK), "size": 100})]
    assert prices.same_stats(834, HAT, seller="Bob") == same and len(session.calls) == 1   # kept a minute
    assert prices.same_stats(9, HAT) == SameStats() and prices.same_stats(10, HAT) is None  # never seen / offline
    assert prices.same_stats(11, None) is None and len(session.calls) == 3                 # no stats: not asked


def test_equip_cards_show_the_prices_of_the_same_stats():
    from mprice_decoder.cozy_prices import SameStats
    hat, cozy = Item(834, "Blue Pole-Feather Hat", 50), CozyPrice(usual=4000.0, basis="sold", sales=3, low=1000.0,
                                                                   low_channel=2, low_fm_room=1)
    same = SameStats(3, 3500.0, 5000.0, 5000.0, {"price": 3500.0, "shop": "Al", "channel": 7, "fm_room": 8, "map": None})
    listing, match = Listing(hat.name, 1, 4200, 468, 339, 369), Match(hat.name, [hat])
    card = make_card(listing, match, [Version(hat, None, cozy)], same=same)
    assert [text(l) for l in card.extra] == ["Same stats  3 on Cozy  ~5,000   low 3,500   sold 5,000"]
    assert [p.text for p in card.pills] == ["FAIR PRICE", "SAME STATS FOR 3,500  CH 7 · FM 8"]  # not the 1k one: other stats
    card = make_card(listing, match, [Version(hat, None, cozy)], same=SameStats())
    assert [text(l) for l in card.extra] == ["Same stats  none on Cozy in 2 weeks"] and len(card.pills) == 1
    hover = make_price_card(hat.name, match, [Version(hat, None, None)], same=same)
    assert [text(l) for l in hover.extra] == ["Same stats  3 on Cozy  ~5,000", "Same stats  cheapest 3,500  Ch 7 · FM 8",
                                              "Same stats  last sold 5,000"]


class StatsPrices:
    def __init__(self):
        self.asked = []

    def get(self, item_id):
        return None

    def same_stats(self, item_id, stats, seller):
        from mprice_decoder.cozy_prices import SameStats
        self.asked.append((item_id, stats, seller))
        return SameStats(2, 3500.0, 3750.0)


def test_hovering_in_the_inventory_reads_the_hovered_tooltip_not_the_worn_one_beside_it():
    """Near the screen's right edge the game slides both tooltips left, so the worn item's (right of the hovered
    one's, level with it) can end up under the cursor."""
    from test_shop import _tooltip
    from mprice_decoder.ocr import TOOLTIP_DOT_DY, read_tooltip
    hovered, cursor = _tooltip("evil_eye_tail")
    worn, worn_cursor = _tooltip("stirge_wing")
    hx, hy = cursor[0], cursor[1] + TOOLTIP_DOT_DY      # roughly where each one's dot is
    wx, wy = worn_cursor[0], worn_cursor[1] + TOOLTIP_DOT_DY
    _, (dx, dy) = read_tooltip(hovered, cursor)
    _, (sx, sy) = read_tooltip(worn, worn_cursor)
    screen = hovered.copy()
    box = worn.crop((sx - 8, sy - 12, sx + 228, sy + 60))   # the worn tooltip's top, dot and name
    screen.paste(box, (dx + 238 - 8, dy - 12))                # 238 px right of the hovered one, level with it
    names, dot = read_tooltip(screen, (dx + 238 + 10, dy - TOOLTIP_DOT_DY))   # cursor over the worn one
    assert dot == (dx, dy) and "evileyetail" in [n.replace(" ", "").lower() for n in names]
    names, dot = read_tooltip(worn, worn_cursor)                # one tooltip alone: as before
    assert dot == (sx, sy)
