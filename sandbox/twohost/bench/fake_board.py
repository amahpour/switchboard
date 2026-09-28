#!/usr/bin/env python3
"""A fake FPGA board's UART for the fake remote machine (docs/DEMO-FPGA.md §7).

It opens a pty pair and links ``<dir>/ttyFAKE0`` to the slave end, which a test
opens like ``/dev/ttyUSB1``. Once a bitstream has been "flashed" (the
``openFPGALoader`` stand-in writes ``<dir>/.board``) it echoes every byte it
receives; after a bitstream whose bytes contain ``BROKEN`` it drops the third
byte of every third line, so the UART test prints ``FAIL 8/12``. Before any
flash it stays silent, like a blank board.

    python3 fake_board.py [--dir ~/bench]
"""

import argparse
import json
import os
import select
import termios
import tty


def board_state(d: str) -> dict:
    try:
        with open(os.path.join(d, ".board")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser("~/bench"))
    a = ap.parse_args()
    os.makedirs(a.dir, mode=0o700, exist_ok=True)
    master, slave = os.openpty()
    tty.setraw(slave)  # a UART: no echo, no line editing, bytes as they come
    attrs = termios.tcgetattr(slave)
    attrs[4] = attrs[5] = termios.B115200
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    link = os.path.join(a.dir, "ttyFAKE0")
    tmp = link + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(os.ttyname(slave), tmp)
    os.replace(tmp, link)
    print(f"fake board: UART at {link} -> {os.ttyname(slave)}", flush=True)
    line, col = 1, 0  # where in the current line the next byte is (since the last flash)
    flashed = None
    while True:
        r, _, _ = select.select([master], [], [], 1.0)
        if not r:
            continue
        try:
            data = os.read(master, 4096)
        except OSError:
            continue  # the other end closed: keep serving (we hold the slave open)
        st = board_state(a.dir)
        if st.get("sha256") != flashed:
            flashed, line, col = st.get("sha256"), 1, 0
        if not flashed:
            continue  # a blank board answers nothing
        out = bytearray()
        for b in data:
            col += 1
            drop = st.get("broken") and line % 3 == 0 and col == 3
            if b == 0x0A:
                line, col = line + 1, 0
            if not drop:
                out.append(b)
        if out:
            os.write(master, bytes(out))


if __name__ == "__main__":
    main()
