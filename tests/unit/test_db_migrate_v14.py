"""The v13 to v14 migration (#131, DESIGN.md §42): preferences gain a nullable
``budget_per_hour`` and ``hop_limit``, both NULL (not set) until a person chooses one in
Settings; every row stays, after a checked backup."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from switchboard import db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_7_0.sql"


def test_preferences_gain_null_budget_and_hop_defaults_with_a_checked_backup(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    con = sqlite3.connect(p)
    con.executescript(FIXTURE.read_text())
    steps = (*db.V3_TO_V4, *db.V4_TO_V5, *db.V5_TO_V6, *db.V6_TO_V7, *db.V7_TO_V8, *db.V8_TO_V9)
    for stmt in (*steps, *db.V9_TO_V10, *db.V10_TO_V11, *db.V11_TO_V12, *db.V12_TO_V13):
        con.execute(stmt)
    con.execute(
        "INSERT INTO preferences(person_id, theme, text_size, room_rules) VALUES(0, 'dark', 'large', 'wt')"
    )
    con.commit()
    assert db.schema_version(con) == 13
    before = db.row_counts(con, db.TABLES)
    con.close()
    os.chmod(p, 0o600)

    con = db.open_db(p)
    assert db.schema_version(con) == db.SCHEMA_VERSION == 14
    assert db.row_counts(con, db.TABLES) == before
    row = con.execute(
        "SELECT theme, text_size, room_rules, budget_per_hour, hop_limit FROM preferences WHERE person_id=0"
    ).fetchone()
    assert tuple(row) == ("dark", "large", "wt", None, None)
    assert p.with_name(p.name + ".v13.bak").exists()
    con.close()
