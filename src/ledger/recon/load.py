"""
Load settlement CSV files into the database, safely.

WHY THIS FILE EXISTS: files from outside your company are untrusted input. A column
renamed, a "12.50" where cents were expected, or a garbled date should stop the load
with a clear message BEFORE any bad row reaches the tables. And because DAGs get
re-run, loading the same file twice must change nothing: (source_file, row_num) is a
unique key and inserts use ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import Connection, text

from ..generators.settlement_files import BANK_COLUMNS, PROCESSOR_COLUMNS


class SettlementFileError(ValueError):
    """The file is malformed. Nothing from it is loaded."""


def _int(value: str, field: str, path: Path, line: int, allow_negative: bool = False) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SettlementFileError(
            f"{path.name} line {line}: {field} must be a whole number of cents, got {value!r}"
        ) from None
    if n < 0 and not allow_negative:
        raise SettlementFileError(f"{path.name} line {line}: {field} must not be negative, got {n}")
    return n


def _date(value: str, field: str, path: Path, line: int) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise SettlementFileError(f"{path.name} line {line}: {field} must be YYYY-MM-DD, got {value!r}") from None


def _read(path: Path, columns: list[str]) -> list[tuple[int, dict]]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or set(reader.fieldnames) != set(columns):
            raise SettlementFileError(f"{path.name}: expected columns {columns}, found {reader.fieldnames}")
        return [(i, row) for i, row in enumerate(reader, start=2)]  # line 1 is the header


def parse_processor_file(path: Path) -> list[dict]:
    """Validate every row; return typed dicts. Raises SettlementFileError on the first problem."""
    path = Path(path)
    out = []
    for line, row in _read(path, PROCESSOR_COLUMNS):
        if row["type"] not in ("charge", "refund"):
            raise SettlementFileError(f"{path.name} line {line}: type must be charge or refund, got {row['type']!r}")
        gross = _int(row["gross_minor"], "gross_minor", path, line)
        fee = _int(row["fee_minor"], "fee_minor", path, line)
        net = _int(row["net_minor"], "net_minor", path, line, allow_negative=True)
        if net != gross - fee:
            raise SettlementFileError(f"{path.name} line {line}: net_minor {net} != gross {gross} - fee {fee}")
        if len(row["currency"]) != 3:
            raise SettlementFileError(f"{path.name} line {line}: currency must be 3 letters")
        out.append({
            "row_num": line, "txn_id": row["txn_id"], "type": row["type"], "gross_minor": gross,
            "fee_minor": fee, "net_minor": net, "currency": row["currency"],
            "txn_date": _date(row["txn_date"], "txn_date", path, line),
            "settled_on": _date(row["settled_on"], "settled_on", path, line),
        })
    return out


def parse_bank_file(path: Path) -> list[dict]:
    path = Path(path)
    out = []
    for line, row in _read(path, BANK_COLUMNS):
        if len(row["currency"]) != 3:
            raise SettlementFileError(f"{path.name} line {line}: currency must be 3 letters")
        out.append({
            "row_num": line, "bank_txn_id": row["bank_txn_id"], "reference": row["reference"],
            "amount_minor": _int(row["amount_minor"], "amount_minor", path, line, allow_negative=True),
            "currency": row["currency"], "value_date": _date(row["value_date"], "value_date", path, line),
        })
    return out


def _count(conn: Connection, table: str, source_file: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table} WHERE source_file = :f"), {"f": source_file}).scalar_one()


def load_processor_file(conn: Connection, path: Path) -> int:
    """Insert the file's rows; return how many were NEW (0 if it was loaded before)."""
    rows = parse_processor_file(path)  # validate everything first
    name = Path(path).name
    before = _count(conn, "processor_settlement", name)
    if rows:
        conn.execute(
            text(
                """
                INSERT INTO processor_settlement
                    (source_file, row_num, txn_id, type, gross_minor, fee_minor, net_minor, currency, txn_date, settled_on)
                VALUES (:source_file, :row_num, :txn_id, :type, :gross_minor, :fee_minor, :net_minor, :currency,
                        :txn_date, :settled_on)
                ON CONFLICT (source_file, row_num) DO NOTHING
                """
            ),
            [{**r, "source_file": name} for r in rows],
        )
    return _count(conn, "processor_settlement", name) - before


def load_bank_file(conn: Connection, path: Path) -> int:
    rows = parse_bank_file(path)
    name = Path(path).name
    before = _count(conn, "bank_settlement", name)
    if rows:
        conn.execute(
            text(
                """
                INSERT INTO bank_settlement
                    (source_file, row_num, bank_txn_id, reference, amount_minor, currency, value_date)
                VALUES (:source_file, :row_num, :bank_txn_id, :reference, :amount_minor, :currency, :value_date)
                ON CONFLICT (source_file, row_num) DO NOTHING
                """
            ),
            [{**r, "source_file": name} for r in rows],
        )
    return _count(conn, "bank_settlement", name) - before
