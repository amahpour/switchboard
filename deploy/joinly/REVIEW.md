# Review rules: the joinly patches

Applies to `deploy/joinly/` (files mounted over the meeting bot's joinly). The root [REVIEW.md](../../REVIEW.md) applies too.

## Flag

- **A changed file whose sha256 pin did not change.** `server.py` and `kokoro.py` are pinned in `docs/DEPLOY.md`, in the plugins' `meeting/fixes.pins`, and in the meeting self-test; `tests/unit/test_joinly_voice_patch.py` keeps every copy identical. A mismatch makes the host script refuse the file. Check all places, and that a changed pin says "re-mount" in `changes/`.
- **A copy that differs.** The same patch file lives here and in the plugins' `meeting/`; they must stay identical.
- **Captions edge cases** (#852). A bot's own caption row must count as the bot from its first frame, before its initials show, or the bot cuts its own spoken line a second after it starts. A person speaking over the bot must still stop it; a cough captioned as nothing or one word should not. Check the rule against a row whose key changes.
- **A log line that holds what someone said.** Log a key's parts and a word count, never the text.
- **A step that bypasses Zoom's or Google's own blocking.** Never.

## Skip

- The upstream joinly code these files patch, except the lines this change touches.
