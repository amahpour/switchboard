"""Test-only hook: append the raw hook payload to a file (fixture recording, DESIGN.md §12.3).

Registered only in live-test launch settings, never by switchboard. It prints
nothing and always exits 0, so it can never approve, block or add context.
The file lives in the run's scratch dir (0600); ``fixtures.py`` sanitizes
what gets committed.
"""

import json
import os
import sys
import time


def main():
    try:
        event, out = sys.argv[1], sys.argv[2]
        raw = sys.stdin.buffer.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"_unparsed_bytes": len(raw)}
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"t": time.time(), "event": event, "payload": payload}) + "\n")
    except BaseException:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
