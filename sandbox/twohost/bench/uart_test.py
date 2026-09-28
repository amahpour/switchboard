#!/usr/bin/env python3
"""The bench's UART test (docs/DEMO-FPGA.md): send N random lines, expect each echoed.

    python3 uart_test.py <device> <baud> [--lines 12] [--timeout 1.0]

The first line of output is the verdict, ``PASS 12/12`` or ``FAIL 8/12`` (then
the first mismatch); exit 0 for a pass, 1 for a fail, 2 for a device that
can't be opened. Works with a real USB-UART and with the fake board's pty.
"""

import argparse
import os
import random
import select
import string
import sys
import termios
import time
import tty

BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600,
         115200: termios.B115200}


def read_line(fd: int, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while not buf.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        r, _, _ = select.select([fd], [], [], left)
        if not r:
            break
        chunk = os.read(fd, 1)
        if not chunk:
            break
        buf += chunk
    return buf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("device")
    ap.add_argument("baud", type=int)
    ap.add_argument("--lines", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=1.0)
    a = ap.parse_args()
    if a.baud not in BAUDS:
        print(f"FAIL 0/{a.lines}: unsupported baud {a.baud}")
        return 2
    try:
        fd = os.open(os.path.expanduser(a.device), os.O_RDWR | os.O_NOCTTY)
    except OSError as e:
        print(f"FAIL 0/{a.lines}: can't open {a.device}: {e.strerror}")
        return 2
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = BAUDS[a.baud]
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        rng = random.Random()
        ok = 0
        first_bad = None
        for i in range(a.lines):
            line = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(24)) + "\n"
            os.write(fd, line.encode())
            got = read_line(fd, a.timeout)
            if got == line.encode():
                ok += 1
            elif first_bad is None:
                first_bad = (i + 1, line.strip(), got.decode(errors="replace").strip())
    finally:
        os.close(fd)
    if ok == a.lines:
        print(f"PASS {ok}/{a.lines}")
        return 0
    print(f"FAIL {ok}/{a.lines}")
    if first_bad is not None:
        n, sent, got = first_bad
        print(f"line {n}: sent {sent!r}, got {got!r}" if got else f"line {n}: sent {sent!r}, no echo")
    return 1


if __name__ == "__main__":
    sys.exit(main())
