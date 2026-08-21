"""The password gate.

The app serves transcripts of private conversations, so the interesting
assertions here are the negative ones: what a request without a valid session
must NOT be able to reach.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from telegram_stt import auth

PASSWORD = "correct horse battery staple"


# --- the primitives --------------------------------------------------------

def test_cookie_roundtrips():
    secret = b"k" * 48
    token, _ = auth.issue(secret, PASSWORD, 30)
    assert auth.verify(secret, PASSWORD, token)


@pytest.mark.parametrize("cookie", [
    None, "", "garbage", "not.a.number", "abc.def",
    "9999999999.deadbeef",                 # right shape, forged signature
    "9999999999.",                         # empty signature
])
def test_junk_cookies_rejected(cookie):
    assert not auth.verify(b"k" * 48, PASSWORD, cookie)


def test_expired_cookie_rejected():
    secret = b"k" * 48
    stale = auth._sign(secret, PASSWORD, int(time.time()) - 1)
    assert not auth.verify(secret, PASSWORD, stale)


def test_expiry_cannot_be_extended():
    """The expiry is signed, so pushing it out invalidates the signature."""
    secret = b"k" * 48
    token, _ = auth.issue(secret, PASSWORD, 1)
    _, _, mac = token.partition(".")
    assert not auth.verify(secret, PASSWORD, f"{int(time.time()) + 10**6}.{mac}")


def test_another_installs_key_is_useless():
    token, _ = auth.issue(b"a" * 48, PASSWORD, 30)
    assert not auth.verify(b"b" * 48, PASSWORD, token)


def test_changing_the_password_invalidates_sessions():
    """Rotating the password must sign everyone out, or rotation is theatre."""
    secret = b"k" * 48
    token, _ = auth.issue(secret, PASSWORD, 30)
    assert not auth.verify(secret, "a new password", token)


def test_secret_is_generated_once_and_kept_private(tmp_path):
    path = tmp_path / "nested" / "session.key"
    first = auth.load_secret(path)
    assert len(first) >= 32
    assert auth.load_secret(path) == first, "a new key each start logs everyone out"
    assert path.stat().st_mode & 0o077 == 0, "the signing key must not be group/world readable"


def test_throttle_backs_off_then_forgives():
    throttle = auth.Throttle(max_attempts=3)
    for _ in range(3):
        throttle.record_failure("10.0.0.1")
    assert throttle.delay_for("10.0.0.1") > 0
    assert throttle.delay_for("10.0.0.2") == 0, "one client must not throttle another"
    throttle.clear("10.0.0.1")
    assert throttle.delay_for("10.0.0.1") == 0


def test_throttle_is_bounded():
    """A delay that grows without limit would wedge a handler thread."""
    throttle = auth.Throttle(max_attempts=1)
    for _ in range(200):
        throttle.record_failure("10.0.0.1")
    assert throttle.delay_for("10.0.0.1") <= 30


def test_cookie_flags():
    header = auth.cookie_header("v", int(time.time()) + 60, secure=True)
    assert "HttpOnly" in header, "readable by script means stealable by script"
    assert "SameSite=Lax" in header
    assert "Secure" in header
    assert "Secure" not in auth.cookie_header("v", int(time.time()) + 60, secure=False)


def test_hint_cookie_carries_no_secret():
    header = auth.hint_header(int(time.time()) + 60, secure=False)
    assert "HttpOnly" not in header      # the page must be able to read it
    token, expires = auth.issue(b"k" * 48, PASSWORD, 30)
    assert token.split(".")[1] not in header


# --- the gate, over real HTTP ----------------------------------------------

@pytest.fixture
def locked(server, monkeypatch):
    """The `server` fixture's bot, re-gated with a password."""
    from telegram_stt import web

    base, bot = server
    gate = web.gate_for(bot)
    gate.password = PASSWORD
    gate.secret = b"k" * 48
    gate.throttle = auth.Throttle()
    yield base, bot, gate


def _get(url, cookie=None, method="GET", data=None):
    request = urllib.request.Request(url, method=method, data=data)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _session(gate):
    token, _ = auth.issue(gate.secret, gate.password, 30)
    return f"{auth.COOKIE}={token}"


# Every route that reveals something about a recording. If a route is added
# without a gate, it belongs here and this test should fail.
PROTECTED = [
    "/", "/index.html",
    "/api/status", "/api/history", "/api/search?q=a", "/api/summary-queue",
    "/api/transcript/x", "/api/summary/x", "/api/summary-status/x",
    "/api/audio/x", "/api/download/text/x", "/api/download/audio/x",
    "/api/download/summary/x", "/api/download/docx/x", "/api/download/json/x",
]


@pytest.mark.parametrize("route", PROTECTED)
def test_no_session_reaches_nothing(locked, route):
    base, _, _ = locked
    status, body, _ = _get(base + route)
    if route.startswith("/api/"):
        assert status == 401, f"{route} answered without a session"
        assert json.loads(body)["error"]
    else:
        # A browser gets the form rather than a bare 401 it cannot act on.
        assert status == 200 and b"<form" in body and b"password" in body
    assert b"transcript" not in body.lower() or route in ("/", "/index.html")


POST_ROUTES = [
    "/api/summarize/x", "/api/summarize-batch", "/api/summarize-cancel/x",
    "/api/summarize-cancel-all", "/api/prune",
    "/api/record/x/update", "/api/record/x/delete", "/api/record/x/purge",
    "/api/record/x/restore", "/api/record/x/describe",
]


@pytest.mark.parametrize("route", POST_ROUTES)
def test_no_session_cannot_mutate(locked, route):
    """Deleting and purging are destructive; they must be behind the gate."""
    base, _, _ = locked
    status, _, _ = _get(base + route, method="POST", data=b'{"ids":[]}')
    assert status == 401, f"{route} accepted an unauthenticated POST"


def test_real_record_is_not_reachable_without_a_session(locked):
    """Not just 401 on a made-up id — the actual archived transcript."""
    base, bot, _ = locked
    record = bot.archive.records()[0]
    stem = record["text_file"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    for route in (f"/api/transcript/{stem}", f"/api/download/text/{stem}",
                  f"/api/audio/{stem}"):
        status, body, _ = _get(base + route)
        assert status == 401
        assert b"the quick brown fox" not in body.lower()


@pytest.mark.parametrize("cookie", [
    f"{auth.COOKIE}=forged",
    f"{auth.COOKIE}=9999999999.0000",
    "stt_signed_in=1",                      # the hint cookie is not a session
    "",
])
def test_forged_cookies_rejected(locked, cookie):
    base, _, _ = locked
    status, _, _ = _get(base + "/api/history", cookie=cookie)
    assert status == 401


def test_a_valid_session_gets_through(locked):
    base, _, gate = locked
    status, body, _ = _get(base + "/api/history", cookie=_session(gate))
    assert status == 200
    assert "records" in json.loads(body)


def test_login_sets_a_working_session(locked):
    import urllib.parse

    base, _, gate = locked
    form = urllib.parse.urlencode({"password": PASSWORD, "next": "/"}).encode()
    request = urllib.request.Request(
        base + "/login", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request)
        status, headers = response.status, response.headers
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, exc.headers

    assert status == 303
    cookies = headers.get_all("Set-Cookie") or []
    session = next(c for c in cookies if c.startswith(auth.COOKIE))
    assert "HttpOnly" in session
    # and it actually opens the door
    got, body, _ = _get(base + "/api/history", cookie=session.split(";")[0])
    assert got == 200


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_wrong_password_is_refused_and_counted(locked):
    import urllib.parse

    base, _, gate = locked
    form = urllib.parse.urlencode({"password": "wrong", "next": "/"}).encode()
    status, body, headers = _get(base + "/login", method="POST", data=form)
    assert status == 401
    assert b"not right" in body
    assert not any(
        c.startswith(auth.COOKIE) and len(c.split("=")[1].split(";")[0]) > 1
        for c in (headers.get("Set-Cookie") or "").split("\n")
    )
    assert gate.throttle.failures, "a failed attempt must be recorded"


def test_open_redirect_is_not_possible(locked):
    """?next= must not be able to bounce a signed-in browser off-site."""
    import urllib.parse

    base, _, _ = locked
    for hostile in ("https://evil.example/", "//evil.example/", "/\\evil"):
        form = urllib.parse.urlencode(
            {"password": PASSWORD, "next": hostile}
        ).encode()
        opener = urllib.request.build_opener(_NoRedirect())
        request = urllib.request.Request(
            base + "/login", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            location = opener.open(request).headers.get("Location")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
        assert not location.startswith("http"), f"{hostile} escaped"
        assert not location.startswith("//"), f"{hostile} escaped"


def test_login_page_does_not_reflect_script(locked):
    base, _, _ = locked
    status, body, _ = _get(base + "/login?next=" + "%22%3E%3Cscript%3E")
    assert b"<script>" not in body


def test_logout_clears_the_session(locked):
    base, _, gate = locked
    cookie = _session(gate)
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(base + "/logout", method="POST", data=b"")
    request.add_header("Cookie", cookie)
    try:
        headers = opener.open(request).headers
    except urllib.error.HTTPError as exc:
        headers = exc.headers
    cleared = [c for c in (headers.get_all("Set-Cookie") or [])]
    assert any(f"{auth.COOKIE}=;" in c and "Max-Age=0" in c for c in cleared)
    assert any(f"{auth.HINT}=;" in c for c in cleared)


def test_security_headers_present(locked):
    base, _, gate = locked
    _, _, headers = _get(base + "/", cookie=_session(gate))
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


def test_rejected_post_does_not_desync_the_connection(locked):
    """A body left unread would be parsed as the next request on the socket."""
    import http.client

    host, _, port = base_parts = locked[0].removeprefix("http://").partition(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("POST", "/api/summarize-batch", body=b'{"ids":["a","b","c"]}',
                 headers={"Content-Type": "application/json"})
    assert conn.getresponse().read() is not None
    # Same connection, second request: it must be understood as a request.
    conn.request("GET", "/api/history")
    second = conn.getresponse()
    assert second.status == 401
    conn.close()


def test_no_password_leaves_the_app_open(server):
    """The loopback default must keep working untouched."""
    from telegram_stt import web

    base, bot = server
    assert not web.gate_for(bot).enabled
    status, _, _ = _get(base + "/api/history")
    assert status == 200


def test_forwarded_for_is_only_trusted_from_loopback(locked):
    """Throttling must bucket by real client, not by the proxy."""
    base, _, gate = locked
    handler = type("H", (), {})()
    from telegram_stt.web import _Handler

    handler.__class__ = _Handler
    handler.client_address = ("127.0.0.1", 1)
    handler.headers = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    assert _Handler._client(handler) == "203.0.113.9"

    handler.client_address = ("192.168.1.5", 1)
    assert _Handler._client(handler) == "192.168.1.5", (
        "a non-loopback peer could forge X-Forwarded-For"
    )


def test_throttle_memory_is_bounded():
    """A rotating X-Forwarded-For must not grow the map without limit."""
    throttle = auth.Throttle(max_tracked=64)
    for n in range(5000):
        throttle.record_failure(f"198.51.100.{n}")
    assert len(throttle.failures) <= 64


def test_oversized_login_body_does_not_desync(locked):
    import http.client

    host, _, port = locked[0].removeprefix("http://").partition(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("POST", "/login", body=b"password=x&junk=" + b"a" * 50_000,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert conn.getresponse().read() is not None
    conn.request("GET", "/api/history")
    assert conn.getresponse().status == 401
    conn.close()
