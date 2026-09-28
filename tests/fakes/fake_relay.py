"""A stand-in socket relay (DESIGN.md §27.5.7): what ``ssh -R``/``ssh -L`` or
``socat`` does to a Unix socket, in the standard library only.

Copied to a path ending in ``/ssh`` (the fake_harness trick), it listens on
``argv[1]`` and forwards every connection, byte for byte, to ``argv[2]`` (the
broker's socket). The broker's kernel peer is then this relay, never the
client behind it. Prints ``ready`` once it listens; runs until killed.
"""

import os
import socket
import sys
import threading


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def main():
    listen, target = sys.argv[1], sys.argv[2]
    os.umask(0o077)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(listen)
    srv.listen(16)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while True:
        client, _ = srv.accept()
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(target)
        except OSError:
            client.close()
            upstream.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    main()
