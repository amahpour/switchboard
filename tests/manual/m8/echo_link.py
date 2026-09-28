"""A stand-in satellite for forced_command.sh (README.md here): the forced command of
a ``restrict,command="..."`` key. Standard library only.

It first writes one JSON line ``{"t": "hello", ...}`` saying whether sshd passed an
original command (``SSH_ORIGINAL_COMMAND``, which the forced command replaces;
only its presence is reported) and whether stdin/stdout are TTYs, then echoes
every line it reads on stdin back on stdout until EOF. It spawns nothing and
opens no socket.
"""

import json
import os
import sys


def main():
    out = sys.stdout.buffer
    hello = {
        "t": "hello",
        "original_command_given": bool(os.environ.get("SSH_ORIGINAL_COMMAND")),
        "stdin_tty": os.isatty(0),
        "stdout_tty": os.isatty(1),
        "ssh_connection_set": "SSH_CONNECTION" in os.environ,
    }
    out.write((json.dumps(hello) + "\n").encode())
    out.flush()
    for line in sys.stdin.buffer:
        out.write(line)
        out.flush()


if __name__ == "__main__":
    main()
