"""Read-only live views of tmux sessions for xterm.js: one pty per viewer, running
`tmux attach -r`, its output streamed over a WebSocket. Nothing typed in the page reaches tmux."""
import asyncio, fcntl, os, struct, subprocess, sys, termios
from urllib.parse import urlparse, parse_qs
from websockets.asyncio.server import serve

SOCK = sys.argv[1]
PORT = int(sys.argv[2])


async def view(ws):
    u = urlparse(ws.request.path)
    name = u.path.strip("/")
    qs = parse_qs(u.query)
    cols, rows = int(qs["cols"][0]), int(qs["rows"][0])
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "C.UTF-8",
           "HOME": os.environ.get("HOME", "/tmp")}
    p = subprocess.Popen(["tmux", "-L", SOCK, "attach", "-r", "-t", name], stdin=slave, stdout=slave,
                         stderr=slave, env=env, start_new_session=True, close_fds=True)
    os.close(slave)
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def readable():
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        q.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, readable)
    try:
        while True:
            data = await q.get()
            if not data:
                break
            await ws.send(data)
    finally:
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        p.terminate()
        os.close(master)


async def main():
    async with serve(view, "127.0.0.1", PORT, max_size=None):
        await asyncio.Future()

asyncio.run(main())
