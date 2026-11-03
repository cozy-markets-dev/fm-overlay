import io
import json
import threading
import time
from urllib.error import HTTPError

import pytest

from mprice_decoder.auth import AuthError, AuthService, MemoryStore, WindowsSessionStore

DEVICE = "d" * 43
TOKEN = "mp_" + "t" * 43
USER = {"id": "1", "discordId": "123456789", "name": "Cozy Friend", "username": "friend", "avatar": None}
START = {"device_code": DEVICE, "user_code": "ABCD-2345", "expires_in": 600, "interval": 5,
         "verification_uri_complete": "https://untrusted.example/ignore-this"}
SIGNED_IN = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400, "user": USER}


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 10
        status, data = self.responses.pop(0)
        if status != 200:
            raise HTTPError(request.full_url, status, "failed", {}, io.BytesIO(json.dumps(data).encode()))
        response = io.BytesIO(json.dumps(data).encode())
        response.code = status
        return response


def test_sign_in_pending_backoff_session_restore_and_logout():
    transport = Opener((200, START), (400, {"error": "authorization_pending"}),
                       (400, {"error": "slow_down", "interval": 10}), (200, SIGNED_IN),
                       (200, {"user": USER}), (200, {"ok": True}))
    store = MemoryStore()
    service = AuthService(store=store, opener=transport)
    attempt = service.start()
    assert attempt.url == "https://cozy.markets/trade-api/auth/desktop?code=ABCD-2345"
    assert DEVICE not in repr(attempt)
    assert service.poll(attempt) is None
    assert service.poll(attempt) is None and attempt.interval == 10
    session = service.poll(attempt)
    assert session.user == USER and TOKEN not in repr(session)
    restored = AuthService(store=store, opener=transport)
    assert restored.me() == USER
    assert transport.requests[-1].get_header("Authorization") == "Bearer " + TOKEN
    assert not any(r.get_header("Cookie") or r.get_header("Origin") for r in transport.requests)
    assert DEVICE not in transport.requests[1].full_url
    restored.logout()
    assert store.load() is None and restored.session is None


@pytest.mark.parametrize("code", ["expired_token", "access_denied", "invalid_grant"])
def test_terminal_poll_errors_do_not_create_a_session(code):
    service = AuthService(store=MemoryStore(), opener=Opener((200, START), (400, {"error": code})))  # not this PC's
    attempt = service.start()
    with pytest.raises(AuthError) as error:
        service.poll(attempt)
    assert error.value.code == code and service.session is None


def test_expired_or_cancelled_attempts_do_not_contact_the_service():
    transport = Opener((200, START))
    service = AuthService(store=MemoryStore(), opener=transport)
    attempt = service.start()
    attempt.expires_at = time.monotonic() - 1
    with pytest.raises(AuthError, match="expired"):
        service.poll(attempt)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AuthError, match="cancelled"):
        service.wait(attempt, cancel)
    assert len(transport.requests) == 1


def test_saved_credentials_are_bound_to_the_site_and_expire():
    store = MemoryStore()
    service = AuthService(store=store, opener=Opener((200, START), (200, SIGNED_IN)))
    service.poll(service.start())
    assert AuthService("https://another.example", store=store).session is None
    store.value["expires_at"] = time.time() - 1
    assert AuthService(store=store).session is None


def test_revoked_account_clears_local_sign_in():
    store = MemoryStore()
    service = AuthService(store=store, opener=Opener((200, START), (200, SIGNED_IN), (403, {"error": "banned"})))
    service.poll(service.start())
    with pytest.raises(AuthError):
        service.me()
    assert service.session is None and store.load() is None


@pytest.mark.parametrize("origin", ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?next=bad"])
def test_refuses_unsafe_service_origins(origin):
    with pytest.raises(ValueError):
        AuthService(origin)


def test_windows_store_only_writes_protected_bytes(monkeypatch, tmp_path):
    from mprice_decoder import auth
    calls = []

    def protect(data, *, decrypt=False):
        calls.append(decrypt)
        return b'{"test": true}' if decrypt else b"protected-not-plaintext"

    monkeypatch.setattr(auth, "_dpapi", protect)
    store = WindowsSessionStore(tmp_path / "discord-session.bin")
    store.save({"access_token": TOKEN})
    assert store.path.read_bytes() == b"protected-not-plaintext"
    assert not store.path.with_suffix(".tmp").exists()
    assert store.load() == {"test": True} and calls == [False, True]
    store.clear()
    assert not store.path.exists()


def test_failed_credential_save_revokes_the_new_server_session():
    class FailedStore(MemoryStore):
        def save(self, value):
            raise OSError("disk unavailable")

    transport = Opener((200, START), (200, SIGNED_IN), (200, {"ok": True}))
    service = AuthService(store=FailedStore(), opener=transport)
    with pytest.raises(AuthError, match="save your sign-in"):
        service.poll(service.start())
    assert service.session is None
    assert transport.requests[-1].full_url.endswith("/logout")


def test_redirects_cannot_forward_a_bearer_token():
    from mprice_decoder.auth import _NoRedirects
    assert _NoRedirects().redirect_request(None, None, 302, "", {}, "https://elsewhere.example") is None


class FakeService:
    """AuthService's surface for Login: no network, and wait() returns once it's let go."""
    def __init__(self, start_error=None, logout_error=None):
        self.session, self.start_error, self.logout_error = None, start_error, logout_error
        self.approved = threading.Event()

    def start(self):
        if self.start_error:
            raise self.start_error
        from mprice_decoder.auth import LoginAttempt
        return LoginAttempt(DEVICE, "ABCD-2345", "https://cozy.markets/trade-api/auth/desktop?code=ABCD-2345",
                            time.monotonic() + 600)

    def wait(self, attempt, cancel):
        while not self.approved.is_set():
            if cancel.wait(0.01):
                raise AuthError("cancelled", "Sign-in cancelled.")
        from mprice_decoder.auth import Session
        self.session = Session(TOKEN, USER, time.time() + 60)
        return self.session

    def me(self):
        self.session = None
        raise AuthError("signed_out", "Sign in with Discord first.", 401)

    def logout(self):
        try:
            if self.logout_error:
                raise self.logout_error
        finally:
            self.session = None


def login_with(service):
    from mprice_decoder.auth import Login
    changes, opened = [], []
    login = Login(service, lambda: changes.append((login.working, login.attempt, login.user)), opened.append)
    threads = []
    login._run = lambda work: threads.append(threading.Thread(target=work, daemon=True)) or threads[-1].start()
    finish = lambda: [t.join(5) for t in threads]
    return login, changes, opened, finish


def settle(condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


def test_login_opens_the_approval_page_and_signs_in():
    service = FakeService()
    login, changes, opened, finish = login_with(service)
    login.sign_in()
    settle(lambda: login.attempt is not None)
    assert opened == ["https://cozy.markets/trade-api/auth/desktop?code=ABCD-2345"]
    assert login.attempt.user_code == "ABCD-2345" and login.user is None
    login.sign_in()                                 # a second click while waiting does nothing
    login.reopen()
    assert len(opened) == 2
    service.approved.set()
    finish()
    assert login.user == USER and login.attempt is None and not login.working and not login.message
    assert changes[0][0] == "Starting sign-in..." and changes[-1] == ("", None, USER)


def test_login_cancel_and_failures_leave_it_signed_out():
    login, _, _, finish = login_with(FakeService())
    login.sign_in()
    settle(lambda: login.attempt is not None)
    login.cancel()
    finish()
    assert login.user is None and login.attempt is None and not login.message  # cancelling isn't an error

    login, _, opened, finish = login_with(FakeService(start_error=AuthError("unavailable", "Could not reach Discord sign-in.")))
    login.sign_in()
    finish()
    assert login.message == "Could not reach Discord sign-in." and not opened and not login.working


def test_login_sign_out_and_a_refused_saved_session():
    service = FakeService(logout_error=AuthError("unavailable", "offline"))
    login, _, _, finish = login_with(service)
    service.approved.set()
    login.sign_in()
    finish()
    assert login.user == USER
    login.sign_out()                               # offline: still signed out on this PC
    finish()
    assert login.user is None and not login.working and not login.message

    service = FakeService()
    service.approved.set()
    service.wait(None, threading.Event())          # a saved sign-in the server no longer takes
    login, changes, _, finish = login_with(service)
    assert login.user == USER
    login.check()
    finish()
    assert login.user is None and changes


def test_signing_out_runs_the_unlink_first_while_the_sign_in_still_stands():
    service = FakeService()
    service.approved.set()
    login, _, _, finish = login_with(service)
    login.sign_in()
    finish()
    seen = []
    login.before_sign_out = lambda: seen.append(service.session is not None)
    login.sign_out()
    finish()
    assert seen == [True] and login.user is None
    login.before_sign_out = lambda: 1 / 0          # a failing unlink never keeps anyone signed in
    service.approved.set()
    login.sign_in()
    finish()
    login.sign_out()
    finish()
    assert login.user is None


def test_options_window_shows_each_sign_in_state():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    from mprice_decoder.overlay import OptionsWindow
    from mprice_decoder.settings import Settings
    service = FakeService()
    login, _, _, finish = login_with(service)
    texts = lambda: [w.cget("text") for row in win.account.winfo_children()
                     for w in [row, *row.winfo_children()] if "text" in w.keys()]
    try:
        win = OptionsWindow(root, Settings(), lambda: None, lambda: None, lambda: None,
                            open_site=lambda: None, on_quit=lambda: None, signin=login)
        win.toggle()
        assert texts()[0] == "Sign in with Discord"
        login.sign_in()
        settle(lambda: login.attempt is not None)
        win.refresh()
        assert "ABCD-2345" in texts() and "Cancel" in texts()
        service.approved.set()
        finish()
        win.refresh()
        assert texts() == ["Signed in with Discord as", "Cozy Friend", "Sign out",
                           "F6 adds the item you're hovering to your price alerts."]
        win.toggle()
        win.refresh()                              # closed: nothing to redraw
    finally:
        login.cancel()
        root.destroy()


def test_links_with_a_query_open_through_an_internet_shortcut(monkeypatch, tmp_path):
    import mprice_decoder.__main__ as app
    opened = []
    monkeypatch.setattr(app.subprocess, "Popen", opened.append)
    monkeypatch.setattr(app.tempfile, "gettempdir", lambda: str(tmp_path))
    app.open_in_browser("https://cozy.markets/market")  # no query: as it is
    app.open_in_browser("https://cozy.markets/trade-api/auth/desktop?code=ABCD-2345")
    shortcut = tmp_path / f"{app.APP_ID}-link.url"
    assert opened == [["explorer.exe", "https://cozy.markets/market"], ["explorer.exe", str(shortcut)]]
    assert shortcut.read_text() == "[InternetShortcut]\nURL=https://cozy.markets/trade-api/auth/desktop?code=ABCD-2345\n"
