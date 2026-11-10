"""joinly's tool schema as the bridge and the relay read it (DESIGN.md §52.5, #713)."""

from __future__ import annotations


async def test_takes_voice_reads_the_speak_text_schema_and_fails_closed() -> None:
    """The patched ``speak_text`` has a ``voice`` property; stock joinly's has only ``text``; a client that
    errors, or a joinly with no such tool, is not trusted with the argument."""
    from types import SimpleNamespace as NS

    from switchboard.meeting import joinly

    def client(tools: object = None, boom: bool = False) -> object:
        async def list_tools() -> object:
            if boom:
                raise RuntimeError("no")
            return tools

        return NS(list_tools=list_tools)

    patched = NS(name="speak_text", inputSchema={"properties": {"text": {}, "voice": {}}})
    stock = NS(name="speak_text", inputSchema={"properties": {"text": {}}})
    other = NS(name="send_chat_message", inputSchema={"properties": {"voice": {}}})
    assert await joinly.takes_voice(client([patched])) is True
    assert await joinly.takes_voice(client([stock])) is False
    assert await joinly.takes_voice(client([other])) is False  # a voice on another tool is not speak_text's
    assert await joinly.takes_voice(client([])) is False
    assert await joinly.takes_voice(client(boom=True)) is False
    assert await joinly.takes_voice(client([NS(name="speak_text", inputSchema=None)])) is False
