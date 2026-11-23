"""Files from outside are untrusted: bad ones must be rejected whole, before touching the database."""

import pytest

from conftest import count
from ledger.recon.load import SettlementFileError, load_bank_file, load_processor_file

HEADER = "txn_id,type,gross_minor,fee_minor,net_minor,currency,txn_date,settled_on\n"


def write(tmp_path, body, header=HEADER, name="processor_2026-01-05.csv"):
    p = tmp_path / name
    p.write_text(header + body)
    return p


def test_valid_file_loads(db, tmp_path):
    p = write(tmp_path, "ch_1,charge,1000,59,941,USD,2026-01-05,2026-01-05\nre_1,refund,200,0,200,USD,2026-01-05,2026-01-05\n")
    with db.begin() as conn:
        assert load_processor_file(conn, p) == 2
    assert count(db, "processor_settlement") == 2


@pytest.mark.parametrize("row,message", [
    ("ch_1,charge,12.50,59,941,USD,2026-01-05,2026-01-05", "whole number of cents"),
    ("ch_1,charge,1000,59,941,USD,05/01/2026,2026-01-05", "YYYY-MM-DD"),
    ("ch_1,transfer,1000,59,941,USD,2026-01-05,2026-01-05", "charge or refund"),
    ("ch_1,charge,1000,59,900,USD,2026-01-05,2026-01-05", "net_minor"),
    ("ch_1,charge,-5,0,-5,USD,2026-01-05,2026-01-05", "negative"),
    ("ch_1,charge,1000,59,941,US,2026-01-05,2026-01-05", "currency"),
])
def test_bad_rows_reject_the_whole_file(db, tmp_path, row, message):
    good = "ch_0,charge,1000,59,941,USD,2026-01-05,2026-01-05\n"
    p = write(tmp_path, good + row + "\n")
    with pytest.raises(SettlementFileError, match=message):
        with db.begin() as conn:
            load_processor_file(conn, p)
    assert count(db, "processor_settlement") == 0  # even the good first row was not loaded


def test_wrong_columns_rejected(db, tmp_path):
    p = write(tmp_path, "x,y\n", header="a,b\n")
    with pytest.raises(SettlementFileError, match="expected columns"):
        with db.begin() as conn:
            load_processor_file(conn, p)


def test_header_only_file_is_valid_and_empty(db, tmp_path):
    with db.begin() as conn:
        assert load_processor_file(conn, write(tmp_path, "")) == 0


def test_bank_file_loads_and_is_idempotent(db, tmp_path):
    p = tmp_path / "bank_2026-01-05.csv"
    p.write_text("bank_txn_id,reference,amount_minor,currency,value_date\nBK-1,po_1,5000,USD,2026-01-05\n")
    with db.begin() as conn:
        assert load_bank_file(conn, p) == 1
    with db.begin() as conn:
        assert load_bank_file(conn, p) == 0
