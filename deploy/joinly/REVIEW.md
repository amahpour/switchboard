# Review rules: the joinly patches

Applies to `deploy/joinly/` (files mounted over the meeting bot's joinly). The root [REVIEW.md](../../REVIEW.md) applies too.

## Flag

- **A changed file whose sha256 pin did not change.** `server.py` and `kokoro.py` are pinned in `docs/DEPLOY.md`, in the plugins' `meeting/fixes.pins` and in the meeting self-test. `captions.py` is the server bot's own file: pinned in `docs/DEPLOY.md` and `tests/live/test_meeting_selftest.py` only, never in the plugins. `tests/unit/test_joinly_voice_patch.py` checks each. A mismatch makes the host script refuse the file, and the broker needs a re-mount: check that `changes/` says so.
- **A copy that differs.** `server.py` and `kokoro.py` live here and in the plugins' `meeting/`; they must stay identical.
- **Captions edge cases** (#852). A bot's own caption row must count as the bot from its first frame, before its initials show, or the bot cuts its own spoken line a second after it starts. A person speaking over the bot must still stop it. How many words count as an interruption (a cough, one word) is still open in #852: do not require either answer.
- **A log line that holds what someone said.** Log a key's parts and a word count, never the text.
- **A step that bypasses Zoom's or Google's own blocking.** Never.

## Skip

- The upstream joinly code these files patch, except the lines this change touches.
