"""
Configuration loading.

WHY THIS FILE EXISTS: every other module needs the database URL, the webhook secret
and the PII key. Reading them in ONE place means (a) no secret is ever hard-coded,
(b) a missing setting fails fast with a clear message instead of a confusing crash
later, and (c) the "test mode only" safety rail lives in exactly one spot.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing or unsafe."""


def _env(name: str) -> str | None:
    load_dotenv()  # reads .env if present; never overrides variables already set
    value = os.getenv(name)
    return value if value else None


def get_db_url() -> str:
    """Return a SQLAlchemy URL.

    Either set LEDGER_DATABASE_URL directly, or set POSTGRES_USER / POSTGRES_PASSWORD /
    POSTGRES_DB (and optionally POSTGRES_HOST / POSTGRES_PORT).
    """
    direct = _env("LEDGER_DATABASE_URL")
    if direct:
        return direct

    missing = [k for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not _env(k)]
    if missing:
        raise ConfigError(
            f"Missing database settings: {', '.join(missing)}. Copy .env.example to .env and fill it in."
        )
    user = quote_plus(_env("POSTGRES_USER"))
    password = quote_plus(_env("POSTGRES_PASSWORD"))
    host = _env("POSTGRES_HOST") or "localhost"
    port = _env("POSTGRES_PORT") or "5432"
    db = _env("POSTGRES_DB")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def get_stripe_settings() -> dict:
    """Return Stripe settings, REFUSING anything that is not test mode.

    Safety rail: a live key (sk_live_...) could move real money. This project only
    ever needs test keys, so a live key is treated as a configuration error.
    """
    api_key = _env("STRIPE_API_KEY")
    if not api_key:
        raise ConfigError("STRIPE_API_KEY is not set (use a TEST key: sk_test_...).")
    if not api_key.startswith("sk_test_"):
        raise ConfigError("Refusing to start: STRIPE_API_KEY must be a TEST key (sk_test_...).")
    return {"api_key": api_key, "webhook_secret": get_webhook_secret()}


def assert_stripe_test_mode() -> None:
    """If a Stripe API key is configured at all, it must be a test key."""
    key = _env("STRIPE_API_KEY")
    if key and not key.startswith("sk_test_"):
        raise ConfigError("Refusing to start: STRIPE_API_KEY must be a TEST key (sk_test_...).")


def get_webhook_secret() -> str:
    """The signing secret Stripe shows for your webhook endpoint (whsec_...)."""
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise ConfigError("STRIPE_WEBHOOK_SECRET is not set (whsec_...).")
    return secret


def get_pii_key() -> bytes:
    """Secret key used to tokenize personal data (HMAC). Keep it out of git."""
    key = _env("PII_TOKEN_KEY")
    if not key:
        raise ConfigError("PII_TOKEN_KEY is not set. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    return key.encode("utf-8")
