from mprice_decoder.annotate import HIGHEST, LOWEST, PROFIT, Version, make_label
from mprice_decoder.data import TTL, Item, ItemDB, Market
from mprice_decoder.shop import Line, parse_listings

# Real OCR output (x, y, text) from tests/fixtures/shop1.png, plus noise lines.
OCR = [
    (441, 188, "pighead 500"),
    (468, 339, "Pig's Head"),
    (468, 356, "1 for 500 mesos"),
    (468, 379, "Earring DEX Scroll: Inter..."),
    (468, 396, "1 for 49,999 mesos"),
    (467, 499, "Arrows for Bows"),
    (468, 516, "1000 for 800 mesos"),
    (664, 344, "Cheng has entered."),
]
LINES = [Line(t, x, y, 100, 12) for x, y, t in OCR]


def test_parse_listings():
    got = [(l.name, l.qty, l.price) for l in parse_listings(LINES)]
    assert got == [
        ("Pig's Head", 1, 500),
        ("Earring DEX Scroll: Inter...", 1, 49999),
        ("Arrows for Bows", 1000, 800),
    ]


class FakeSource:
    ITEMS = {
        95: Item(95, "Earring DEX Scroll: Intermediate", 1),
        96: Item(96, "Earring DEX Scroll: Lesser", 1),
        97: Item(97, "Earring DEX Scroll: Greater", 2),
        355: Item(355, "Pig's Head", 7),
    }

    def __init__(self):
        self.calls = 0

    def search(self, q):
        self.calls += 1
        return [(i.id, i.name) for i in self.ITEMS.values()]

    def item(self, item_id):
        self.calls += 1
        info = {"type": "Use", "subtype": "Scroll", "levelReq": 0, "reqJob": None,
                "stats": {"successRate": 60, "watk": 2, "price": 1}, "iconKey": str(item_id)}
        return self.ITEMS[item_id], info

    def market(self, item_id):
        self.calls += 1
        return Market(21500, 3, sold=1, for_sale=2, cheapest_sell=20000)

    def icon_png(self, icon_key):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (30, 29), (191, 113, 71, 255)).save(buf, "PNG")
        return buf.getvalue()



def _here():
    from mprice_decoder.settings import Settings
    return Settings(channel=5, fm_room=2)  # a known location, so scans aren't blocked


HERE = _here()


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def make_db(tmp_path, clock=None):
    return ItemDB(tmp_path / "t.sqlite", source=FakeSource(), clock=clock or Clock())


def names(match):
    return [i.name for i in match.items]


def test_lookup_truncated_and_cached(tmp_path):
    db = make_db(tmp_path)
    assert names(db.lookup("Earring DEX Scroll: Inter...")) == ["Earring DEX Scroll: Intermediate"]
    calls = db.source.calls
    db.lookup("Earring DEX Scroll: Inter...")
    assert db.source.calls == calls  # served from SQLite


def test_lookup_cut_off_name_returns_every_version(tmp_path):
    db = make_db(tmp_path)
    assert names(db.lookup("Earring DEX Scroll: ...")) == [
        "Earring DEX Scroll: Greater", "Earring DEX Scroll: Intermediate", "Earring DEX Scroll: Lesser"]
    # OCR dropped the "..." but the text is still a prefix
    assert names(db.lookup("Earring DEX Scroll: Les")) == ["Earring DEX Scroll: Lesser"]


def test_lookup_ocr_typo_and_miss(tmp_path):
    db = make_db(tmp_path)
    assert names(db.lookup("Pig's Haed")) == ["Pig's Head"]
    assert not db.lookup("Totally Unknown Thing").found


def test_market_ttl(tmp_path):
    clock = Clock()
    db = make_db(tmp_path, clock)
    db.market(95)
    calls = db.source.calls
    db.market(95)
    assert db.source.calls == calls
    clock.t += TTL + 1
    db.market(95)
    assert db.source.calls == calls + 1


def test_seen_low_high_and_ttl(tmp_path):
    clock = Clock()
    db = make_db(tmp_path, clock)
    for price in (20000, 25000, 20000):  # repeat scans of one price count once
        db.record_seen(95, price)
    s = db.seen(95)
    assert (s.low, s.high, s.count) == (20000, 25000, 2)
    clock.t += TTL + 1
    assert db.seen(95) is None


def _listing(name, qty, price):
    return parse_listings([Line(name, 468, 339, 80, 12), Line(f"{qty} for {price} mesos", 468, 356, 80, 12)])[0]


def _label(db, listing):
    match = db.lookup(listing.name)
    versions = [Version(i, db.market(i.id)) for i in match.items]
    seen = None
    if len(match.items) == 1:
        db.record_seen(match.items[0].id, listing.price / listing.qty)
        seen = db.seen(match.items[0].id)
    return make_label(listing, match, versions, seen)


def test_profit_label(tmp_path):
    label = _label(make_db(tmp_path), _listing("Pig's Head", 5, 20))
    assert label.lines[0][0] == ("NPC 7 ea = 35", PROFIT)
    assert label.text == "NPC 7 ea = 35 (+15)"                    # no FM price: that's on the card
    pricey = _label(make_db(tmp_path), _listing("Pig's Head", 5, 100))
    assert pricey.text == "24h 4-20  HIGHEST" and "NPC" not in pricey.text  # NPC pays less: no NPC price


def test_lowest_and_highest_flags(tmp_path):
    db = make_db(tmp_path)
    first = _label(db, _listing("Earring DEX Scroll: Inter...", 1, 20000))
    assert "LOWEST" not in first.text and "HIGHEST" not in first.text  # nothing to compare yet
    high = _label(db, _listing("Earring DEX Scroll: Inter...", 1, 30000))
    assert high.lines[0][-1] == ("  HIGHEST", HIGHEST) and "24h 20k-30k" in high.text
    low = _label(db, _listing("Earring DEX Scroll: Inter...", 1, 15000))
    assert low.lines[0][-1] == ("  LOWEST", LOWEST)


def test_cut_off_name_gets_no_label(tmp_path):
    """Couldn't tell which tier it is: the card lists the candidates, the label stays out of the way."""
    label = _label(make_db(tmp_path), _listing("Earring DEX Scroll: ...", 1, 20000))
    assert label.lines == []


def test_icon_colour_picks_scroll_tier():
    from PIL import Image
    from mprice_decoder.icons import feature, pick
    solid = lambda rgb: feature(Image.new("RGBA", (30, 29), rgb + (255,)))
    lesser, inter, greater, chaos = (solid(c) for c in [(116, 142, 141), (191, 113, 71), (207, 160, 65), (131, 117, 113)])
    assert pick(solid((196, 156, 73)), [chaos, greater, inter, lesser]) == 1   # yellow shop icon
    assert pick(solid((182, 115, 79)), [chaos, greater, inter, lesser]) == 2   # red
    assert pick(solid((60, 200, 60)), [chaos, greater, inter, lesser]) is None  # nothing close
    assert pick(None, [chaos, greater]) is None


def test_merge_attempts_fills_missing_rows_and_keeps_all_spellings():
    from mprice_decoder.shop import merge_attempts
    a1 = [_listing("Pig's Haed", 1, 500)]                       # row 1 misread, row 2 missed
    a2 = [_listing("Pig's Head", 1, 500),
          parse_listings([Line("Arrows for Bows", 467, 379, 80, 12),
                          Line("1000 for 800 mesos", 468, 396, 80, 12)])[0]]
    a3 = [_listing("Pig's Head", 1, 500)]
    rows = merge_attempts([a1, a2, a3])
    assert [(r.names, r.qty, r.price) for r in rows] == [
        (["Pig's Head", "Pig's Haed"], 1, 500),
        (["Arrows for Bows"], 1000, 800),
    ]


def test_scan_retries_ocr_then_tries_each_spelling(tmp_path, monkeypatch):
    import mprice_decoder.__main__ as app
    reads = iter([
        [Line("P1g5 H3@d", 468, 339, 80, 12), Line("1 for 500 mesos", 468, 356, 80, 12, score=0.8)],
        [Line("Pig's Head", 468, 339, 80, 12), Line("1 for 500 mesos", 468, 356, 80, 12)],
    ])
    calls = []
    monkeypatch.setattr(app, "read_lines", lambda img, attempt: calls.append(attempt) or next(reads))
    db = make_db(tmp_path)
    labels, panel, *_ = app.scan(lambda: None, db, HERE)
    assert panel.cards[0].title == "Pig's Head"
    assert calls == [0, 1]   # first price read wasn't confident, so it read again
    assert labels[0].lines == []  # 500 > NPC 7 and no range yet: nothing to say beside the row


def test_tiered_item_lists_other_tiers_on_its_card(tmp_path):
    from mprice_decoder.annotate import make_card
    db = make_db(tmp_path)
    listing = _listing("Earring DEX Scroll: Inter...", 1, 20000)
    match = db.lookup(listing.name)
    others = [i for i in db.lookup("Earring DEX Scroll: ...").items if i.id != match.items[0].id]
    versions = [Version(match.items[0], Market(None, 0))]
    siblings = [Version(i, Market(5000, 2) if i.id == 96 else None) for i in others]
    assert make_label(listing, match, versions, siblings=siblings).lines == []  # nothing to act on
    card = make_card(listing, match, versions, siblings=siblings)
    assert ["".join(t for t, _ in line) for line in card.extra] == ["Other tiers  Lesser ~5k   Greater -"]


def test_single_misread_price_never_reaches_history(tmp_path, monkeypatch):
    import mprice_decoder.__main__ as app
    row = lambda price: [Line("Pig's Head", 468, 339, 80, 12), Line(f"1 for {price} mesos", 468, 356, 80, 12)]
    db = make_db(tmp_path)
    shaky = lambda price: [l if "for" not in l.text else Line(l.text, l.x, l.y, l.w, l.h, score=0.7) for l in row(price)]
    reads = iter([shaky("11,499"), []])  # one shaky read, and the retry missed the row
    monkeypatch.setattr(app, "read_lines", lambda img, attempt: next(reads))
    app.scan(lambda: None, db, HERE)
    assert db.seen(355) is None
    reads = iter([row("1,499")])  # one confident read is enough
    app.scan(lambda: None, db, HERE)
    assert db.seen(355).high == 1499


def test_name_with_icon_junk_glued_on_is_kept():
    lines = [
        Line("O Slime Bubble", 432, 352, 120, 12, words=[("O", 432), ("Slime", 468), ("Bubble", 510)]),
        Line("1 for 399 mesos", 468, 370, 90, 10),
        Line("D Leather", 434, 392, 80, 10),  # no word positions: fall back to dropping short tokens
        Line("1 for 799 mesos", 468, 410, 90, 10),
    ]
    assert [(l.name, l.x) for l in parse_listings(lines)] == [("Slime Bubble", 468), ("Leather", 468)]


def test_cut_off_price_with_lookalike_letters():
    lines = [Line("Claw Attack Scroll: Interm...", 468, 432, 150, 12), Line("1 for gg,ggg me...", 468, 448, 110, 10),
             Line("Meshcap", 468, 392, 60, 12), Line("1 for 6,767 mesos", 468, 408, 100, 10),
             Line("Arrows for Crossbows", 468, 312, 120, 12)]  # a name, not a price line
    got = [(l.name, l.qty, l.price, l.uncertain) for l in parse_listings(lines)]
    assert got == [("Meshcap", 1, 6767, False), ("Claw Attack Scroll: Interm...", 1, 99999, True)]


def test_uncertain_price_is_never_confirmed():
    from mprice_decoder.shop import merge_attempts
    claw = parse_listings([Line("Claw Attack Scroll: Interm...", 468, 432, 150, 12),
                           Line("1 for gg,ggg me...", 468, 448, 110, 10)])
    row, = merge_attempts([claw, claw, claw])
    assert row.uncertain and not row.confirmed


def test_unreadable_price_gets_no_label(tmp_path):
    lines = [Line("Claw Attack Scroll: Interm...", 468, 432, 150, 12), Line("1 for me...", 468, 448, 60, 10)]
    listing, = parse_listings(lines)
    assert (listing.qty, listing.price, listing.uncertain) == (1, None, True)
    db = make_db(tmp_path)
    match = db.lookup("Earring DEX Scroll: Inter...")
    label = make_label(listing, match, [Version(match.items[0], Market(21500, 3))])
    assert label.lines == []  # can't tell if it's under the NPC price


def test_labels_never_show_fm_prices(tmp_path):
    db = make_db(tmp_path)  # fake FM usual is 21,500: 15k is a deal, shown on the card instead
    _label(db, _listing("Earring DEX Scroll: Inter...", 1, 20000))
    deal = _label(db, _listing("Earring DEX Scroll: Inter...", 1, 15000))
    assert deal.text == "24h 15k-20k  LOWEST" and "FM" not in deal.text


def _card(db, listing, seen=None):
    from mprice_decoder.annotate import make_card
    match = db.lookup(listing.name)
    versions = [Version(i, db.market(i.id)) for i in match.items]
    info = db.info(match.items[0].id) if len(match.items) == 1 else None
    return make_card(listing, match, versions, seen, info)


def test_card_for_a_deal(tmp_path):
    from mprice_decoder.annotate import DEAL
    card = _card(make_db(tmp_path), _listing("Earring DEX Scroll: Inter...", 1, 15000))
    assert card.title == "Earring DEX Scroll: Intermediate"
    assert card.subtitle == "60% success  \u00b7  W.ATK +2"
    assert "".join(t for t, _ in card.price) == "Asking 15k    FM ~21.5k (3)    NPC 1"
    assert [p.text for p in card.pills] == ["DEAL  30% UNDER FM"] and card.accent == DEAL


def test_card_verdicts(tmp_path):
    db = make_db(tmp_path)  # FM ~21,500, cheapest on MeowDB 20,000
    texts = lambda price: [p.text for p in _card(db, _listing("Earring DEX Scroll: Inter...", 1, price)).pills]
    assert texts(21000) == ["FAIR PRICE", "MEOWDB HAS ONE FOR 20k"]
    assert texts(30000) == ["40% OVER FM", "MEOWDB HAS ONE FOR 20k"]
    assert texts(50000) == ["2.3x FM PRICE", "MEOWDB HAS ONE FOR 20k"]


def test_card_for_unclear_price_and_cut_off_name(tmp_path):
    db = make_db(tmp_path)
    unclear, = parse_listings([Line("Earring DEX Scroll: Inter...", 468, 432, 150, 12), Line("1 for me...", 468, 448, 60, 10)])
    assert [p.text for p in _card(db, unclear).pills] == ["PRICE UNCLEAR"]
    cut = _card(db, _listing("Earring DEX Scroll: ...", 1, 20000))
    assert cut.subtitle.startswith("Name is cut off") and len(cut.extra) == 3
    assert ["".join(t for t, _ in line) for line in cut.extra][0] == "Greater    FM ~21.5k (3)"


def test_panel_renders(tmp_path):
    from PIL import Image
    from mprice_decoder.annotate import Panel
    from mprice_decoder.render import PilPainter, draw_panel
    db = make_db(tmp_path)
    card = _card(db, _listing("Earring DEX Scroll: Inter...", 1, 15000))
    card.icon = db.icon_png(95)
    img = Image.new("RGB", (800, 600), "white")
    panel = Panel(left=500, top=300, right=10_000, bottom=560, cards=[card] * 3)
    draw_panel(PilPainter(img), panel)
    assert img.getpixel((798, 400)) == (255, 255, 255) and img.getpixel((780, 400)) != (255, 255, 255)  # stays on-screen
    assert img.getpixel((450, 400)) == (255, 255, 255)  # never left of its box (over the shop)
    tall = Image.new("RGB", (800, 600), "white")
    draw_panel(PilPainter(tall), Panel(left=500, top=300, right=10_000, bottom=560, cards=[card] * 12))
    assert tall.getpixel((600, 570)) == (255, 255, 255)  # 12 cards still end above the box's bottom


def test_identical_listings_share_a_card(tmp_path, monkeypatch):
    import mprice_decoder.__main__ as app
    arrows = lambda y: [Line("Pig's Head", 468, y, 80, 12), Line("1000 for 800 mesos", 468, y + 17, 80, 12)]
    monkeypatch.setattr(app, "read_lines", lambda img, attempt: arrows(339) + arrows(379))
    labels, panel, *_ = app.scan(lambda: None, make_db(tmp_path), HERE)
    assert len(labels) == 2 and len(panel.cards) == 1 and panel.cards[0].count == 2


def test_junk_meowdb_listing_is_ignored(tmp_path):
    db = make_db(tmp_path)
    db.source.market = lambda item_id: Market(1200, 4, cheapest_sell=12)
    assert [p.text for p in _card(db, _listing("Pig's Head", 1, 500)).pills] == ["DEAL  58% UNDER FM"]


def test_scan_log_skips_rescans_of_the_same_shop(tmp_path):
    clock = Clock()
    db = make_db(tmp_path, clock)
    pig = [(355, "Pig's Head", 1, 500, True, b"png")]
    db.record_scan("pighead 500", "LuckyGandalf", pig)
    clock.t += 60
    db.record_scan("pighead 500", "LuckyGandalf", pig)          # F8 again: not logged twice
    db.record_scan("other shop", "Someone", pig)                # another shop: logged
    clock.t += db.REPEAT_WINDOW + 1
    db.record_scan("pighead 500", "LuckyGandalf", pig)          # much later: logged again
    rows = db.conn.execute("SELECT c.seller, s.unit_price FROM sightings s JOIN scans c ON c.id=s.scan_id").fetchall()
    assert rows == [("LuckyGandalf", 500.0), ("Someone", 500.0), ("LuckyGandalf", 500.0)]
    assert db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 4
    assert db.conn.execute("SELECT png FROM shop_icons WHERE item_id=355").fetchone()[0] == b"png"


def test_a_shop_put_up_again_on_another_channel_is_logged_again(tmp_path):
    clock = Clock()
    db = make_db(tmp_path, clock)
    pig = [(355, "Pig's Head", 1, 500, True, b"png")]
    db.record_scan("pighead 500", "LuckyGandalf", pig, channel=4, fm_room=2)
    clock.t += 60
    db.record_scan("pighead 500", "LuckyGandalf", pig, channel=4, fm_room=2)    # same spot: not logged twice
    db.record_scan("pighead 500", "LuckyGandalf", pig)                          # channel unknown: still a repeat
    clock.t += 60
    db.record_scan("pighead 500", "LuckyGandalf", pig, channel=16, fm_room=2)   # dc'd, back on ch 16: logged
    db.record_scan("pighead 500", "LuckyGandalf", pig, channel=16, fm_room=5)   # another room: logged
    db.record_scan("pighead 500", "LuckyGandalf", pig, channel=16, fm_room=5)   # and not again
    rows = db.conn.execute("SELECT c.channel, c.fm_room FROM sightings s JOIN scans c ON c.id=s.scan_id").fetchall()
    assert rows == [(4, 2), (16, 2), (16, 5)]


def test_old_prices_are_imported_once(tmp_path):
    db = make_db(tmp_path)
    db.record_seen(355, 450)
    db._import_old_sightings()  # already imported at startup (empty then): no-op
    db2 = ItemDB(tmp_path / "t.sqlite", source=FakeSource(), clock=Clock())
    assert db2.conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0  # flag set on first open


def test_fm_room_from_map_name():
    from mprice_decoder.ocr import clean_map_name, fm_room_of
    assert fm_room_of("FreeMarket<2") == 2 and fm_room_of("Free Market <8>") == 8
    assert fm_room_of("Free Market Entrance") is None and fm_room_of("Henesys Market") is None
    assert fm_room_of(None) is None
    assert clean_map_name("Free Market<8>") == clean_map_name(" Free  Market < 8>") == "Free Market <8>"
    assert clean_map_name("FreeMarket <2>") == "Free Market <2>" and clean_map_name("Henesys  Market") == "Henesys Market"


def test_known_map():
    """A minimap read is one of the game's maps or nothing (real reads from Cozy Markets' scans): the FM room only by a
    number read, a shop sign on the end cut off, a name cut short or a letter or two off only when one map fits."""
    from mprice_decoder.ocr import known_map
    assert known_map("Free Market <2>") == "Free Market <2>" and known_map("Sunset Sky") == "Sunset Sky"
    assert known_map("FeeMarket <3>") == known_map("TeeMarket <3") == known_map("larket <3>") == "Free Market <3>"
    assert known_map("Sumset Sky") == "Sunset Sky" and known_map("Perion Street Comer") == "Perion Street Corner"
    assert known_map("Kerning CityConstructionSite") == "Kerning City Construction Site"
    assert known_map("Kerning City Construction Site BlackKitty") == "Kerning City Construction Site"  # a shop sign
    assert known_map("Kerning City Const") == "Kerning City Construction Site"  # cut short
    assert known_map("Henesys Humting Ground!") == "Henesys Hunting Ground I"
    for junk in ("FT", "SW", "Kn", "172", "Hidden Street", "ree Market", "TeeMarket", "FreeMa", "Free Market <25>",
                 "Henesys Hunting Ground", "Ant Tunnel", "HenesysHumting Ground", None, ""):
        assert known_map(junk) is None, junk  # gibberish, an FM room or a series' number not read: can't tell


def test_map_name_from_minimap():
    """Only text on the minimap's title panel counts: not shop signs, the window
    title or our own overlay, and nothing at all when the minimap is collapsed."""
    from pathlib import Path
    from PIL import Image
    from mprice_decoder.ocr import read_map
    fixtures = Path(__file__).parent / "fixtures"
    assert read_map(Image.open(fixtures / "fm2_window.png")) == "Free Market <2>"
    assert read_map(Image.open(fixtures / "shop8_cclaw.png")) == "Free Market <8>"  # under our status text
    assert read_map(Image.open(fixtures / "shop1.png")) is None        # collapsed minimap
    assert read_map(Image.open(fixtures / "shop7_dutch.png")) is None  # a two-line shop sign


def test_settings_roundtrip(tmp_path):
    from mprice_decoder.settings import Settings
    path = tmp_path / "settings.json"
    assert Settings.load(path) == Settings()          # no file yet: defaults
    Settings(channel=5, fm_room=None, show_labels=False).save(path)
    assert Settings.load(path).channel == 5 and Settings.load(path).show_labels is False
    path.write_text("not json")
    assert Settings.load(path) == Settings()          # broken file: defaults, no crash
    path.write_text('{"channel": 5, "show_panel": true, "open_dashboard": true}')  # saved by an older version
    assert Settings.load(path).show_panel is True and not hasattr(Settings.load(path), "open_dashboard")
    assert Settings.load(path).share_scans is True     # sharing is opt-out


def test_hotkeys_can_be_rebound(tmp_path):
    from mprice_decoder.overlay import OptionsWindow
    from mprice_decoder.settings import Settings, hotkey_name
    assert [hotkey_name(k) for k in ("F8", "f12", "a", "7", "F25", "Shift_L", "Escape")] == [
        "F8", "F12", "A", "7", None, None, None]
    s, saved = Settings(), []
    win = OptionsWindow(None, s, lambda: saved.append(1), lambda: None, lambda: None)  # no Tk needed
    win.set_key("scan_key", "Q")
    assert (s.scan_key, s.clear_key, s.options_key) == ("Q", "F9", "F10") and saved
    win.set_key("clear_key", "Q")                     # taken by scan: they swap
    assert (s.scan_key, s.clear_key) == ("F9", "Q")
    path = tmp_path / "settings.json"
    s.save(path)
    assert Settings.load(path).clear_key == "Q"
    path.write_text('{"scan_key": "Shift"}')          # can't poll that: default
    assert Settings.load(path).scan_key == "F8" and Settings.load(path).show_options is True


def test_location_needs_the_minimap(monkeypatch):
    import mprice_decoder.__main__ as app
    from mprice_decoder.settings import Settings
    monkeypatch.setattr(app, "last_map", None)
    s = Settings(channel=5)
    assert app.where(s, "Free Market <2>") == (5, 2, "Free Market <2>")
    assert app.where(s, None) == (5, None, None)      # minimap hidden: you may have moved, so unknown
    assert app.where(s, "Henesys Market") == (5, None, "Henesys Market")  # left the FM: no room
    s.fm_room = 7
    assert app.where(s, "Free Market <3>") == (5, 7, "Free Market <7>")  # manual room wins
    assert app.where_text(5, 2, "Free Market <2>") == "Ch 5 · FM 2"
    assert app.where_text(5, None, "Henesys Market") == "Ch 5 · Henesys Market"
    assert app.where_text(None, None) == "Ch ? · Map ?"


def test_channel_from_the_change_channel_window():
    """A VPN or ping reducer hides the channel's port: the Change Channel window shows it."""
    import numpy as np
    from mprice_decoder.channel import ScreenChannel, is_loading, read_channel_window
    fixtures = Path(__file__).parent / "fixtures"
    frame = lambda name: np.array(Image.open(fixtures / name).convert("RGB"))
    win = lambda name: (lambda w: w and (w["current"], w["picked"], w["count"]))(read_channel_window(frame(name)))
    assert win("channel_open_ch7.png") == (None, 7, 20)    # just opened: yours is blue
    assert win("channel_pick_ch9.png") == (7, 9, 20)       # 9 picked: yours is dark
    assert win("channel_pick_ch6.png") == (7, 6, 20)
    assert win("channel_hover_ch14.png") == (7, 6, 20)     # the mouse over 14 isn't a pick
    assert win("shop1.png") is None and win("fm2_window.png") is None
    black = np.zeros((480, 640, 3), np.uint8)
    assert is_loading(black) and not is_loading(frame("shop1.png"))

    seen = ScreenChannel()
    assert seen.follow(frame("shop1.png"), 0) is None
    assert seen.follow(frame("channel_open_ch7.png"), 1) == 7
    assert seen.follow(frame("channel_pick_ch9.png"), 2) == 7     # picked, not gone yet
    assert seen.follow(frame("shop1.png"), 3) == 7                # closed: still 7 until a loading screen
    assert seen.follow(black, 4) == 9                             # it loaded: you're on 9
    assert seen.follow(frame("channel_pick_ch6.png"), 10) == 7    # the window says 7 (say, cancelled before)
    seen.follow(frame("shop1.png"), 11)                           # closed with 6 picked...
    assert seen.follow(frame("shop1.png"), 30) == 7               # ...but no loading screen: stayed on 7
    assert seen.follow(black, 31) == 7                            # a later loading screen is a map change


def test_channel_prefers_the_connection_then_the_screen(monkeypatch):
    import mprice_decoder.__main__ as app
    from types import SimpleNamespace
    monkeypatch.setattr(app, "screen_channel", SimpleNamespace(channel=9))
    monkeypatch.setattr(app, "read_channel", lambda: 4)
    assert app.detect_channel() == 4
    monkeypatch.setattr(app, "read_channel", lambda: None)
    assert app.detect_channel() == 9
    monkeypatch.setattr(app, "screen_channel", SimpleNamespace(channel=None))
    assert app.detect_channel() is None

def test_channel_from_the_game_connection(monkeypatch):
    import mprice_decoder.__main__ as app
    from mprice_decoder.channel import channel_from_ports
    from mprice_decoder.settings import Settings
    assert channel_from_ports([8542, 8913, 443]) == 3   # chat (8913) and https are other servers
    assert channel_from_ports([8913, 443]) is None      # mid channel change / cash shop
    assert channel_from_ports([8542, 8543]) is None     # ambiguous: don't guess
    monkeypatch.setattr(app, "last_channel", None)
    s = Settings()
    assert app.where(s, None, 4)[0] == 4
    assert app.where(s, None, None)[0] == 4             # connection dropped: keep the last channel
    s.channel = 7
    assert app.where(s, None, 4)[0] == 7                # manual channel wins


def test_own_shop_is_found_and_no_shop_means_no_ocr():
    """Your own shop has OPEN/CLOSE STORE instead of BUY ITEM/LEAVE STORE. And with
    no shop at all, nothing is OCR'd (the whole screen took 5-15s)."""
    from pathlib import Path
    import numpy as np
    from PIL import Image
    from mprice_decoder import ocr
    fixtures = Path(__file__).parent / "fixtures"
    rgb = lambda name: np.array(Image.open(fixtures / name).convert("RGB"))
    assert ocr.locate_buttons(rgb("shop8_cclaw.png")) == (560, 278)   # someone else's: unchanged
    assert ocr.locate_buttons(rgb("shop10_own.png")) == (514, 183)    # yours: same layout around it
    assert ocr.read_header(Image.open(fixtures / "shop10_own.png")) == ("scrolls and ores", "Cheng")
    # Yours in a 1920x1080 window, as F8 sees it (brought back to 1366x768): CLOSE STORE blurs to ~0.90
    assert ocr.locate_buttons(rgb("shop11_own_1920.png")) == (605, 182)
    # Yours is now tried first: someone else's BUY ITEM half-matches CLOSE STORE but has LEAVE STORE under it
    assert ocr.locate_buttons(rgb("shop8_cclaw.png")) == (560, 278)
    assert ocr.locate_buttons(rgb("shop1.png")) == (562, 272)



def test_reads_your_name_off_the_status_bar():
    from pathlib import Path
    from PIL import Image
    from mprice_decoder import ocr
    fixtures = Path(__file__).parent / "fixtures"
    assert ocr.read_player(Image.open(fixtures / "shop10_own.png")) == "Cheng"
    assert ocr.read_player(Image.open(fixtures / "shop6_claw_live.png")) == "Cheng"  # in someone else's shop
    assert ocr.read_player(Image.open(fixtures / "shop1.png")) == "Cheng"            # window a different size
    assert ocr.read_player(Image.open(fixtures / "shop2.png")) is None                # status bar not in the shot
    assert ocr.read_player(Image.new("RGB", (1366, 768), "#3a6ea5")) is None
    assert ocr.read_lines(Image.new("RGB", (1366, 768), "#3a6ea5")) is None


def test_no_shop_window_stops_the_scan(tmp_path, monkeypatch):
    import mprice_decoder.__main__ as app
    tries = []
    monkeypatch.setattr(app, "read_lines", lambda img, attempt: tries.append(attempt))  # None: no shop
    db = make_db(tmp_path)
    assert app.scan(lambda: None, db, HERE)[3] == "No shop window found"
    assert tries == [0]                                                  # no upscaled retry
    assert db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0


def test_no_log_without_a_location(tmp_path, monkeypatch):
    """Minimap hidden or channel unknown (a VPN hides its port): the shop is still priced, but nothing is logged."""
    import mprice_decoder.__main__ as app
    from mprice_decoder.settings import Settings
    import time
    monkeypatch.setattr(app, "read_lines", lambda img, attempt: [
        Line("Pig's Head", 468, 339, 80, 12), Line("1 for 500 mesos", 468, 356, 80, 12)])
    monkeypatch.setattr(app, "read_location", lambda grab: ("pigs", "PigGuy", "Cheng"))
    monkeypatch.setattr(app, "read_channel", lambda: 5)
    monkeypatch.setattr(app, "read_map", lambda grab: None)
    db = make_db(tmp_path)
    labels, panel, place, blocked = app.scan(lambda: None, db, Settings())
    assert len(labels) == 1 and panel.cards[0].title == "Pig's Head" and blocked is None
    assert place == "Ch 5 · Map ? · not saved: Please expand the minimap"
    monkeypatch.setattr(app, "read_map", lambda grab: "Henesys Market")
    monkeypatch.setattr(app, "read_channel", lambda: None)
    monkeypatch.setattr(app, "last_channel", None)
    assert app.scan(lambda: None, db, Settings())[2] == "Ch ? · Henesys Market · not saved: Channel unknown: open Change Channel once"
    time.sleep(0.2)  # a log would come once the title and seller are read
    assert db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
    monkeypatch.setattr(app, "read_channel", lambda: 5)
    monkeypatch.setattr(app, "read_map", lambda grab: "Henesys Market")
    labels, _, _, blocked = app.scan(lambda: None, db, Settings())
    assert len(labels) == 1 and blocked is None
    deadline = time.monotonic() + 5  # logged once the title and seller are read: after the prices are drawn
    while not db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert db.conn.execute("SELECT channel, map_name, scanned_by FROM scans").fetchall() == [
        (5, "Henesys Market", "Cheng")]
    assert app.blocked_reason(None, None, "Henesys Market") == "Channel unknown: open Change Channel once"
    assert app.blocked_reason(5, 7, "Free Market <7>") is None  # manual FM room counts as known
    monkeypatch.setattr(app, "read_channel", lambda: 5)
    monkeypatch.setattr(app, "read_map", lambda grab: "FT")  # a minimap read that's no map: not saved, and said why
    assert app.scan(lambda: None, db, Settings())[2] == "Ch 5 · Map ? · not saved: Can't read the map"
    monkeypatch.setattr(app, "read_map", lambda grab: "FeeMarket <3>")  # a misread, but surely FM 3: saved as that
    assert app.scan(lambda: None, db, Settings())[2] == "Ch 5 · FM 3"
    deadline = time.monotonic() + 5
    while db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert db.conn.execute("SELECT channel, fm_room, map_name FROM scans").fetchall() == [
        (5, None, "Henesys Market"), (5, 3, "Free Market <3>")]


def test_scan_location_is_saved(tmp_path):
    db = make_db(tmp_path)
    db.record_scan("ores and etc", "BoredClam", [(355, "Pig's Head", 1, 500, True, None)], channel=5, fm_room=2,
                   map_name="Free Market <2>")
    db.record_scan("pots", "Henny", [(355, "Pig's Head", 1, 600, True, None)], channel=5, map_name="Henesys Market")
    assert db.conn.execute("SELECT shop_title, seller, channel, fm_room, map_name FROM scans").fetchall() == [
        ("ores and etc", "BoredClam", 5, 2, "Free Market <2>"), ("pots", "Henny", 5, None, "Henesys Market")]


def test_sold_rows_are_greyed_out():
    """Row positions as the OCR finds them in the real screenshots."""
    from pathlib import Path
    from PIL import Image
    from mprice_decoder.shop import Listing, is_sold
    fixtures = Path(__file__).parent / "fixtures"

    def sold(png, rows):
        img = Image.open(fixtures / png)
        return [is_sold(img, Listing("", 1, 1, x, y, bottom)) for x, y, bottom in rows]
    # shop2: Iron Ore greyed out, the rest still for sale
    assert sold("shop2.png", [(336, 60, 95), (335, 101, 136), (334, 140, 176), (334, 223, 255), (337, 263, 294)]) == \
        [False, False, False, False, True]
    assert sold("shop9_sold.png", [(466, 311, 346), (467, 350, 385), (470, 390, 426), (463, 429, 466)]) == [True] * 4
    assert not any(sold("shop1.png", [(465, 334, 371), (464, 377, 412), (466, 417, 451), (465, 498, 530)]))


def test_sold_label_has_no_flags(tmp_path):
    from mprice_decoder.annotate import SOLD
    db = make_db(tmp_path)
    listing = _listing("Earring DEX Scroll: Inter...", 1, 15000)  # would be a -30% FM deal
    listing.sold = True
    label = _label(db, listing)
    assert label.lines == [[("SOLD", SOLD)]]
    sold_cheap = _listing("Pig's Head", 5, 20)                     # under NPC, but gone
    sold_cheap.sold = True
    assert _label(db, sold_cheap).text == "SOLD"


def test_scan_log_records_a_sale_once(tmp_path):
    clock = Clock()
    db = make_db(tmp_path, clock)
    pig = (355, "Pig's Head", 1, 500, True, None)
    db.record_scan("pighead 500", "LuckyGandalf", [pig])
    clock.t += 60
    db.record_scan("pighead 500", "LuckyGandalf", [pig + (True,)])   # it sold since: logged as a sale
    clock.t += 60
    db.record_scan("pighead 500", "LuckyGandalf", [pig + (True,)])   # still sold: not again
    assert db.conn.execute("SELECT sold FROM sightings ORDER BY id").fetchall() == [(0,), (1,)]


def _tooltip(name):
    """A tooltip fixture and the cursor position saved in its file name."""
    from pathlib import Path
    from PIL import Image
    path = next((Path(__file__).parent / "fixtures").glob(f"tooltip_{name}_*.png"))
    x, y = path.stem.rsplit("_", 2)[1:]
    return Image.open(path), (int(x), int(y))


def test_tooltip_name_is_read_next_to_its_dot():
    """The name, not the meso count showing through the box; also when the
    tooltip is pushed left at the window's edge, and in NPC shops."""
    from mprice_decoder.ocr import TOOLTIP_DOT_DY, read_tooltip
    for name, want in [("evil_eye_tail", "Evil Eye Tail"), ("stirge_wing", "Stirge Wing"),
                       ("npc_shop", "Sapphire Ore"), ("scroll", "Cape Magic Def. Scroll: Lesser")]:
        img, cursor = _tooltip(name)
        names, dot = read_tooltip(img, cursor)
        assert want.replace(" ", "").lower() in [n.replace(" ", "").lower() for n in names], (name, names)
        assert dot[1] - cursor[1] == TOOLTIP_DOT_DY
    img, cursor = _tooltip("none")
    assert read_tooltip(img, cursor) is None


def test_hover_card_tries_each_reading(tmp_path):
    from mprice_decoder.__main__ import price_card
    db = make_db(tmp_path)
    card = price_card(db, ["zzzz", "Pig's Head"])
    assert card.title == "Pig's Head"
    text = "".join(t for t, _ in card.price)
    assert "FM ~21.5k" in text and "NPC 7" in text and "Asking" not in text
    assert "".join(t for t, _ in card.extra[0]) == "MeowDB  selling from 20k"


def test_hover_card_for_a_tier_and_a_miss(tmp_path):
    from mprice_decoder.__main__ import price_card
    db = make_db(tmp_path)
    card = price_card(db, ["Earring DEX Scroll: Lesser"])
    assert "Other tiers" in "".join(t for t, _ in card.extra[-1])
    assert price_card(db, ["zzzz"]).subtitle == "Not found on MeowDB"


def test_cheap_shop_check_agrees_with_the_full_one():
    from pathlib import Path
    import numpy as np
    from PIL import Image
    from mprice_decoder import ocr
    for path in sorted((Path(__file__).parent / "fixtures").glob("*.png")):
        rgb = np.array(Image.open(path).convert("RGB"))
        full = ocr.locate_buttons(rgb)
        assert ocr.find_shop(rgb) == full, path.name
        if full:  # and right where it was last time
            assert ocr.find_shop(rgb, near=(full[0] + 5, full[1] - 3)) == full, path.name


def _fixture_rgb(name):
    from pathlib import Path
    import numpy as np
    from PIL import Image
    return np.array(Image.open(Path(__file__).parent / "fixtures" / name).convert("RGB"))


def test_list_fingerprint_ignores_the_cursor_and_labels_but_not_the_list():
    from mprice_decoder import ocr
    rgb = _fixture_rgb("shop5_claw.png")
    anchor = ocr.locate_buttons(rgb)
    x0, y0, x1, y1 = ocr._region(rgb, anchor, ocr.LIST_BOX)
    base = ocr.list_fingerprint(rgb, anchor)
    moved = rgb.copy()
    moved[y0 + 50:y0 + 80, x0 + 100:x0 + 124] = 255  # the game's cursor, drawn over a row
    moved[y0:y1, x0:x0 + 30] = 0                     # a label over the icons
    cursor = (x0 + 110, y0 + 60)
    assert not ocr.list_changed(base, ocr.list_fingerprint(moved, anchor, cursor))
    scrolled = rgb.copy()
    scrolled[y0:y1 - 40] = rgb[y0 + 40:y1]           # one row up
    assert ocr.list_changed(base, ocr.list_fingerprint(scrolled, anchor))
    other = _fixture_rgb("shop9_sold.png")
    assert ocr.list_changed(base, ocr.list_fingerprint(other, ocr.locate_buttons(other)))


def test_shop_watch_debounces():
    import numpy as np
    from mprice_decoder.__main__ import ShopWatch
    w = ShopWatch()
    a, b = np.zeros((20, 20), np.float32), np.full((20, 20), 255, np.float32)
    seq = [(0.0, (1, 1), a), (0.2, (1, 1), b),   # opening: still changing
           (0.4, (1, 1), b),                     # same, but not for long enough
           (0.6, (1, 1), b),                     # settled: scan
           (0.8, (1, 1), b), (5.0, (1, 1), b),   # unchanged: never again
           (5.2, None, None), (5.4, (1, 1), b),  # one bad look isn't a close
           (6.0, (1, 1), a), (6.2, (1, 1), a), (6.4, (1, 1), a),  # scrolled: rescan once settled
           (7.0, None, None), (7.5, None, None), (7.9, None, None)]  # gone for good
    got = [w.update(t, anchor, fp) for t, anchor, fp in seq]
    assert got == [None, None, None, "scan", None, None, None, None, None, None, "scan", None, None, "closed"]
    assert w.update(8.2, (1, 1), a) is None and w.update(8.6, (1, 1), a) == "scan"  # reopened


def test_the_overlay_and_the_test_painter_take_the_same_calls():
    """draw_panel runs on TkPainter on screen and PilPainter in tests: a call only one of them
    takes would pass the tests and leave the overlay's panel empty."""
    import inspect
    from mprice_decoder.overlay import TkPainter
    from mprice_decoder.render import PilPainter
    for name, fn in inspect.getmembers(PilPainter, inspect.isfunction):
        if not name.startswith("_"):
            assert inspect.signature(getattr(TkPainter, name)) == inspect.signature(fn), name


import numpy as np  # noqa: E402  (the resolution tests below)
import pytest  # noqa: E402
from pathlib import Path  # noqa: E402
from PIL import Image  # noqa: E402
from mprice_decoder import ocr  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _game(name):
    """A resolution screenshot's game: its client area, without a window's border and title bar."""
    rgb = np.array(Image.open(FIXTURES / f"{name}.png").convert("RGB"))
    if name.startswith("res_window"):
        h, w = rgb.shape[:2]
        game_h = {"1024": 768, "1280": 720, "1366": 768, "1920": 1080}[name.split("_")[2]]
        rgb = rgb[h - 1 - game_h:h - 1, 1:w - 1]
    return rgb


@pytest.mark.parametrize("name, scale", [("res_window_1024", 1), ("res_window_1280", 1), ("res_window_1366", 1),
                                         ("res_window_1920", 1080 / 768), ("res_fullscreen_1920_shop", 1080 / 768),
                                         ("res_fullscreen_1366", 1440 / 768), ("res_fullscreen_1280", 2)])
def test_how_much_bigger_the_game_is_drawn_is_told_by_its_status_bar(name, scale):
    ocr._scales.clear()
    assert ocr.game_scale(_game(name)) == pytest.approx(scale)


@pytest.mark.parametrize("name", ["res_window_1366_shop", "res_window_1920_shop", "res_fullscreen_1920_shop"])
def test_a_shop_reads_the_same_in_a_1080p_window_or_full_screen(name):
    """1920x1080 draws the game's 1366x768 1.40625x as big: brought back to that, the shop's found where it is
    at 1366x768 and its rows read."""
    ocr._scales.clear()
    ocr._last_anchor = None
    img, scale = ocr.game_view(Image.fromarray(_game(name)))
    assert img.size[1] == 768 and scale == pytest.approx(1 if "1366" in name else 1080 / 768)
    x, y = ocr.locate_buttons(np.array(img))
    assert abs(x - 562) <= 2 and abs(y - 245) <= 2
    listings = parse_listings(ocr.read_lines(img))
    assert [(l.qty, l.price) for l in listings] == [(1, 500), (1, 500), (1, 999999)]
    assert [l.name for l in listings][:2] == ["Salad", "Salad"]


def test_labels_and_cards_go_where_the_bigger_game_shows_the_shop():
    from mprice_decoder.annotate import Label, Panel, hover_panel, scale_up
    labels, panel = scale_up([Label(100, 200)], Panel(600, 100, 10_000, 500), 1.5)
    assert (labels[0].right, labels[0].center_y) == (150, 300)
    assert (panel.left, panel.top, panel.right, panel.bottom) == (900, 150, 10_000, 750)
    assert scale_up(labels, panel, 1) == (labels, panel)
    assert hover_panel(None, (100, 300), 1.5).left == 100 + 450          # right of a 1.5x tooltip


def test_a_half_redrawn_frame_isnt_trusted_for_long():
    """Changing resolution, one frame matched 1.5x at 0.91 (1.40625x was right): it's looked at again soon."""
    ocr._scales.clear()
    now = [0.0]
    good = _game("res_window_1920")
    odd = good.copy()
    odd[-40:] = 0  # no status bar to tell by: a guess
    assert ocr.game_scale(odd, clock=lambda: now[0]) == pytest.approx(1080 / 768)   # from the height
    now[0] += ocr.SCALE_UNSURE
    assert ocr.game_scale(good, clock=lambda: now[0]) == pytest.approx(1080 / 768)
    assert ocr._scales[(1920, 1080)][2] == ocr.SCALE_EVERY


def test_an_enhanced_equips_name_matches_without_its_upgrade_count(tmp_path):
    """An enhanced equip's tooltip reads "Korean Fan (+7)" (2026-10-21: "Not found on MeowDB"): any +1 to +99, however
    OCR spaces it, is left off, wherever a name's read (tooltips, shop rows)."""
    from mprice_decoder.data import strip_upgrades
    for name in ("Korean Fan", "Brown Legolia Pants", "Red Shadow (F)", "Blue Knucklevest Pants (M)"):
        for level in (1, 7, 10, 15):
            for reading in (f"{name} (+{level})", f"{name}(+{level})", f"{name.replace(' ', '')}(+{level})",
                            f"{name} (+ {level})", f"{name} +{level}", f"{name}(+{level}"):
                assert strip_upgrades(reading).replace(" ", "") == name.replace(" ", ""), reading
    for name in ("Red Shadow(F)", "Dark Force Horns (M)", 'Red " + "', "Steel Ore 2", "Arrow for Bow (+0)x"):
        assert strip_upgrades(name) == name                                  # real names stay as they are

    class Equips(FakeSource):
        ITEMS = {1: Item(1, "Korean Fan", 1), 2: Item(2, "Brown Legolia Pants", 1), 3: Item(3, "Dark Force Horns (M)", 1),
                 4: Item(4, "Dark Force Horns (F)", 1)}
    db = ItemDB(tmp_path / "t.sqlite", source=Equips(), clock=Clock())
    assert names(db.lookup("KoreanFan(+7)")) == ["Korean Fan"]               # was [], "Not found on MeowDB"
    assert names(db.lookup("Brown Legolia Pants (+10)")) == ["Brown Legolia Pants"]
    assert names(db.lookup("Korean Fan")) == ["Korean Fan"]
    assert "Dark Force Horns (F)" in names(db.lookup("Dark Force Horns (F) (+3)"))  # gendered and enhanced
