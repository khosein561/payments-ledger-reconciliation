"""
Command-line interface:  python -m ledger <command>

WHY THIS FILE EXISTS: operators and demos need a simple way to run each stage by
hand. The CLI is a thin layer over the same functions the DAG and tests use.

    migrate     create / upgrade the database schema and seed accounts
    demo        end-to-end: fake events -> ledger -> settlement files with planted
                errors -> reconciliation -> exception report -> score vs the answer key
    recon       reconcile one date or a range from already-loaded files
    replay      retry a dead-letter event after fixing its cause
    serve       run the webhook API (uvicorn)
    reset       DEV ONLY: wipe all tables (the ledger is otherwise append-only)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text

from .core.accounts import seed_accounts
from .db import get_engine
from .dq import run_dq_checks
from .generators.settlement_files import generate_settlement_files
from .generators.simulate import SimConfig, build_events, post_events
from .ingest.service import replay_dead_letter
from .migrate import apply_migrations
from .pipeline import load_settlement, publish, reconcile_date
from .recon.evaluate import score_against_plan, to_markdown

DEMO_PII_KEY = b"demo-key-not-a-secret"  # demo data is fake; real deployments set PII_TOKEN_KEY
ALL_TABLES = (
    "recon_breaks, recon_matches, recon_runs, bank_settlement, processor_settlement, dead_letter_events, "
    "webhook_events, audit_log, postings, journal_entries, accounts"
)


def _d(value: str) -> date:
    return date.fromisoformat(value)


def cmd_migrate(args) -> int:
    engine = get_engine()
    applied = apply_migrations(engine)
    with engine.begin() as conn:
        seeded = seed_accounts(conn)
    print(f"migrations applied: {applied or 'none (already up to date)'}; accounts seeded: {seeded}")
    return 0


def cmd_demo(args) -> int:
    engine = get_engine()
    apply_migrations(engine)
    with engine.begin() as conn:
        seed_accounts(conn)

    cfg = SimConfig(seed=args.seed, start=args.start, days=args.days, charges_per_day=args.per_day)
    end = cfg.start + timedelta(days=cfg.days - 1)
    out_dir = Path(args.out_dir)

    events = build_events(cfg)
    outcomes = post_events(engine, events, DEMO_PII_KEY)
    print(f"1. posted {len(events)} fake events: {dict(outcomes)}")

    with engine.begin() as conn:
        plan = generate_settlement_files(conn, cfg.start, end, out_dir, seed=args.seed)
    print(f"2. wrote settlement files to {out_dir}/ with {len(plan['planted'])} planted discrepancies")

    for i in range(cfg.days):
        d = cfg.start + timedelta(days=i)
        load_settlement(engine, d, out_dir)
        reconcile_date(engine, d)
    print(f"3. loaded files and reconciled {cfg.days} days")

    as_of = end + timedelta(days=1)
    df, paths = publish(engine, as_of, out_dir / "reports")
    print(f"4. exception report: {paths['markdown']} ({len(df)} open breaks)")

    with engine.begin() as conn:
        score = score_against_plan(conn, plan, cfg.start, end)
        checks = run_dq_checks(conn)
    print("5. results vs the answer key:\n")
    print(to_markdown(score))
    print("\n   data-quality checks:", ", ".join(f"{c.name}={'ok' if c.passed else 'FAIL'}" for c in checks))
    total = score.iloc[-1]
    return 0 if (total["false_positive"] == 0 and total["missed"] == 0) else 1


def cmd_recon(args) -> int:
    engine = get_engine()
    start = args.date or args.start
    end = args.date or args.end or start
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        run_id = reconcile_date(engine, d)
        print(f"reconciled {d} (run {run_id})")
    return 0


def cmd_replay(args) -> int:
    print(f"replay result: {replay_dead_letter(get_engine(), args.id)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("ledger.ingest.webhook_app:app", host=args.host, port=args.port)
    return 0


def cmd_reset(args) -> int:
    if not args.i_understand_this_deletes_everything:
        print("refusing: pass --i-understand-this-deletes-everything (development databases only)")
        return 2
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        seed_accounts(conn)
    print("all tables truncated and accounts re-seeded")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply schema migrations and seed accounts").set_defaults(func=cmd_migrate)

    p = sub.add_parser("demo", help="run the full end-to-end demonstration")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--per-day", type=int, default=60, help="charges per day")
    p.add_argument("--start", type=_d, default=date(2026, 1, 5))
    p.add_argument("--out-dir", default="data/settlement")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("recon", help="reconcile a date or range from loaded files")
    p.add_argument("--date", type=_d)
    p.add_argument("--start", type=_d)
    p.add_argument("--end", type=_d)
    p.set_defaults(func=cmd_recon)

    p = sub.add_parser("replay", help="replay a dead-letter event by id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("serve", help="run the webhook API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("reset", help="DEV ONLY: truncate every table")
    p.add_argument("--i-understand-this-deletes-everything", action="store_true")
    p.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "recon" and not (args.date or args.start):
        print("recon needs --date or --start", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
