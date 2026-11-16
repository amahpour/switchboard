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


async def test_speak_by_sentence_says_each_sentence_in_order_and_stops_when_told() -> None:
    """A line goes to joinly a sentence at a time, in order (#761); an abbreviation does not split it; the
    caller can stop the rest, and someone speaking over a sentence ends the line, as it ended the whole
    call before. Fails if the text goes as one call, the order changes, or a stop is ignored."""
    from switchboard.meeting import joinly

    sent: list[str] = []

    async def speak(text: str) -> str:
        sent.append(text)
        return "Interrupted by detected speech." if text == "Two." and interrupt else "Finished speaking."

    interrupt = False
    assert await joinly.speak_by_sentence(speak, "One. Two! Is it e.g. three? Yes.", lambda: True)
    assert sent == ["One.", "Two!", "Is it e.g. three?", "Yes."]

    sent.clear()
    gone = False

    async def speak_then_go(text: str) -> str:
        nonlocal gone
        sent.append(text)
        gone = True
        return "Finished speaking."

    await joinly.speak_by_sentence(speak_then_go, "One. Two. Three.", lambda: not gone)
    assert sent == ["One."]

    sent.clear()
    interrupt = True
    said = await joinly.speak_by_sentence(speak, "One. Two. Three.", lambda: True)
    assert sent == ["One.", "Two."] and said.startswith(joinly.INTERRUPTED)
    sent.clear()
    await joinly.speak_by_sentence(speak, "no end mark", lambda: True)
    assert sent == ["no end mark"]
