"""Request signing for the Hotel TV wire (docs/HA-PAIRING.md §3, §4).

`X-HotelTV-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(key, base)>` where
`base = t + "." + id + "." + METHOD + "." + PATH + "." + raw_body`, `id` is the
X-HotelTV-Id header and PATH the fixed logical path `/hotel_tv/webhook` (never the
real URL path). The HMAC key is the key string's UTF-8 bytes: the connection
`secret` for the server, the derived TV key for a TV.

Pure module: no Home Assistant imports, so it can move to a PyPI library later.
"""

from __future__ import annotations

import hashlib
import hmac

# The logical path of the webhook (§3).
PATH_WEBHOOK = "/hotel_tv/webhook"
# Prefix of the message a TV key is derived from (§4).
TV_KEY_PREFIX = "hotel_tv/tv/"


def tv_key(secret: str, device_id: str) -> str:
    """The key of one TV: hex(HMAC-SHA256(secret, "hotel_tv/tv/" + device_id))."""
    return hmac.new(
        secret.encode("utf-8"),
        (TV_KEY_PREFIX + device_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signature_base(
    t: int, message_id: str, method: str, path: str, body: bytes
) -> bytes:
    """Return the exact bytes that are signed."""
    return f"{t}.{message_id}.{method.upper()}.{path}.".encode() + body


def compute_signature(
    key: str, t: int, message_id: str, method: str, path: str, body: bytes
) -> str:
    """Return the hex HMAC-SHA256 of the signature base."""
    return hmac.new(
        key.encode("utf-8"),
        signature_base(t, message_id, method, path, body),
        hashlib.sha256,
    ).hexdigest()


def signature_header(
    key: str, t: int, message_id: str, method: str, path: str, body: bytes
) -> str:
    """Return the value of the X-HotelTV-Signature header."""
    return f"t={t},v1={compute_signature(key, t, message_id, method, path, body)}"


def parse_signature_header(value: str | None) -> tuple[int, list[str]] | None:
    """Parse `t=..,v1=..` into (t, [v1, ...]); None when malformed."""
    if not value:
        return None
    t: int | None = None
    sigs: list[str] = []
    for part in value.split(","):
        name, sep, val = part.strip().partition("=")
        if not sep:
            return None
        if name == "t":
            try:
                t = int(val)
            except ValueError:
                return None
        elif name == "v1":
            sigs.append(val.strip().lower())
    if t is None or not sigs:
        return None
    return t, sigs


def verify_signature(
    key: str,
    header: str | None,
    message_id: str,
    method: str,
    path: str,
    body: bytes,
    now: float,
    tolerance: int = 300,
) -> int | None:
    """Verify a signature header; the signed `t` when valid, else None.

    Constant-time compare; |now - t| must be within `tolerance` seconds.
    """
    parsed = parse_signature_header(header)
    if parsed is None or not message_id or not key:
        return None
    t, sigs = parsed
    if abs(now - t) > tolerance:
        return None
    expected = compute_signature(key, t, message_id, method, path, body)
    ok = False
    for sig in sigs:
        # Evaluate every candidate; never short-circuit on a match.
        ok |= hmac.compare_digest(expected, sig)
    return t if ok else None


class ReplayGuard:
    """The message ids accepted within the tolerance window (§3: `replay`).

    A signed message is valid while |now - t| <= tolerance, so its id is kept
    until t + tolerance; after that the signature check refuses it anyway.
    """

    def __init__(self, tolerance: int = 300, max_entries: int = 10000) -> None:
        """Initialize."""
        self._tolerance = tolerance
        self._max = max_entries
        self._seen: dict[str, float] = {}

    def _prune(self, now: float) -> None:
        self._seen = {k: v for k, v in self._seen.items() if v >= now}
        if len(self._seen) > self._max:
            keep = sorted(self._seen.items(), key=lambda kv: kv[1])[-self._max :]
            self._seen = dict(keep)

    def check_and_remember(self, message_id: str, t: int, now: float) -> bool:
        """True (and remembered) the first time; False for a repeat."""
        expires = self._seen.get(message_id)
        if expires is not None and expires >= now:
            return False
        if len(self._seen) >= self._max:
            self._prune(now)
        self._seen[message_id] = max(t, now) + self._tolerance
        return True

    def __len__(self) -> int:
        """The number of remembered ids."""
        return len(self._seen)
