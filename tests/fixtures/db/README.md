# Database fixtures

- `v0_2_0.sql`: a schema-v1 database written by switchboard 0.2.0 (its store and
  delivery engine, a fixed clock, made-up pids and ids), dumped with
  `sqlite3.Connection.iterdump()`. Every table has rows: two rooms, a participant
  of every harness (one of them ended), memberships (one left, one held), chat,
  join, leave and notice messages, batches and deliveries in several states,
  events and a web session. `tests/unit/test_db_migrate_v2.py` loads it into a
  temp database and migrates it to schema v2 (DESIGN.md §27.6).
- `make_v0_2_0.py` made it. It needs the 0.2.0 code (it refuses any other schema
  version); see its docstring for the `git worktree` recipe.

- `v0_7_0.sql`: a schema-v3 database written by switchboard 0.7.0 (the same kind of
  scenario, plus a claimed owner, two passkeys, web sessions that say how they were made,
  and machines that dial in: approved, pending and removed, one with a member in a room).
  `tests/unit/test_db_migrate_v4.py` migrates it to schema v4 (DESIGN.md §32.2).
  `make_v0_7_0.py` made it, from the 0.7.0 code (see its docstring).

- `v1_13_0.sql`: a schema-v59 database written by switchboard 1.13.0, holding people's
  webhooks as it kept them in `broker_settings` rows (an address, an ok and a failed status,
  a sent key, and one row that does not parse). `tests/unit/test_db_migrate_v60.py` migrates
  it to schema v60, which moves them into `webhooks` and `webhook_sent` (#923, #926, DESIGN.md
  §62.4). `make_v1_13_0.py` made it, from the 1.13.0 code (see its docstring).

Nothing here comes from a real session; `tests/unit/test_fixtures_scan.py` checks
these files like every other fixture.
