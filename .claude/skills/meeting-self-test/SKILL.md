---
name: meeting-self-test
description: Run the meeting self-test - make a throwaway Zoom meeting with the Zoom connector, send switchboard's meeting bot and a second test bot into it, check every meetings feature (call lines, a spoken name waking an agent, an agent speaking, a snapshot, leaving), then delete the meeting and report a table. Use when asked to test or verify meetings end to end, or after a change to src/switchboard/meeting/ or to how joinly is run.
argument-hint: "[a Meet or Teams link, if the person gives one]"
---

# Run the meeting self-test

The mocks in `tests/integration/` show that the broker does the right thing with a scripted joinly. They cannot show that a real call carries the words, the voice and the picture. This test can: it puts two joinly bots and a throwaway broker into one real call and checks the [meetings guide](../../../docs/guide/meetings.md) feature by feature. The test is [`tests/live/test_meeting_selftest.py`](../../../tests/live/test_meeting_selftest.py); this skill is the careful way to run it. The link to a call carries its passcode, so how it travels matters as much as the steps.

Asking for the self-test is the person's yes to **one** Zoom meeting, made and then deleted. It is not a yes to anything else in their Zoom account.

1. **Check what it needs.**
   - `docker version` answers, and `docker image inspect ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0` finds the image. It is 3.7 GB, so if it is missing, say so and ask before `docker pull` (the tag is the one [DEPLOY.md](../../../docs/DEPLOY.md) runs).
   - For a Zoom meeting: the **Zoom connector** is connected, and its `meeting_create` and `meeting_delete` tools are in your tool list (they may be deferred: load them first). If they are not there, stop and say the connector is missing. Don't try another way into Zoom.
   - For a Meet or Teams link from the person: nothing to check here. See "A Meet or Teams link" below.
   - Nothing else is running the suite: take the machine's lock for step 4, and read [CONTRIBUTING.md](../../../CONTRIBUTING.md) ("Sharing a machine") if the machine is shared.

2. **Make the meeting** (Zoom only) with the Zoom connector's `meeting_create`:
   - `userId: "me"`, `topic: "switchboard self-test"`, `type: 2`, `duration: 15`.
   - `start_time`: now, as a local timestamp with no zone suffix, and `timezone`: the person's IANA timezone. Ask if you don't know it. The connector says not to guess it from the server's clock.
   - `settings`: `join_before_host: true`, `jbh_time: 0`, `waiting_room: false`, `mute_upon_entry: false`, `auto_recording: "none"`, `meeting_authentication: false`.
   - Leave `password` out. The account makes one, and it is in the join link.
   - From the result keep two things in this conversation, and nowhere else: the meeting's `id` and its `join_url`. Never print `join_url`, `start_url` or the passcode, and never put them in your reply, the report, a commit, an issue or a pull request. The report uses `https://zoom.us/j/<meeting-id>` where it must show the shape of a link.

3. **Put the link in a file.** In the session's scratchpad directory (or a `mktemp -d` one, which is mode 0700):

   ```bash
   (umask 077; printf '%s\n' '<the join_url>' > <scratchpad>/link.txt) && chmod 600 <scratchpad>/link.txt && stat -c %a <scratchpad>/link.txt
   ```

   The last command must print `600`: the test refuses any other mode, because the passcode is in the file. Writing it shows the link once in this session's own transcript. That can't be avoided, and it stays local. The file, never an argument or an environment variable, is how the test gets it.

4. **Run the test**, through the machine's suite lock (`sb-suite <command>` where it exists, otherwise the `flock` line in CONTRIBUTING.md). It takes three to six minutes, and no model is called:

   ```bash
   SWITCHBOARD_LIVE=meeting \
   SWITCHBOARD_MEETING_URL_FILE=<scratchpad>/link.txt \
   SWITCHBOARD_SELFTEST_OUT=<scratchpad>/selftest-out \
     uv run pytest -m live tests/live/test_meeting_selftest.py -s -q
   ```

   It starts two joinly containers on free loopback ports and a throwaway broker in a temp home. On a Zoom link both run with our speaker-name fix (joinly-ai/joinly#187) mounted, the file [DEPLOY.md](../../../docs/DEPLOY.md) pins, fetched and checked by its sha256: stock 0.5.4 can't read Zoom's participant list, and the first run without it failed steps 2 and 6. On every platform both also run with the first-word fix (#620, joinly-ai/joinly#188), a second pinned file mounted the same way: stock joinly clips the first word of a line, and the hotwords can turn it into "Claude". On every platform both also run with the voice patch (#713, two files of this repository's `deploy/joinly/`, checked by sha256), which step 6 uses. `SWITCHBOARD_SELFTEST_STOCK_JOINLY=1` runs stock joinly, without any fix (and without step 6). `SWITCHBOARD_SELFTEST_CALLOUTS=N` repeats step 3 N times (up to 30) and reports how many woke the agent: use it to measure a miss that is rare (#602). `SWITCHBOARD_SELFTEST_OBSERVER=1` adds an independent listener, so a missed wake says whether the call's audio or the bot lost the name (#602). A failed step keeps each container's log, scrubbed of the link, in the output folder, and `meeting-selftest.md` names the files. `SWITCHBOARD_SELFTEST_LOGS=1` keeps them on a passing run too. Delete the logs after reading them: they name the call's participants. Both bots join the call, seven steps run in order, and both bots leave and both containers go whatever happens. It prints each step as it ends, then the table, and writes `meeting-selftest.json` and `meeting-selftest.md` into the output folder. Neither holds the link or the meeting id.

   Nobody should talk on the call while it runs. A voice over the bots breaks steps 2 to 4.

5. **Clean up, whether the test passed, failed or never started.** Do this even if step 4 raised an error or was stopped:
   - delete exactly the meeting you made with the Zoom connector's `meeting_delete`, passing the `id` from step 2 as `meetingId`. Never look a meeting up by its topic, never delete anything else, and never delete before the run has ended: deleting the meeting throws the bots out;
   - delete `<scratchpad>/link.txt`;
   - `docker ps -a --filter label=switchboard-selftest` lists nothing. If it lists a container, it is one the test started and did not remove (a run killed hard): remove only those, by name.

6. **Report** the table from `meeting-selftest.md`, pasted as it is, and one line for each failed step with what it probably means (below). Say that the meeting was deleted and the file removed. If the report names container logs, read them and then delete them: they name the call's participants. If the link, the passcode or the meeting id appears anywhere in what you are about to send, stop: the test is supposed to keep them out of every output, so that is a bug to report on its own, without pasting what leaked.

## The bot on a person's machine (a hosted bot)

The same test exists with the bot started by the plugin's host script instead of by `/meeting <link>` (#623): `tests/live/test_meeting_selftest_hosted.py`. The procedure above is the same, with that file in step 4. It needs the joinly image from step 1 and builds switchboard's own image from the checkout, so nothing new is pulled. With **no link** the module runs only its first test, which needs no meeting at all (a hosted bot starts and stops with no call), so run that one first and never make a meeting for it. With the link file it runs the six steps; step 5 is that a hosted call has no snapshot yet, and step 6 also needs both containers gone. To run the bot on a second machine, `SWITCHBOARD_SELFTEST_HOSTED=remote` prints the commands for it; the module's docstring has them. The link goes to the second machine in a file of its own, never in a message or a command line.

## What the seven steps check

| # | Step | Passes when |
|---|---|---|
| 1 | `/meeting <link>` from a person | the room's call bar goes live |
| 2 | the speaker joins, says a warm-up line, then a known sentence | a call line with at least 60% of the sentence's words reaches the room (the report also says whether the warm-up was heard: on Zoom a bot's first words after it unmutes can be lost) |
| 3 | the speaker says "Claude one, can you hear me?" | the scripted agent `claude-1` is woken by that line, addressed to it |
| 4 | `/meeting voice on`, and `claude-1` says `@call I can hear you loud and clear` | the speaker's own transcript has at least 60% of those words |
| 5 | `/meeting snapshot` | a stored file whose bytes are a JPEG, and its card in the room |
| 6 | the speaker says one line in each of two Kokoro voices (`am_adam`, `bf_emma`), then a line in a voice that does not exist (not run with stock joinly) | the bot's room has both lines as call lines, neither answer says the voice was not found, and the unknown one does (the patch falls back and still speaks). Listen: the two lines should sound like two people |
| 7 | `/meeting leave`, and the speaker leaves | the bar ends, and the speaker no longer sees the bot |

The words are compared after lowercasing and dropping punctuation, with digits read as words ("Claude 1" is "Claude one"). Small words are ignored. Speech-to-text of text-to-speech through a call is close, not exact, so most of the words is a pass. A step that can't start because an earlier one failed is a failure ("not attempted"), so a run is green only when all six passed.

## What a failure looks like

The report's **Seen** column is what the test saw. It never holds the link.

| Seen | What it means | What to do |
|---|---|---|
| Step 1: "the call ended before it went live" and "couldn't join the call" | joinly could not join as a guest in 45 seconds | the Zoom settings below, or a bad or expired link |
| Step 2: "the speaker could not join" | the second bot could not join | the same causes, or the machine ran out of memory for two Chromiums |
| Step 2: "no call line reached the room" | both are in the call, but the bot heard nothing | audio not joined, or guests muted on entry; check `mute_upon_entry` |
| Step 2: a line with other words | speech-to-text got it wrong; the match is shown | a ratio just under 60% is noise; far under is a real problem |
| Step 3: "claude-1 was not woken; call lines heard instead: ..." | the line arrived without the name: the start of the sentence was cut ("Can you hear me?"), or the name was misheard | the call lines shown say which; a cut start is #602, joinly's speech detection |
| Step 4: "the speaker heard nothing from the bot" | `speak_text` ran, but nothing reached the other bot | the bot is muted or its audio is blocked in the call |
| Step 5: "no card reached the room" or "not a JPEG" | joinly gave no picture, or a different format | the notice text beside it says which |
| Step 6: "the bot is still listed" | the bar ended, but the bot did not leave the call | joinly's `leave_meeting` failed; look at its log by running it by hand |
| a "problem": "a piece of the link is in ..." | the broker kept part of the link | a real bug. Stop and report it. Don't paste what was found |
| a "problem": a container is still there | cleanup failed | remove it by name |

## Zoom settings that block it

The bots join as guests, in a browser, with no account.

- **Guests must be able to join from a browser.** The account setting that shows the "Join from your browser" link must be on. Without it joinly cannot get past the page that asks to open the Zoom app.
- **No forced waiting room.** A locked account setting that puts everyone in the waiting room overrides `waiting_room: false`, and nobody admits the bots. (`waiting_room: true` also turns `join_before_host` off.)
- **No required sign-in.** A locked setting that only lets authenticated users join overrides `meeting_authentication: false`, and the guests are turned away.
- **Join before host** must be allowed. If an admin has locked it off, the bots wait for a host who never comes.
- **Mute upon entry** must not be forced on.

Zoom names these differently between releases, and an admin can lock them for the whole account. If the first run fails at step 1 or 2, look here before the code.

## A Meet or Teams link

If the person gives you a link instead, use it as it is: write it to the file as in step 3, run steps 4 and 6, and delete the file. Don't create or delete anything in Meet or Teams. Skip step 2 (making the meeting) and the meeting deletion in step 5.

Someone has to admit both bots. Tell the person before you start, because the first bot gives up on joining after 45 seconds:

- the bot that joins first is `switchboard (transcribing)`; admit it as soon as it asks;
- the second is `switchboard self-test speaker`, about half a minute later; admit it too.

Both leave at the end by themselves.
