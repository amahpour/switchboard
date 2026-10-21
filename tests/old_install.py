"""What an old ``switchboard install`` wrote into a harness's config, for the uninstall tests.

Before 0.38.0 (#385) a harness ran switchboard's hook script through a ``/bin/sh`` guard written into
its config. ``switchboard uninstall`` still has to recognise and remove those entries, so the tests
build them here, byte for byte as they were written. The product no longer writes one.
"""

from __future__ import annotations


def hook_command(
    python: str, home: str, sha12: str, harness: str, event: str, max_wait: float | int | None = None
) -> str:
    """The hook command an old install wrote: a /bin/sh guard, so a missing file or interpreter exits 0."""
    mw = f" --max-wait {int(max_wait)}" if max_wait is not None else ""
    h = f"{home}/hooks/switchboard_hook-{sha12}.py"
    return (
        f'/bin/sh -c \'H="{h}"; P="{python}"; [ -r "$H" ] && [ -x "$P" ] && exec "$P" -I -S "$H"'
        f' --home "{home}" --harness {harness} --event {event}{mw}; exit 0\''
    )
