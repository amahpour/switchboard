# Optional retry keys for say: blocked on the tool-list guard

Issue [#823](https://github.com/amahpour/switchboard/issues/823) asks for an optional
idempotency key so retrying a `say` after a lost answer cannot post it twice. This is a
record of the blocker, not an implemented feature.

## Measured blocker

On the issue's fresh main baseline, adding only `key: str = ""` to the `say` tool's Python
signature changes the serialized tool list from **4,644 to 4,681 characters**, a growth of
**37**. No description or backend change is included in that measurement.

The existing
[tool-list guard](https://github.com/amahpour/switchboard/blob/main/tests/unit/test_mcp_server_static.py)
requires exactly 4,644 characters. The schema-only probe fails it:

```console
$ uv run --frozen pytest -q tests/unit/test_mcp_server_static.py::test_tool_list_fits_the_review_budget
E       AssertionError: 4681
E       assert 4681 == 4644
1 failed
```

The task explicitly says to stop and report if adding the input trips a guard. The probe
was removed. Neither the tool schema nor the guard changes in this draft. No backend,
field allowlist, migration or deduplication behavior has been changed.

## Work remaining

Proceed only after the maintainer resolves this guard constraint. The implementation
still needs a bounded optional key, scoped to the authenticated member, with a short
retention window and storage in `store.py`. A repeated key with the same message must
return the original post; a key reused for different input must be refused. Posting and
recording the key must be atomic, including the transition from an accepted queued say
to a stored message. A retry must not repeat delivery, rate-limit accounting or unread
acknowledgements.

Tests must cover a lost answer followed by retry, broker restart, queued says, expiration,
different members using the same key, and conflicting input. The existing tool schema,
input field checks and size guard must remain intact. Deployment and tool documentation
and a release note belong with that implementation.

Until then, a `say` whose answer was lost after storage can be duplicated when resent.
