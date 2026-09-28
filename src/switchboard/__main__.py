import sys

if len(sys.argv) > 1 and sys.argv[1] == "satellite" and sys.platform.startswith("linux"):
    # The far end of a remote link: non-dumpable before anything else is imported or read,
    # so the window in which a same-user process could open its /proc/<pid>/fd is as short
    # as it can be (DESIGN.md §27.4.8). The satellite's own harden() repeats it and reports.
    import ctypes

    try:
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        pass

from switchboard.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
