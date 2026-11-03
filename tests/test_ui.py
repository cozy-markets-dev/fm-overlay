"""F10 and adding an alert in React (ui_child.py in its own process, ui_host.py in MaplePrice): the pipe between
them, what the pages are told, what they may ask, and the Tk windows when the React ones can't start."""
import io
import json
import sys
import threading
import time

from mprice_decoder import ui_child
from mprice_decoder.settings import Settings
from mprice_decoder.ui_host import Fallback, UiHost, WebAlert, WebOptions, avatar_url


class Host:
    def __init__(self, alive=True):
        self.sent, self._alive = [], alive

    def send(self, msg):
        self.sent.append(msg)
        return True

    def alive(self):
        return self._alive

    def last_state(self):
        return next(m["state"] for m in reversed(self.sent) if "state" in m)


class Login:
    def __init__(self):
        self.user, self.working, self.message, self.attempt = None, "", "", None
        self.calls = []
        for name in ("sign_in", "reopen", "cancel", "sign_out"):
            setattr(self, name, lambda name=name: self.calls.append(name))


def options(**kw):
    host, settings, changes = Host(), Settings(), []
    web = WebOptions(host, settings, lambda: changes.append(1), **kw)
    return web, host, settings, changes


def test_the_pages_are_told_the_switches_and_the_sign_in():
    login = Login()
    web, host, settings, _ = options(signin=login, open_site=lambda: None)
    web.toggle()
    assert host.sent[-1] == {"toggle": "options"}
    state = host.last_state()
    assert state["settings"] == {"shopPrices": True, "hoverPrices": False, "priceAlerts": True, "alertSound": True,
                                 "hoverKey": "F7", "alertKey": "F6", "bazaarSync": True, "bazaarPublic": False,
                                 "alertVolume": 100, "language": "en", "overlayScale": 1.0,
                                 "overlayScales": [1.0, 1.25, 1.5]}
    assert state["bazaar"] is None                                   # no sign-in: no Bazaar sync to tell of
    assert state["site"] is True and state["alert"] is None
    assert state["signin"] == {"user": None, "working": "", "message": "", "code": None}
    login.user = {"name": "Lotzz", "discordId": "123", "avatar": "a" * 32}
    web.refresh()
    assert host.last_state()["signin"]["user"] == {"name": "Lotzz",
                                                   "avatar": f"https://cdn.discordapp.com/avatars/123/{'a' * 32}.png?size=64"}
    assert options()[0].state()["signin"] is None and options()[0].state()["site"] is False


def test_a_discord_avatar_is_only_ever_a_discord_picture():
    assert avatar_url({"discordId": "123", "avatar": "a_" + "0" * 32}).startswith("https://cdn.discordapp.com/")
    for bad in ({"discordId": "123", "avatar": "../../x"}, {"discordId": "1/2", "avatar": "a" * 32},
                {"discordId": "123", "avatar": None}, {}):
        assert avatar_url(bad) is None


def test_what_a_page_asks_is_done_answered_and_followed_by_the_new_state():
    login, opened = Login(), []
    web, host, settings, changes = options(signin=login, open_site=lambda: opened.append("site"),
                                           on_quit=lambda: opened.append("quit"), open_alerts=lambda: opened.append("alerts"))
    web.handle({"call": "switch", "args": {"field": "hover_prices", "on": True}, "id": 1})
    assert settings.hover_prices is True and changes == [1]
    assert host.sent[-2] == {"reply": 1, "result": None} and host.last_state()["settings"]["hoverPrices"] is True
    web.handle({"call": "switch", "args": {"field": "cozy_url", "on": False}, "id": 2})   # not a switch: nothing
    assert settings.cozy_url == "https://cozy.markets" and changes == [1]
    for n, call in enumerate(("site", "my_alerts", "quit", "sign_in", "sign_in_again", "cancel_sign_in", "sign_out")):
        web.handle({"call": call, "args": {}, "id": 10 + n})
    assert opened == ["site", "alerts", "quit"] and login.calls == ["sign_in", "reopen", "cancel", "sign_out"]
    web.handle({"call": "no_such_thing", "id": 99})
    assert host.sent[-2] == {"reply": 99, "result": None}                                # still answered


def test_adding_an_alert_from_its_window():
    later, prices = [], []
    web, host, *_ = options(later=lambda ms, fn: later.append((ms, fn)))
    alert = WebAlert(web, "Blue Pole-Feather Hat", "4,999", "~4,999", b"png", submit=prices.append)
    assert host.sent[-1] == {"show": "alert"}
    form = host.last_state()["alert"]
    assert (form["name"], form["suggested"], form["usual"], form["icon"]) == (
        "Blue Pole-Feather Hat", "4,999", "~4,999", "data:image/png;base64,cG5n")
    web.handle({"call": "add_alert", "args": {"price": "lots"}, "id": 1})
    assert prices == [] and host.last_state()["alert"]["result"]["ok"] is False
    web.handle({"call": "add_alert", "args": {"price": "3.5k"}, "id": 2})
    web.handle({"call": "add_alert", "args": {"price": "3.5k"}, "id": 3})                   # pressed again: once
    assert prices == [3500] and host.last_state()["alert"]["busy"] is True
    alert.done(False, "5 items at most: stop watching one first.")
    assert host.last_state()["alert"]["busy"] is False and alert.is_open()
    alert.done(True, "Added")
    assert host.last_state()["alert"]["result"] == {"ok": True, "text": "Added"} and later[0][0] == 1500
    later[0][1]()                                                                           # closes itself
    assert host.sent[-1] == {"hide": "alert"} and not alert.is_open()
    again = WebAlert(web, "Yellow Square", "", None, None, submit=prices.append)
    assert host.last_state()["alert"]["id"] > form["id"] and again.is_open()
    web.handle({"event": "hidden", "window": "alert"})                                      # its Cancel or X
    assert not again.is_open()


def test_f10_is_the_react_window_while_its_process_runs_else_the_tk_one():
    class Window:
        capturing, toggled = False, 0

        def toggle(self):
            self.toggled += 1

    web, host, *_ = options()
    tk = Window()
    pick = Fallback(web, tk)
    pick.toggle()
    assert host.sent[-1] == {"toggle": "options"} and tk.toggled == 0
    host._alive = False
    pick.toggle()
    assert tk.toggled == 1 and Fallback(None, tk).current is tk


CHILD = """
import json, sys
print(json.dumps({"event": "ready"}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("quit"):
        break
    print(json.dumps({"call": "echo", "args": msg, "id": 1}), flush=True)
"""


def test_the_windows_process_is_started_talked_to_and_stopped():
    got = []
    host = UiHost(got.append, [sys.executable, "-c", CHILD])
    assert host.start(wait=10) and host.alive()
    assert host.send({"state": {"x": 1}})
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    assert got == [{"call": "echo", "args": {"state": {"x": 1}}, "id": 1}]
    host.close()
    assert host.proc.poll() is not None


def test_a_windows_process_that_cant_start_is_given_up_on_at_once():
    host = UiHost(lambda msg: None, [sys.executable, "-c", "import sys; sys.exit(1)"])
    t = time.monotonic()
    assert host.start(wait=10) is False and time.monotonic() - t < 5 and not host.alive()
    assert not UiHost(lambda msg: None, ["/no/such/program"]).start(wait=1)


class FakeEvent:
    def __init__(self):
        self.handlers, self._set = [], True

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def is_set(self):
        return self._set


class FakeWindow:
    def __init__(self, title, url, js_api, **kw):
        self.title, self.url, self.api, self.kw, self.calls = title, url, js_api, kw, []
        self.events = type("Events", (), {})()
        self.events.closing, self.events.loaded = FakeEvent(), FakeEvent()
        for name in ("show", "hide", "restore", "destroy"):
            setattr(self, name, lambda name=name: self.calls.append(name))

    def resize(self, w, h):
        self.calls.append(("resize", w, h))

    def run_js(self, script):
        self.calls.append(("js", script))


class FakeWebview:
    def __init__(self):
        self.windows = []

    def create_window(self, title, url, js_api, **kw):
        w = FakeWindow(title, url, js_api, **kw)
        self.windows.append(w)
        return w


def test_the_windows_process_shows_hides_and_carries_calls_both_ways():
    out, lines = io.BytesIO(), []
    webview = FakeWebview()
    to_child = io.BytesIO()
    exits = []
    child = ui_child.Child(to_child, out, webview, exit=exits.append)
    options = child.window("options")
    assert options.kw["hidden"] and options.url.endswith("mapleprice.html#options") and options.kw["on_top"]

    def feed(*msgs):
        child.inp = io.BytesIO(b"".join((json.dumps(m) + "\n").encode() for m in msgs))
        child.read()

    feed({"state": {"n": 1}}, {"toggle": "options"})
    assert child.state == {"n": 1} and options.calls[:1] == [("js", 'window.__maple && window.__maple.update({"n": 1})')]
    assert "show" in options.calls and "options" in child.shown
    assert options.events.closing.handlers[0]() is False                     # its X: hidden, not closed
    assert "hide" in options.calls and "options" not in child.shown
    sent = [json.loads(l) for l in out.getvalue().splitlines()]
    assert {"event": "hidden", "window": "options"} in sent
    feed({"show": "options"})
    options.api.fit(300, 344, 381)              # a 16 x 39 frame: the window grows by it
    assert options.calls[-1] == ("resize", ui_child.WIDTH["options"] + 16, 339)
    options.api.fit(280, 344, 381)              # a later fit that crossed that resize: the frame isn't measured again
    assert options.calls[-1] == ("resize", ui_child.WIDTH["options"] + 16, 319)
    alert_window = child.window("alert")
    alert_window.api.fit(200, 384, 381)         # hidden: resizing would show it (pywebview), so it waits
    assert not any(c[0] == "resize" for c in alert_window.calls if isinstance(c, tuple))
    feed({"show": "alert"})
    assert ("resize", ui_child.WIDTH["alert"] + 16, 239) in alert_window.calls     # shown: now it's sized
    feed({"hide": "alert"})

    answer = []
    asking = threading.Thread(target=lambda: answer.append(options.api.call("switch", {"field": "shop_prices"})))
    asking.start()
    deadline = time.monotonic() + 5
    while '"call": "switch"' not in out.getvalue().decode() and time.monotonic() < deadline:
        time.sleep(0.01)
    ask = json.loads(out.getvalue().splitlines()[-1])
    feed({"reply": ask["id"], "result": "done"}, {"show": "alert"}, {"quit": True})
    asking.join(5)
    assert answer == ["done"] and ask["args"] == {"field": "shop_prices"}
    alert = child.windows["alert"]
    assert alert.url.endswith("#alert") and "show" in alert.calls
    assert "destroy" in options.calls and "destroy" in alert.calls             # MaplePrice closing: so is this
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exits and set(exits) == {0}                                         # and if they don't close, it ends


def test_the_alert_volume_language_and_overlay_size_are_set_from_the_page_and_checked():
    played = []
    web, host, settings, changes = options(test_sound=lambda: played.append(settings.alert_volume))
    web.handle({"call": "volume", "args": {"value": 35}, "id": 1})
    web.handle({"call": "test_sound", "args": {}, "id": 2})
    assert settings.alert_volume == 35 and played == [35] and host.last_state()["settings"]["alertVolume"] == 35
    web.handle({"call": "language", "args": {"code": "zh"}, "id": 3})
    web.handle({"call": "overlay_scale", "args": {"value": 1.5}, "id": 4})
    assert (settings.language, settings.overlay_scale) == ("zh", 1.5) and len(changes) == 3
    for call, args in (("volume", {"value": "loud"}), ("language", {"code": "fr"}), ("overlay_scale", {"value": 3})):
        web.handle({"call": call, "args": args, "id": 9})       # not a volume, language or size: nothing
    assert (settings.alert_volume, settings.language, settings.overlay_scale) == (35, "zh", 1.5) and len(changes) == 3
    web.handle({"call": "volume", "args": {"value": 250}, "id": 10})
    assert settings.alert_volume == 100                          # kept to 0-100


def test_hand_edited_volume_language_and_size_fall_back_to_the_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alert_volume": "x", "language": "fr", "overlay_scale": 9}))
    s = Settings.load(path)
    assert (s.alert_volume, s.language, s.overlay_scale) == (100, "en", 1.0)
    path.write_text(json.dumps({"alert_volume": 140, "language": "zh", "overlay_scale": 1.25}))
    s = Settings.load(path)
    assert (s.alert_volume, s.language, s.overlay_scale) == (100, "zh", 1.25)


def test_the_chime_is_scaled_to_its_volume():
    import array
    import io
    import wave
    from mprice_decoder import __main__ as app

    def samples(data):
        with wave.open(io.BytesIO(data)) as w:
            return w.getparams(), array.array("h", w.readframes(w.getnframes()))
    full_params, full = samples(app.alert_wav(100))
    half_params, half = samples(app.alert_wav(50))
    assert full_params == half_params and len(full) == len(half) > 1000
    loudest = max(range(len(full)), key=lambda i: abs(full[i]))
    assert half[loudest] == int(full[loudest] * 0.25)            # the square of the volume: half sounds like half
    assert not any(samples(app.alert_wav(0))[1]) and app.alert_wav(50) is app.alert_wav(50)
    app.chime(0)                                                  # silent: nothing played, nothing raised


def test_overlay_texts_are_looked_up_by_their_english_and_missing_ones_stay_english():
    from mprice_decoder import i18n
    from mprice_decoder.annotate import make_alert_card
    try:
        i18n.set_language("zh")
        assert i18n.t("Asking ") == "售价 " and i18n.t("{n} sold", n=23) == "已售 23"
        assert i18n.t("Nothing like this is translated") == "Nothing like this is translated"
        assert i18n.t("{who} waved", who="Cheng") == "Cheng waved"
        card = make_alert_card({"itemName": "Red Shadow", "price": 9000, "maxPrice": 10000, "shop": "Leanback"})
        assert (card.title, card.subtitle, card.price[0][0]) == ("Red Shadow 售 9,000", "低于你的 10k", "商店 ")
        i18n.set_language("klingon")                               # not a language: English
        assert i18n.language == "en" and i18n.t("Asking ") == "Asking "
    finally:
        i18n.set_language("en")
    keys = [k for k in i18n.ZH if k.count("{") != i18n.ZH[k].count("{")]
    assert keys == []                                              # every translation fills in what its English does


def test_the_overlay_size_draws_cards_bigger_from_the_same_spots():
    from PIL import Image
    from mprice_decoder.annotate import Card, Label, Panel, hover_panel, shrink
    from mprice_decoder.render import PilPainter, draw_panel

    def drawn(scale):
        img = Image.new("RGB", (1400, 900), "#ff00fe")
        card = Card("Red Shadow", "Lv 20", [("Asking ", "#000000"), ("9,000", "#000000")])
        labels, panel = shrink([], Panel(300, 100, 10_000, 800, cards=[card]), scale)
        draw_panel(PilPainter(img, scale), panel)
        return _not_key_box(img)
    one, big = drawn(1), drawn(1.5)
    assert one[:2] == big[:2] == (300, 100)                       # the same corner on screen
    assert abs((big[2] - big[0]) - 1.5 * (one[2] - one[0])) <= 3   # 1.5 times as wide and tall
    assert abs((big[3] - big[1]) - 1.5 * (one[3] - one[1])) <= 6
    labels, _ = shrink([Label(600, 300)], None, 1.5)
    assert (labels[0].right, labels[0].center_y) == (400, 200)
    panel = hover_panel(None, (900, 300), 1, size=1.5)              # left of the tooltip: its right edge stays put
    assert panel.right * 1.5 == 900 - 14 and panel.right - panel.left == 420


def _not_key_box(img):
    """The box around what's drawn on a key-coloured (#ff00fe) image."""
    from PIL import Image, ImageChops
    key = Image.new("RGB", img.size, "#ff00fe")
    return ImageChops.difference(img, key).getbbox()
