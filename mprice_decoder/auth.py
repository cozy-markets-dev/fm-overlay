"""Discord sign-in through Cozy Markets; no Discord secrets or tokens in the app.

Source service check: python -m mprice_decoder.auth [--origin http://localhost:5173]
"""
import argparse
import ctypes
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .identity import data_directory
from .version import VERSION

log = logging.getLogger("mprice_decoder")


class AuthError(Exception):
    def __init__(self, code, message, status=0, interval=None):
        super().__init__(message)
        self.code, self.status, self.interval = code, status, interval


@dataclass
class LoginAttempt:
    device_code: str = field(repr=False)
    user_code: str
    url: str
    expires_at: float
    interval: int = 5


@dataclass
class Session:
    access_token: str = field(repr=False)
    user: dict
    expires_at: float


class MemoryStore:
    """Non-Windows service development does not persist a bearer token to disk."""
    def __init__(self):
        self.value = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def clear(self):
        self.value = None


def _dpapi(data, *, decrypt=False):
    """Protect credentials for the current Windows user, without prompting."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class WindowsSessionStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_directory() / "discord-session.bin"

    def load(self):
        try:
            return json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
        except (OSError, ValueError):
            return None  # deleted, corrupt, or saved by a different Windows user

    def save(self, value):
        protected = _dpapi(json.dumps(value).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_bytes(protected)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self):
        self.path.unlink(missing_ok=True)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward an app token to another host


class AuthService:
    def __init__(self, origin="https://cozy.markets", *, store=None, opener=None):
        parsed = urlsplit(origin)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or not parsed.netloc:
            raise ValueError("Discord sign-in requires HTTPS (HTTP is allowed on localhost)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Use the site's origin for Discord sign-in")
        self.origin = origin.rstrip("/")
        self.store = store if store is not None else (WindowsSessionStore() if sys.platform == "win32" else MemoryStore())
        self.opener = opener or build_opener(_NoRedirects())
        self.session = None
        saved = self.store.load()
        if isinstance(saved, dict) and saved.get("origin") == self.origin:
            token, expires, user = saved.get("access_token"), saved.get("expires_at"), saved.get("user")
            if (isinstance(token, str) and re.fullmatch(r"mp_[A-Za-z0-9_-]{43}", token)
                    and isinstance(expires, (int, float)) and expires > time.time() and isinstance(user, dict)):
                self.session = Session(token, user, expires)

    def _request(self, method, path, body=None, *, authenticated=False, prefix="/trade-api/auth/desktop",
                 plain_errors=False):
        headers = {"Accept": "application/json", "User-Agent": f"MaplePrice/{VERSION}"}
        if authenticated:
            if self.session is None or self.session.expires_at <= time.time():
                raise AuthError("signed_out", "Sign in with Discord first.", 401)
            headers["Authorization"] = f"Bearer {self.session.access_token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + prefix + path, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("response too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("expected an object")
        except (URLError, OSError, TimeoutError) as error:
            raise AuthError("unavailable", "Could not reach Discord sign-in. Please try again.") from error
        except (ValueError, UnicodeError) as error:
            raise AuthError("bad_response", "The sign-in service returned an invalid response.") from error
        if status != 200:
            code = result.get("error", "failed")
            if plain_errors and isinstance(code, str) and 0 < len(code) <= 200:  # the server's own words
                raise AuthError("refused", code[:1].upper() + code[1:].rstrip(".") + ".", status)
            messages = {"authorization_pending": "Waiting for approval in your browser.",
                        "slow_down": "Waiting before checking sign-in again.",
                        "expired_token": "Sign-in expired. Please start again.",
                        "access_denied": "Sign-in was cancelled or this account cannot connect.",
                        "invalid_grant": "This sign-in has ended. Please start again."}
            raise AuthError(str(code), messages.get(str(code), "Discord sign-in failed. Please try again."),
                            status, interval=result.get("interval"))
        return result

    def start(self):
        result = self._request("POST", "/start", {})
        device, code = result.get("device_code"), result.get("user_code")
        ttl, interval = result.get("expires_in"), result.get("interval")
        if (not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", device)
                or not isinstance(code, str) or not re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code)
                or not isinstance(ttl, int) or not 1 <= ttl <= 600
                or not isinstance(interval, int) or not 5 <= interval <= 60):
            raise AuthError("bad_response", "The sign-in service returned an invalid response.")
        # Build this from the configured origin, not an arbitrary browser URL in a response.
        return LoginAttempt(device, code, self.origin + "/trade-api/auth/desktop?code=" + code,
                            time.monotonic() + ttl, interval)

    def poll(self, attempt):
        if time.monotonic() >= attempt.expires_at:
            raise AuthError("expired_token", "Sign-in expired. Please start again.")
        try:
            result = self._request("POST", "/token", {"device_code": attempt.device_code})
        except AuthError as error:
            if error.status == 400 and error.code in ("authorization_pending", "slow_down"):
                if error.code == "slow_down":
                    attempt.interval = min(60, max(attempt.interval + 5, error.interval if isinstance(error.interval, int) else 0))
                return None
            raise
        token, ttl, user = result.get("access_token"), result.get("expires_in"), result.get("user")
        if (not isinstance(token, str) or not re.fullmatch(r"mp_[A-Za-z0-9_-]{43}", token)
                or result.get("token_type") != "Bearer" or not isinstance(ttl, int) or not 1 <= ttl <= 30 * 86400
                or not isinstance(user, dict) or not isinstance(user.get("discordId"), str)):
            raise AuthError("bad_response", "The sign-in service returned an invalid response.")
        session = Session(token, user, time.time() + ttl)
        self.session = session
        try:
            self.store.save({"origin": self.origin, "access_token": token, "user": user, "expires_at": session.expires_at})
        except OSError as error:
            try:
                self.logout()
            except (AuthError, OSError):
                pass
            raise AuthError("storage_failed", "Could not save your sign-in. Please try again.") from error
        return session

    def wait(self, attempt, cancel=None):
        """Call from a worker; cancellation wakes it immediately between requests."""
        cancel = cancel if cancel is not None else threading.Event()
        while not cancel.wait(min(attempt.interval, max(0, attempt.expires_at - time.monotonic()))):
            session = self.poll(attempt)
            if session is not None:
                return session
        raise AuthError("cancelled", "Sign-in cancelled.")

    def me(self):
        try:
            result = self._request("GET", "/me", authenticated=True)
        except AuthError as error:
            if error.status in (401, 403):
                self.session = None
                self.store.clear()
            raise
        user = result.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("discordId"), str):
            raise AuthError("bad_response", "The sign-in service returned an invalid response.")
        self.session.user = user
        return user

    def api(self, method, path, body=None, prefix="/trade-api/desktop"):
        """One of MaplePrice's routes for the signed-in trader (/trade-api/desktop: price alerts;
        /trade-api/inventory/desktop: the Bazaar). A refusal says why in the server's words; a sign-in it no longer
        takes is forgotten here too."""
        try:
            return self._request(method, path, body, authenticated=True, prefix=prefix, plain_errors=True)
        except AuthError as error:
            if error.status == 401:
                self.session = None
                self.store.clear()
            raise

    def logout(self):
        try:
            if self.session:
                self._request("POST", "/logout", {}, authenticated=True)
        finally:
            self.session = None
            self.store.clear()


class Login:
    """The F10 window's optional Discord sign-in. Requests run on worker threads, and changed() is called from
    them, so the caller hands it on to the Tk thread."""

    def __init__(self, service, changed, open_url, before_sign_out=lambda: None):
        self.service, self.changed, self.open_url = service, changed, open_url
        self.before_sign_out = before_sign_out  # run while the sign-in still stands (sync.Sync.unlink_owner)
        self.attempt = None  # waiting for approval in the browser
        self.working = ""    # "Starting sign-in..." until the code arrives, "Signing out..."
        self.message = ""    # the last problem, shown under the button
        self._cancel = None
        self._lock = threading.Lock()

    @property
    def user(self):
        session = self.service.session
        return session.user if session else None

    def _run(self, work):
        threading.Thread(target=work, daemon=True, name="discord").start()

    def check(self):
        """A saved sign-in shows at once; this asks the server whether it still stands."""
        if self.service.session is None:
            return

        def work():
            try:
                self.service.me()
            except AuthError:
                pass  # offline: keep it; refused: me() has signed out already
            self.changed()
        self._run(work)

    def sign_in(self):
        with self._lock:
            if self._cancel is not None:
                return
            cancel = self._cancel = threading.Event()
        self.working, self.message = "Starting sign-in...", ""
        self.changed()

        def work():
            try:
                attempt = self.service.start()
                if cancel.is_set():
                    return
                self.attempt, self.working = attempt, ""
                self.changed()
                self.open_url(attempt.url)
                self.service.wait(attempt, cancel)
            except AuthError as error:
                if error.code != "cancelled":
                    self.message = str(error)
            finally:
                with self._lock:
                    self.attempt, self.working, self._cancel = None, "", None
                self.changed()
        self._run(work)

    def reopen(self):
        if self.attempt:
            self.open_url(self.attempt.url)

    def cancel(self):
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()

    def sign_out(self):
        self.cancel()
        self.working, self.message = "Signing out...", ""
        self.changed()

        def work():
            try:
                try:
                    self.before_sign_out()
                except Exception:
                    log.exception("before signing out")
                self.service.logout()
            except AuthError:
                pass  # signed out on this PC all the same; the server's session runs out by itself
            finally:
                self.working = ""
                self.changed()
        self._run(work)


def main():
    parser = argparse.ArgumentParser(description="Check the MaplePrice Discord sign-in service from source.")
    parser.add_argument("--origin", default="https://cozy.markets")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true")
    action.add_argument("--logout", action="store_true")
    args = parser.parse_args()
    service = AuthService(args.origin)
    try:
        if args.logout:
            service.logout()
            print("Signed out of MaplePrice.")
        elif args.status:
            print("Signed in as", service.me().get("name", "Discord user"))
        else:
            attempt = service.start()
            print(f"Approve code {attempt.user_code} at {attempt.url}", flush=True)
            if sys.platform == "win32":
                import subprocess
                subprocess.Popen(["explorer.exe", attempt.url])
            else:
                import webbrowser
                webbrowser.open(attempt.url)
            session = service.wait(attempt)
            print("Connected as", session.user.get("name", "Discord user"))
    except (AuthError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
