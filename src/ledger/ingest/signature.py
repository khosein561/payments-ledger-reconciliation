"""
Webhook signature verification (Stripe's scheme).

WHY THIS FILE EXISTS: a webhook URL is public. Anyone could POST a fake "payment
succeeded" event to it. Stripe signs every delivery with a secret only you and Stripe
know, so the first thing we do is check that signature. No valid signature = we do not
even parse the body.

The scheme (documented by Stripe):
    header:   Stripe-Signature: t=<unix timestamp>,v1=<hex signature>[,v1=<another>]
    signed:   f"{t}.{raw_body}"
    signature = HMAC-SHA256(key=webhook_secret, message=signed), hex-encoded
We also reject timestamps older than a tolerance window, which stops "replay" of a
captured-but-valid request weeks later.

(The official `stripe` library's `Webhook.construct_event` does the same; this is
written out by hand so you can see and test every step.)
"""

from __future__ import annotations

import hashlib
import hmac
import time

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """The webhook signature is missing, malformed, stale, or wrong."""


def compute_signature(payload: bytes, secret: str, timestamp: int) -> str:
    signed = f"{timestamp}.".encode("utf-8") + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def make_signature_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a valid header. Used by tests and by the local simulator."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={compute_signature(payload, secret, ts)}"


def verify_stripe_signature(
    payload: bytes,
    header: str,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Raise SignatureError unless `header` is a valid, fresh signature for `payload`."""
    if not header:
        raise SignatureError("missing Stripe-Signature header")

    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                raise SignatureError("malformed timestamp in signature header") from None
        elif key == "v1":
            candidates.append(value)

    if timestamp is None or not candidates:
        raise SignatureError("signature header must contain t= and v1=")

    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        raise SignatureError("timestamp outside the tolerance window (possible replay)")

    expected = compute_signature(payload, secret, timestamp)
    # compare_digest takes constant time, so an attacker can't learn the signature
    # one character at a time by measuring how long the comparison took.
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise SignatureError("signature does not match")
