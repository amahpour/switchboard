"""Write ``v1_13_0.sql``: a schema-v59 database made by switchboard 1.13.0, dumped as SQL, holding people's
webhooks as 1.13.0 kept them, in ``broker_settings`` rows (#723).

The v59 -> v60 migration (#923, #926, DESIGN.md §62.4) moves these rows into ``webhooks`` and
``webhook_sent``; ``tests/unit/test_db_migrate_v60.py`` tests it on this dump. The rows come from 1.13.0's own
code: ``webhook.save`` and ``webhook._record`` for an address and how its last post went, and the notifier's
``_keep`` for what it sent, after its policy sent one question. The owner (person 0) has an address, an ok
status and a sent key; bob has an address and a failed status; person 7 has a webhook row that does not
parse (written with the store's own ``setting_set``, as a hand edit would leave it), with a status and a sent
row of its own; and one setting that is not a webhook's. No broker, no processes, a fixed clock, made-up
addresses (``hooks.example.com``), nobody's data.

It must run against the 1.13.0 code (schema version 59), not this checkout's:

    git worktree add --detach <tmp>/sb-1130 v1.13.0
    cd <tmp>/sb-1130 && uv sync
    uv run python <this repo>/tests/fixtures/db/make_v1_13_0.py <this repo>/tests/fixtures/db/v1_13_0.sql

It imports only the installed ``switchboard`` package, never this repo's tests.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from switchboard import db, notify
from switchboard.broker import webhook
from switchboard.store import Store

T0 = 1_790_000_000.0
OWNER_URL = "https://hooks.example.com/services/T000/B000/ownersecret?token=t0"
BOB_URL = "https://hooks.example.com/services/T000/B000/bobsecret?token=t1"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


def main(out: Path) -> None:
    if db.SCHEMA_VERSION != 59:
        raise SystemExit(f"run this against switchboard 1.13.0 (schema 59), not schema {db.SCHEMA_VERSION}")
    with tempfile.TemporaryDirectory() as td:
        con = db.open_db(Path(td) / "switchboard.db")
        store = Store(con, Clock(T0))  # type: ignore[arg-type]
        state = SimpleNamespace(store=store)
        bob = store.person_add("bob", b"\x01" * 16, "0" * 64, 3600.0)

        webhook.save(state, 0, OWNER_URL, 300.0)
        webhook._record(state, 0, T0, None)
        n = webhook.Notifier(state)  # type: ignore[arg-type]
        pol = n._policy_of(0)
        need = notify.Need("q:build:12", "question", "#build", "build", "claude-1", T0)
        pol.due([need], T0, 300.0)
        sent = pol.due([need], T0 + 301, 300.0)
        pol.sent(sent, T0 + 301, True)
        n._keep(0, pol)

        webhook.save(state, bob.id, BOB_URL, 600.0)
        webhook._record(state, bob.id, T0 + 5, "status")

        store.setting_set("webhook:7", "{not json")
        store.setting_set("webhook_status:7", json.dumps({"at": T0, "ok": True, "error": None}))
        store.setting_set("webhook_sent:7", json.dumps(["q:build:99"]))
        store.setting_set("notify_text:0", "0")  # the owner's "show the first line" setting (#837)
        con.commit()
        out.write_text("\n".join(con.iterdump()) + "\n")
        con.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
