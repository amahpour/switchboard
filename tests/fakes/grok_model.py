"""A stand-in for Grok Build's model (DESIGN.md §49.15, #851): a chat-completions server that Grok's
custom-model setting (``[model.<id>] base_url``) points at, so Grok Build runs for real with no xAI login
and no tokens spent.

It is a scripted agent, not a model. On each request it reads the conversation Grok sends and does the next
thing the person's words ask for, in the order they were asked, one tool call per answer:

- ``Call join with room "R" and screen_name "N"`` is switchboard's ``join`` (through Grok's ``use_tool``);
- ``call say with the text "W"`` is ``say`` in the room it joined;
- ``run the shell command "C"`` is Grok's ``run_terminal_command``;

and once nothing is left, a one-word reply that ends the turn. What counts as asked is every user-role message
since the agent's last reply: the prompt, a Stop hook's feedback (a wake), and a tool hook's system
reminder (a busy session's priority message). Tool results are never read for instructions.

It records every request it was sent (``requests``), which is how a test sees what reached the model.
Test-only.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# a quote may come escaped (``\\"``): a room message reaches the model inside the envelope's quoted text
Q = r'\\?"'
JOIN = re.compile(rf"Call join with room {Q}([^\"\\]{{1,64}}){Q} and screen_name {Q}([^\"\\]{{1,24}}){Q}")
SAY = re.compile(rf"call say with the text {Q}([^\"\\]{{1,200}}){Q}")
RUN = re.compile(rf"run the shell command {Q}([^\"\\]{{1,200}}){Q}")
# Grok's own side requests (a session title, a dashboard line) are answered with a word and never scripted
SIDE = ("You are tasked with generating the session title", "Write an ultra-short")


def text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def asked(messages: list[dict[str, Any]]) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """What the person has asked for since the agent's last reply, as ``(tool, arguments)`` in order, and how
    many tool calls the agent has made since then."""
    last_reply = 0
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and not m.get("tool_calls") and text_of(m).strip():
            last_reply = i + 1
    room = ""
    for m in messages:
        found = JOIN.search(text_of(m)) if m.get("role") == "user" else None
        if found:
            room = found.group(1)
    todo: list[tuple[str, dict[str, Any]]] = []
    calls = 0
    for m in messages[last_reply:]:
        if m.get("role") == "assistant":
            calls += len(m.get("tool_calls") or [])
            continue
        if m.get("role") != "user":
            continue
        text = text_of(m)
        steps: list[tuple[int, tuple[str, dict[str, Any]]]] = []
        for found in JOIN.finditer(text):
            args = {"room": found.group(1), "screen_name": found.group(2)}
            join = {"tool_name": "switchboard__join", "tool_input": args}
            steps.append((found.start(), ("use_tool", join)))
        for found in SAY.finditer(text):
            args = {"room": room, "text": found.group(1)}
            steps.append((found.start(), ("use_tool", {"tool_name": "switchboard__say", "tool_input": args})))
        for found in RUN.finditer(text):
            # waited for in the foreground (Grok backgrounds a command after 30 s unless told otherwise)
            run = {"command": found.group(1), "description": "run what was asked", "block_until_ms": 200000}
            steps.append((found.start(), ("run_terminal_command", run)))
        todo += [step for _, step in sorted(steps, key=lambda s: s[0])]  # in the order the words came
    return todo, calls


class GrokModel:
    """The stand-in server, on 127.0.0.1 and a free port, in a daemon thread."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # the model list Grok may ask for
                self._json({"object": "list", "data": [{"id": "standin", "object": "model"}]})

            def do_POST(self) -> None:
                n = min(int(self.headers.get("content-length") or 0), 64 * 1024 * 1024)
                try:
                    req = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    req = {}
                with model.lock:
                    model.requests.append(req)
                message = model.answer(req)
                if req.get("stream"):
                    self._stream(message)
                else:
                    self._json(
                        {
                            "id": "standin", "object": "chat.completion", "created": int(time.time()),
                            "model": "standin",
                            "choices": [{"index": 0, "message": message, "finish_reason": _finish(message)}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        }
                    )  # fmt: skip

            def _json(self, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, message: dict[str, Any]) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                delta = dict(message)
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [dict(delta["tool_calls"][0], index=0)]
                for chunk in (
                    {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": _finish(message)}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ):
                    chunk.update(
                        {"id": "standin", "object": "chat.completion.chunk", "created": int(time.time())}
                    )
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> GrokModel:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, req: dict[str, Any]) -> dict[str, Any]:
        messages = req.get("messages") or []
        first = text_of(messages[0]) if messages else ""
        if not req.get("tools") or first.startswith(SIDE):
            return {"role": "assistant", "content": "switchboard"}
        todo, calls = asked(messages)
        if calls < len(todo):
            tool, args = todo[calls]
            call = {
                "id": f"call_{len(self.requests)}",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args)},
            }
            return {"role": "assistant", "content": None, "tool_calls": [call]}
        return {"role": "assistant", "content": "done"}

    def seen(self, needle: str) -> bool:
        """Whether any request to the model carried ``needle`` anywhere."""
        with self.lock:
            return any(needle in json.dumps(r) for r in self.requests)


def _finish(message: dict[str, Any]) -> str:
    return "tool_calls" if message.get("tool_calls") else "stop"
