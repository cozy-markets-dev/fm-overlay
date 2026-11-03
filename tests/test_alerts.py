"""Price alerts in MaplePrice (alerts.py): Cozy Markets' alerts for the Discord account it's signed in with,
popped up as they come; F6 adds the hovered item; F10 switches them off."""
import queue
import sys
import types

import pytest

from mprice_decoder import __main__ as app
from mprice_decoder.alerts import Alerts, mesos
from mprice_decoder.annotate import make_alert_card
from mprice_decoder.auth import AuthError, AuthService, MemoryStore
from mprice_decoder.settings import Settings

from test_auth import START, SIGNED_IN, TOKEN, Opener
from test_shop import make_db


def text(line):
    return "".join(t for t, _ in line)


def test_prices_are_read_as_people_type_them():
    assert [mesos(t) for t in ("2499", "2,499", "2.5k", "1.2m", "3b", " 750 mesos ", "1K")] == [
        2499, 2499, 2500, 1_200_000, 3_000_000_000, 750, 1000]
    assert [mesos(t) for t in ("", "abc", "0", "-5", "1.2.3", "5x")] == [None] * 6


def signed_in(*responses):
    """An AuthService signed in, whose next requests get `responses`."""
    transport = Opener((200, START), (200, SIGNED_IN), *responses)
    service = AuthService(store=MemoryStore(), opener=transport)
    service.poll(service.start())
    return service, transport


def test_the_app_routes_carry_its_token_and_say_why_they_refuse():
    service, transport = signed_in((200, {"watch": {"id": 3}}), (400, {"error": "5 items at most: stop watching one first"}),
                                   (401, {"error": "MaplePrice sign-in expired"}))
    assert Alerts(service, Settings(), queue.Queue()).add(834, "Blue Pole-Feather Hat", 3000) == {"id": 3}
    request = transport.requests[-1]
    assert request.full_url == "https://cozy.markets/trade-api/desktop/watches" and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    with pytest.raises(AuthError, match=r"^5 items at most: stop watching one first\.$"):
        service.api("POST", "/watches", {})
    with pytest.raises(AuthError):
        service.api("GET", "/watches")
    assert service.session is None          # a sign-in the server no longer takes: signed out here too


class Service:
    def __init__(self, *bodies):
        self.session, self.bodies, self.asked = object(), list(bodies), 0

    def api(self, method, path, body=None):
        self.asked += 1
        b = self.bodies.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def hit(id, name="Claw", price=900):
    return {"id": id, "itemId": 1, "itemName": name, "price": price, "qty": 1, "maxPrice": 1000, "shop": "Al",
            "channel": 3, "fmRoom": 7, "mapName": "Free Market <7>"}


def test_new_alerts_pop_up_once_and_the_first_look_only_counts_the_unread():
    events, settings = queue.Queue(), Settings()
    service = Service({"hits": [hit(5), hit(4)], "unread": 2}, {"hits": [hit(7), hit(6), hit(5)], "unread": 4},
                      {"hits": [hit(7)], "unread": 4}, AuthError("unavailable", "offline"),
                      {"hits": [hit(9)], "unread": 1})
    alerts = Alerts(service, settings, events)
    alerts.step()
    assert events.get_nowait() == ("alerts_unread", 2) and events.empty()   # what came before: on the website
    alerts.step()
    kind, new = events.get_nowait()
    assert kind == "alerts" and [h["id"] for h in new] == [6, 7]           # oldest first
    alerts.step()
    alerts.step()                                                          # nothing new; then offline
    assert events.empty()
    settings.price_alerts = False
    alerts.step()
    assert alerts.last is None and service.asked == 4                      # off: not asked
    service.session = None
    settings.price_alerts = True
    alerts.step()
    assert service.asked == 4                                              # signed out: not asked


def test_an_alert_card_says_where_and_against_what():
    card = make_alert_card({**hit(1, "Blue Pole-Feather Hat", 2500), "qty": 2, "maxPrice": 3000})
    assert (card.title, card.subtitle) == ("Blue Pole-Feather Hat for 2,500 ea", "under your 3,000 · Ch 3 · FM 7")
    assert text(card.price) == "Shop Al"   # worded like the website's pop-up, with the shop to find it by


def test_the_hotkey_names_the_hovered_item_and_what_it_usually_goes_for(tmp_path, monkeypatch):
    from mprice_decoder import ocr
    from mprice_decoder.cozy_prices import CozyPrice
    fake = types.ModuleType("mprice_decoder.capture")
    fake.grab_game = lambda rect: (None, 1.0)
    monkeypatch.setitem(sys.modules, "mprice_decoder.capture", fake)
    monkeypatch.setattr(sys.modules["mprice_decoder"], "capture", fake, raising=False)
    monkeypatch.setattr(app, "cursor_pos", lambda: (110, 220))
    seen = []
    monkeypatch.setattr(ocr, "read_tooltip", lambda img, cursor: seen.append(cursor) or (["Pigs Head"], (0, 0)))

    class Prices:
        def get(self, item_id):
            return CozyPrice(usual=450.0)
    monkeypatch.setattr(app, "cozy_prices", Prices())
    item, worth = app.hovered_item(make_db(tmp_path), (10, 20, 800, 600))
    assert (item.name, worth, seen) == ("Pig's Head", 450.0, [(100, 200)])
    monkeypatch.setattr(ocr, "read_tooltip", lambda img, cursor: None)
    assert app.hovered_item(make_db(tmp_path), (10, 20, 800, 600)) is None


def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    return root


def test_the_alert_dialog_takes_a_price_and_shows_the_answer():
    from mprice_decoder.overlay import AlertDialog
    root = tk_root()
    try:
        asked = []
        dialog = AlertDialog(root, "Blue Pole-Feather Hat", "4,999", "Usually ~4,999 on Cozy Markets.")
        dialog.submit = asked.append
        dialog.entry.delete(0, "end")
        dialog.entry.insert(0, "lots")
        dialog._add()
        assert asked == [] and dialog.message.cget("text") == "Type a price, like 2,500 or 2.5k."
        dialog.entry.delete(0, "end")
        dialog.entry.insert(0, "3.5k")
        dialog._add()
        dialog._add()                                   # a second press while it's being added: nothing
        assert asked == [3500] and str(dialog.add.cget("state")) == "disabled"
        dialog.done(False, "5 items at most: stop watching one first.")
        assert str(dialog.add.cget("state")) == "normal"
        dialog.done(True, "Added")
        assert dialog.message.cget("text") == "Added" and dialog.is_open()   # closes itself shortly
        dialog.close()
        assert not dialog.is_open()
        dialog.done(True, "late answer")                # closed already: nothing to do
    finally:
        root.destroy()


def test_every_hotkey_can_be_set_on_the_command_line():
    """run_overlay reads an override for each one: a hotkey without its option stopped MaplePrice starting."""
    from mprice_decoder.settings import HOTKEYS
    args = app.arguments().parse_args([])
    assert all(hasattr(args, field) and getattr(args, field) is None for field, _ in HOTKEYS)
    assert app.arguments().parse_args(["--alert-key", "F5"]).alert_key == "F5"


def test_the_alert_sound_is_a_short_quiet_chime_and_can_be_switched_off():
    import wave
    from mprice_decoder.ui_host import WebOptions
    with wave.open(str(app.ALERT_SOUND)) as w:
        seconds = w.getnframes() / w.getframerate()
        frames = w.readframes(w.getnframes())
    assert w.getnchannels() == 1 and 0.3 < seconds < 1.5
    loudest = max(abs(int.from_bytes(frames[i:i + 2], "little", signed=True)) for i in range(0, len(frames), 2))
    assert loudest < 0.6 * 32767                      # a chime over the game, not an alarm
    app.chime()                                       # off Windows: nothing, and no error
    settings = Settings()

    class Host:
        def send(self, msg):
            return True
    WebOptions(Host(), settings, lambda: None).handle({"call": "switch", "args": {"field": "alert_sound", "on": False}, "id": 1})
    assert settings.alert_sound is False


def test_the_watch_list_comes_from_the_apps_routes_and_an_item_can_be_let_go():
    service, transport = signed_in((200, {"watches": [{"id": 4, "itemId": 834, "itemName": "Blue Pole-Feather Hat",
                                                       "maxPrice": 3000}, {"bad": 1}], "most": 5}),
                                   (200, {"ok": True}))
    alerts = Alerts(service, Settings(), queue.Queue())
    watches, most = alerts.watching()
    assert [w["id"] for w in watches] == [4] and most == 5
    assert transport.requests[-1].full_url == "https://cozy.markets/trade-api/desktop/watches"
    alerts.remove(4)
    assert transport.requests[-1].get_method() == "DELETE" and transport.requests[-1].full_url.endswith("/watches/4")


def test_f10_shows_the_watch_list_signed_in_and_unwatches_on_its_x():
    from mprice_decoder.ui_host import WebOptions

    class Host:
        def __init__(self):
            self.sent = []

        def send(self, msg):
            self.sent.append(msg)
            return True

    class Login:
        user, working, message, attempt = {"name": "Lotzz"}, "", "", None

    let_go = []
    web = WebOptions(Host(), Settings(), lambda: None, signin=Login(), unwatch=let_go.append)
    assert web.state()["watching"] is None                       # not asked yet
    web.watching = {"most": 5, "items": [{"id": 4, "itemId": 834, "name": "Blue Pole-Feather Hat", "maxPrice": 3000,
                                          "icon": None}]}
    assert web.state()["watching"]["items"][0]["maxPrice"] == 3000
    web.handle({"call": "unwatch", "args": {"id": 4}, "id": 1})
    web.handle({"call": "unwatch", "args": {"id": "4; drop"}, "id": 2})  # not an id: nothing
    assert let_go == [4]
    Login.user = None
    assert web.state()["watching"] is None                       # signed out: not shown


def test_the_most_youd_pay_can_be_changed_from_f10():
    from mprice_decoder.ui_host import WebOptions
    service, transport = signed_in((200, {"watch": {"id": 4, "maxPrice": 2500}}))
    assert Alerts(service, Settings(), queue.Queue()).change(4, 2500) == {"id": 4, "maxPrice": 2500}
    request = transport.requests[-1]
    assert (request.get_method(), request.full_url) == ("PATCH", "https://cozy.markets/trade-api/desktop/watches/4")

    class Host:
        def __init__(self):
            self.sent = []

        def send(self, msg):
            self.sent.append(msg)
            return True

    class Login:
        user, working, message, attempt = {"name": "Lotzz"}, "", "", None

    changed, host = [], Host()
    web = WebOptions(host, Settings(), lambda: None, signin=Login(), set_max=lambda i, p: changed.append((i, p)))
    web.watching = {"most": 5, "items": [{"id": 4, "itemId": 155, "name": "Pet Equip Jump Scroll: Greater",
                                          "maxPrice": 9000, "icon": None}]}
    web.handle({"call": "set_max", "args": {"id": 4, "price": "2.5k"}, "id": 1})
    assert changed == [(4, 2500)] and host.sent[-2] == {"reply": 1, "result": {"ok": True}}
    assert host.sent[-1]["state"]["watching"]["items"][0]["maxPrice"] == 2500    # shown at once, not "saving..."
    web.handle({"call": "set_max", "args": {"id": 4, "price": "lots"}, "id": 2})
    assert changed == [(4, 2500)] and host.sent[-2]["result"] == {"ok": False, "text": "Type a price, like 2,500 or 2.5k."}
    web.watching, web.watch_note = {"most": 5, "items": []}, "You're not watching that."
    assert web.state()["watching"]["note"] == "You're not watching that."
