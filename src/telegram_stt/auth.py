"""Password gate for the web app.

Only needed once the app is reachable from outside the machine. On loopback or
a tailnet the network is the boundary; on the public internet it is this.

A single shared password, exchanged for a signed cookie. There is one user, so
accounts and password hashing per-user would be ceremony; what actually matters
is that the cookie cannot be forged, the comparison does not leak timing, and
guessing is slow.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

COOKIE = "stt_session"
# A companion cookie carrying no secret and readable by JavaScript, purely so
# the page knows whether to offer a sign-out button. The real cookie is
# HttpOnly and deliberately invisible to the page.
HINT = "stt_signed_in"
_LOCK = threading.Lock()


def load_secret(path: Path) -> bytes:
    """A per-install signing key, generated once and kept out of the repo.

    Deriving it from the password instead would invalidate every session the
    moment the password changed — which is right — but would also mean the key
    is only as unpredictable as the password. This is independent of both.
    """
    with _LOCK:
        if path.is_file():
            existing = path.read_bytes().strip()
            if len(existing) >= 32:
                return existing
        secret = secrets.token_bytes(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secret)
        os.chmod(path, 0o600)
        log.info("generated a new session key at %s", path)
        return secret


def _sign(secret: bytes, password: str, expires: int) -> str:
    # The password is part of the signed material, so changing it invalidates
    # every outstanding session without touching the key.
    payload = f"{expires}".encode()
    key = hmac.new(secret, password.encode(), hashlib.sha256).digest()
    mac = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def issue(secret: bytes, password: str, lifetime_days: int) -> tuple[str, int]:
    expires = int(time.time()) + max(1, lifetime_days) * 86400
    return _sign(secret, password, expires), expires


def verify(secret: bytes, password: str, cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    raw_expires, _, _ = cookie.partition(".")
    try:
        expires = int(raw_expires)
    except ValueError:
        return False
    if expires < time.time():
        return False
    expected = _sign(secret, password, expires)
    return hmac.compare_digest(expected, cookie)


def check_password(configured: str, offered: str) -> bool:
    """Constant-time, so a wrong guess takes as long as a right one."""
    return hmac.compare_digest(configured.encode(), (offered or "").encode())


@dataclass
class Throttle:
    """Slows down guessing, per client address.

    Not a lockout: locking an address out lets anyone lock the owner out too.
    A delay that grows with failures makes a brute force impractical while
    leaving a fat-fingered password merely annoying.
    """

    max_attempts: int = 5
    window_seconds: int = 900
    # A client that can vary its apparent address (a rotating X-Forwarded-For)
    # would otherwise grow this dict for the whole window.
    max_tracked: int = 4096
    failures: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _prune(self, now: float) -> None:
        for address, times in list(self.failures.items()):
            recent = [t for t in times if now - t < self.window_seconds]
            if recent:
                self.failures[address] = recent
            else:
                self.failures.pop(address, None)

    def delay_for(self, address: str) -> float:
        now = time.time()
        with self._lock:
            self._prune(now)
            count = len(self.failures.get(address, []))
        if count < self.max_attempts:
            return 0.0
        return min(2 ** (count - self.max_attempts), 30)

    def record_failure(self, address: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(now)
            if (len(self.failures) >= self.max_tracked
                    and address not in self.failures):
                # Drop whoever failed longest ago; they are closest to expiry
                # anyway, so this costs the least real throttling.
                oldest = min(self.failures, key=lambda a: self.failures[a][-1])
                self.failures.pop(oldest, None)
            self.failures.setdefault(address, []).append(now)

    def clear(self, address: str) -> None:
        with self._lock:
            self.failures.pop(address, None)


def cookie_header(value: str, expires: int, secure: bool) -> str:
    parts = [
        f"{COOKIE}={value}",
        "Path=/",
        "HttpOnly",                     # unreadable from JavaScript
        "SameSite=Lax",                 # not sent on cross-site POSTs
        f"Max-Age={max(0, expires - int(time.time()))}",
    ]
    if secure:
        parts.append("Secure")          # HTTPS only
    return "; ".join(parts)


def hint_header(expires: int, secure: bool) -> str:
    parts = [f"{HINT}=1", "Path=/", "SameSite=Lax",
             f"Max-Age={max(0, expires - int(time.time()))}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header(secure: bool) -> list[str]:
    """Both cookies, expired. Returns one value per Set-Cookie header."""
    out = []
    for name, extra in ((COOKIE, ["HttpOnly"]), (HINT, [])):
        parts = [f"{name}=", "Path=/", "SameSite=Lax", "Max-Age=0", *extra]
        if secure:
            parts.append("Secure")
        out.append("; ".join(parts))
    return out


def read_cookie(header: str | None) -> str | None:
    for chunk in (header or "").split(";"):
        name, _, value = chunk.strip().partition("=")
        if name == COOKIE:
            return value
    return None
