"""
PII masking and tokenization.

WHY THIS FILE EXISTS: payment events contain personal data (names, emails, addresses).
The ledger does not need any of it to be correct, and every copy of personal data is a
liability. So we redact BEFORE storing: analysts and logs never see the raw values.

  mask_email  -> human-readable but unidentifiable ("a***@example.com")
  tokenize    -> a stable pseudonym (same input = same token), so you can still
                 count "how many events did customer X generate?" without knowing X.
                 It is a keyed HMAC, so nobody can reverse or brute-force the token
                 without the secret key.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from typing import Any

# Keys whose values are personal data in a Stripe-shaped payload.
PII_KEYS = {
    "email", "receipt_email", "customer_email",
    "name", "phone", "address", "line1", "line2", "city", "postal_code",
    "ip_address", "tax_id", "card",
}


def mask_email(email: str) -> str:
    """'alice@example.com' -> 'a***@example.com'. Anything malformed becomes '***'."""
    if not isinstance(email, str) or email.count("@") != 1:
        return "***"
    local, domain = email.split("@")
    if not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def tokenize(value: str, key: bytes) -> str:
    """Deterministic keyed pseudonym: 'tok_' + first 24 hex chars of HMAC-SHA256."""
    digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return "tok_" + digest[:24]


def redact_payload(payload: Any, key: bytes) -> Any:
    """Return a deep copy of `payload` with personal data replaced.

    Emails are masked; every other PII field is replaced by a token. Non-PII fields
    (amounts, ids, timestamps) are untouched because the ledger needs them.
    """
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in PII_KEYS and v is not None:
                    if "email" in k and isinstance(v, str):
                        out[k] = mask_email(v)
                    else:
                        raw = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
                        out[k] = tokenize(raw, key)
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(copy.deepcopy(payload))
