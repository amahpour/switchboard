"""Passwords on a hosted broker (DESIGN.md §32.4): hashing, one-time passwords, the rules
for a chosen password, and the sign-in limiter.

- ``hash_password``/``verify_password``: the standard library's scrypt (n=2^14, r=8, p=1,
  a 16-byte random salt), stored as ``scrypt$16384$8$1$<salt>$<hash>`` (base64). The
  comparison is constant-time, and a hash in any other shape never verifies.
- ``one_time_password``: 16 Crockford base32 characters in four groups of four (80 bits),
  easy to read out and type: what the owner hands a new person, and the owner's own first
  password, printed once in the broker's log (the claim, §31.3). ``normalize_one_time``
  reads one back as typed (any case, with or without dashes or spaces, I and L for 1, O for 0).
- ``password_problem``: why a chosen password won't do, or None.
- ``SignInLimiter``: failed sign-ins, in memory. After ``FREE_FAILURES`` failures for one
  name within ``WINDOW_S``, that name waits (30 s, doubling to 15 min) before the next try;
  more than ``GLOBAL_FAILURES`` failures a minute over all names pause every password
  sign-in for ``GLOBAL_PAUSE_S``. A success clears the name. Unknown names count like known
  ones, so the answer never says which names exist.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections import deque
from typing import Any

from switchboard.clock import Clock, SystemClock

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_LEN, MAX_LEN = 10, 256
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_FIX = str.maketrans({"I": "1", "L": "1", "O": "0"})
ONE_TIME_CHARS = 16

FREE_FAILURES = 5
WINDOW_S = 15 * 60.0
FIRST_WAIT_S, MAX_WAIT_S = 30.0, 15 * 60.0
GLOBAL_FAILURES = 100
GLOBAL_PAUSE_S = 10.0
MAX_TRACKED = 10_000


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: Any, stored: str | None) -> bool:
    """Whether ``password`` is the one ``stored`` was made from. Never raises."""
    if not isinstance(password, str) or not stored or len(password) > MAX_LEN:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, want = base64.b64decode(parts[4], validate=True), base64.b64decode(parts[5], validate=True)
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P) or len(salt) != 16 or len(want) != 32:
            return False
        got = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def one_time_password() -> str:
    raw = "".join(secrets.choice(CROCKFORD) for _ in range(ONE_TIME_CHARS))
    return "-".join(raw[i : i + 4] for i in range(0, ONE_TIME_CHARS, 4))


def normalize_one_time(text: Any) -> str | None:
    """A one-time password as typed, in its canonical 16 characters, or None."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    s = "".join(text.split()).replace("-", "").upper().translate(_FIX)
    if len(s) != ONE_TIME_CHARS or any(c not in CROCKFORD for c in s):
        return None
    return s


def password_problem(password: Any, name: str) -> str | None:
    """Why ``password`` won't do as someone's own password, or None."""
    if not isinstance(password, str):
        return "a password is required"
    if len(password) < MIN_LEN:
        return f"use at least {MIN_LEN} characters"
    if len(password) > MAX_LEN:
        return f"use at most {MAX_LEN} characters"
    if len(set(password)) < 3:
        return "use more than one or two different characters"
    if password.strip().lower() == name.lower():
        return "don't use your name"
    if normalize_one_time(password) is not None:
        return "choose your own password, not a one-time one"
    return None


class SignInLimiter:
    """Failed password sign-ins per name and overall (module docstring)."""

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or SystemClock()
        self._names: dict[str, tuple[deque[float], float, int]] = {}  # name -> (failures, until, locks)
        self._all: deque[float] = deque()
        self._paused_until = 0.0

    def wait_s(self, name: str) -> float:
        """Seconds until ``name`` may try again (0: now)."""
        now = self.clock.now()
        until = self._paused_until
        e = self._names.get(name.lower())
        if e is not None:
            until = max(until, e[1])
        return max(0.0, until - now)

    def failed(self, name: str) -> None:
        now = self.clock.now()
        key = name.lower()[:64]
        fails, until, locks = self._names.get(key, (deque(), 0.0, 0))
        fails.append(now)
        while fails and fails[0] <= now - WINDOW_S:
            fails.popleft()
        if len(fails) >= FREE_FAILURES:
            until = now + min(FIRST_WAIT_S * 2**locks, MAX_WAIT_S)
            locks += 1
        self._names[key] = (fails, until, locks)
        self._all.append(now)
        while self._all and self._all[0] <= now - 60.0:
            self._all.popleft()
        if len(self._all) > GLOBAL_FAILURES:
            self._paused_until = now + GLOBAL_PAUSE_S
        if len(self._names) > MAX_TRACKED:
            self._prune(now)

    def ok(self, name: str) -> None:
        self._names.pop(name.lower()[:64], None)

    def _prune(self, now: float) -> None:
        for k in [
            k for k, (f, u, _l) in self._names.items() if u <= now and (not f or f[-1] <= now - WINDOW_S)
        ]:
            del self._names[k]
        while len(self._names) > MAX_TRACKED:  # still full: forget the oldest
            del self._names[next(iter(self._names))]


# a hash to check a password against when the name is unknown: the answer takes as long
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
