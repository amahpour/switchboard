"""The v10 to v11 migration (#178): machines gain who approved them; every machine already approved
counts as the owner's approval, and keeps its row. (Schema 29 drops the machines' table later, #385: this
tests the step on a copy, and that the whole path to the current schema still keeps every other row.)"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_machines_gain_who_approved_them_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    for stmt in (
        *db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9, *db.V9_TO_V10,
    ):  # fmt: skip
        con.execute(stmt)
    con.commit()
    assert db.schema_version(con) == 10
    before = db.row_counts(con, db.kept(db.V14_TABLES))
    # the step itself, on a copy: machines gain who paired and who approved them, and the rows stay
    step = sqlite3.connect(":memory:")
    con.backup(step)
    machines = step.execute("SELECT COUNT(*) FROM link_machines").fetchone()[0]
    assert machines > 0
    for stmt in db.V10_TO_V11:
        step.execute(stmt)
    cols = [r[1] for r in step.execute("PRAGMA table_info(link_machines)")]
    assert cols[-2:] == ["person_id", "approved_by"]
    assert step.execute("SELECT COUNT(*) FROM link_machines").fetchone()[0] == machines
    assert {r[0] for r in step.execute("SELECT approved_by FROM link_machines")} <= {None}
    step.close()
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 29
    assert db.row_counts(con, db.kept(db.V14_TABLES)) == before
    assert p.with_name(p.name + ".v10.bak").exists()
    con.close()
