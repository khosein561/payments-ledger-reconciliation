"""
FastAPI service that receives Stripe webhooks.

WHY THIS FILE EXISTS: it is the front door. Its only jobs are HTTP concerns:
  1. read the RAW request body (the signature is computed over the exact bytes, so
     we must not let a JSON parser re-serialize them first),
  2. verify the signature,
  3. hand the event to `process_event`,
  4. translate the outcome into the right HTTP status.
Everything else lives elsewhere so it can be tested without a web server.

Status codes:
    200  processed / ignored / duplicate / parked in dead letter (all "we have it")
    400  bad signature or malformed body (sender should NOT retry)
    503  database unavailable (sender SHOULD retry)
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from ..config import assert_stripe_test_mode, get_webhook_secret
from ..db import get_engine
from .service import process_event
from .signature import SignatureError, verify_stripe_signature

log = logging.getLogger("ledger.webhook")


def create_app(engine: Engine | None = None, webhook_secret: str | None = None, pii_key: bytes | None = None) -> FastAPI:
    """Application factory. Tests pass their own engine/secret; production reads the environment."""
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        assert_stripe_test_mode()  # refuse to boot with a live Stripe key configured
        yield

    app = FastAPI(title="Payments ledger webhook receiver", lifespan=lifespan)

    def _engine() -> Engine:
        return engine or get_engine()

    @app.post("/webhook")
    async def receive_webhook(request: Request) -> dict:
        payload = await request.body()  # raw bytes, exactly as signed
        signature = request.headers.get("stripe-signature", "")

        try:
            verify_stripe_signature(payload, signature, webhook_secret or get_webhook_secret())
        except SignatureError as exc:
            log.warning("rejected webhook: %s", exc)
            raise HTTPException(status_code=400, detail="invalid signature") from None

        try:
            event = json.loads(payload)
            if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
                raise ValueError("event must be an object with string 'id' and 'type'")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"malformed event: {exc}") from None

        try:
            # The DB driver is blocking; run it in a worker thread so one slow query
            # does not freeze the whole async server.
            status = await run_in_threadpool(process_event, _engine(), event, pii_key)
        except OperationalError:
            log.error("database unavailable while processing %s", event["id"])
            raise HTTPException(status_code=503, detail="database unavailable, please retry") from None

        return {"status": status}

    @app.get("/healthz")
    def healthz() -> dict:
        try:
            with _engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=503, detail="database unavailable") from None
        return {"status": "ok"}

    return app


# Module-level app for `uvicorn ledger.ingest.webhook_app:app`. Settings are read
# lazily (on first request), so merely importing this module needs no environment.
app = create_app()
