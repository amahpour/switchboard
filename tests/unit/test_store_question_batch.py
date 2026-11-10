"""Whether an agent's last batch put a question to it, read from rows (DESIGN.md §52.2, #713)."""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_world import World


def batch_of(w: World, m, *messages) -> None:
    """Offer ``messages`` to ``m`` in one batch and confirm it, as a delivered wake does."""
    b = w.store.create_batch(m.id, path="wait", kind="wake", items=[(x.id, 1) for x in messages])
    w.store.confirm_batch(b.id, "test")


def test_a_mention_in_the_last_batch_is_a_question_to_its_member(tmp_path: Path) -> None:
    """An @mention of the member, in the last batch it holds, makes that batch a question to it, also after
    it was answered (the broker, not the store, makes a batch answer once). Fails if it is not found."""
    w = World(tmp_path, FakeClock())
    _, m = w.agent("alpha")
    w.agent("beta")
    batch_of(w, m, w.human("@alpha what is the limit?", mentions=("alpha",)))
    assert w.store.question_batch(m.id) is not None
    w.store.mark_handled(m.id)
    assert w.store.question_batch(m.id) is not None


def test_a_message_that_names_someone_else_or_no_one_is_not_a_question_to_this_member(tmp_path: Path) -> None:
    w = World(tmp_path, FakeClock())
    _, m = w.agent("alpha")
    w.agent("beta")
    batch_of(w, m, w.human("@beta what is the limit?", mentions=("beta",)), w.human("we talk a lot"))
    assert w.store.question_batch(m.id) is None


def test_a_reply_to_the_members_own_message_is_a_question_to_it(tmp_path: Path) -> None:
    """The delivery rules treat a reply to one's message like an @mention (#137); so does the voice slot."""
    w = World(tmp_path, FakeClock())
    _, m = w.agent("alpha")
    _, other = w.agent("beta")
    mine = w.agent_says(m, "my earlier message")
    batch_of(w, other, mine)  # not a question to beta: it names no one and replies to no one
    assert w.store.question_batch(other.id) is None
    batch_of(w, m, w.agent_says(other, "and your point?", reply_to=mine.id))
    assert w.store.question_batch(m.id) is not None


def test_only_the_last_batch_counts(tmp_path: Path) -> None:
    """An old mention in an earlier batch, answered or not, does not mark a line said after a later batch."""
    w = World(tmp_path, FakeClock())
    _, m = w.agent("alpha")
    batch_of(w, m, w.human("@alpha old question", mentions=("alpha",)))
    batch_of(w, m, w.human("just chatter"))
    assert w.store.question_batch(m.id) is None
