---
name: record-verification
description: Record a short, captioned screen recording of a web UI change working, for a pull request's Verification section - a throwaway broker, headless Chromium, a scenario file of clicks and captions, and an MP4, an inline GIF and a sheet of its steps. Use when a change is to something a person does in the web UI (a click path, a dialog, a layout that responds), or when asked to record the verification.
argument-hint: "<what to show, or the pull request's number>"
---

# Record a change working

A pull request's Verification shows the change working ([CLAUDE.md](../../../CLAUDE.md), "How a change lands"). For something a person does in the web UI, a recording shows it better than a paragraph does: the reviewer watches fifteen seconds instead of checking out the branch. The stills stay the Previews (before and after, light, dark, the phone); the recording is the change in use.

| The change | Its Verification |
|---|---|
| a click path, a dialog, a menu, a layout that responds to a click | a recording, made as below |
| a colour, a word, a size: nothing to click | the stills are enough |
| CLI output, an API, a rule | the command and what it printed |
| real agents talking through real harnesses | not this script: it drives one page against stand-in agents |

The tests' output goes in the section either way. A recording shows the change worked once; the test is what keeps it working.

1. **Write the captions first.** One change, three to six steps, under thirty seconds. A caption says what the person does or what to look at, in plain words, and it is a claim: the picture under it has to show it. For a fix, captions that name the action ("Open Members again") rather than the result ("the name stays whole") read true on `main` as well, so one file records both.

2. **Write the scenario**, a file of its own outside the repo's tree (it travels with the recording, step 6):

   ```python
   """What this shows, in a line."""

   from playwright.sync_api import expect


   def scenario(take, world):
       world.create_room("#sprint-groming")                 # seed what the change needs
       world.add_agents("#sprint-groming", ("claude-1",))
       take.open("#sprint-groming")                         # getting there isn't recorded
       take.begin()                                         # the recording starts here
       take.step("A room with a typo in its name")
       take.pause(1.2)                                      # time to read, nothing else
       take.step("Click the room's name")
       take.click("#room-title-button")                     # a ring marks where it landed
       expect(take.page.locator("#app-dialog-name")).to_be_focused()
       take.step("Type the new name and press Enter")
       take.type("#app-dialog-name", "#sprint-grooming", clear=True)
       take.press("Enter")
       expect(take.page.locator("#room-title")).to_have_text("sprint-grooming")
       take.pause(2.0)
   ```

   `take` has `open`, `begin`, `step`, `click`, `type`, `press`, `pause` and `rest` (move the pointer away, when a hover tip covers what the step shows); `take.page` is Playwright's page for anything else. `world` is `tests/ui_world.py`'s `UIWorld`: a test broker in a throwaway home with alice, three rooms and the stand-in agents. `rename_room` in [`docs/media/ui_record.py`](../../../docs/media/ui_record.py) is the worked example.

   - **Wait for the event with `expect`,** as the e2e tests do. `take.pause` is reading time for the viewer, never a wait for the page.
   - **One window size.** The frames are joined at the size the recording started with, so a scenario never resizes the window. A phone is `--phone`.
   - **Only test data in the picture:** alice, the stand-in agents, rooms you made up. It is a public repo.

3. **Record it.** Once per machine: `uv run playwright install chromium`. The `record` group (ffmpeg and Pillow) is not part of the regular setup; `--group record` adds it for this command.

   ```bash
   uv run --group record python docs/media/ui_record.py path/to/my_change.py --out <dir>
   ```

   `--dark` and `--phone` (390x844) change the theme and the size; `--name` names the files. It starts a broker and a browser, so on a shared machine it waits its turn like a test suite ([CONTRIBUTING.md](../../../CONTRIBUTING.md), "Sharing a machine"). A wait that times out exits non-zero: fix the scenario, don't lengthen a pause.

4. **Look at it before you post it.** Open `<name>.steps.png`: the picture at the end of each step, caption and all. For each one: does it show what its caption says? Is anything cut off, covered by a tip, or caught mid-animation? Is everything in it test data? Then watch the GIF once if you can.

   A recording often shows something nobody asked about: a name cut to one letter, an error in a dialog nobody has typed in yet. That is a finding. Fix it if this change caused it, file an issue if it didn't, and never rewrite the scenario to steer around it.

5. **For a fix, record the bug too,** when the same clicks show it: in a worktree of `origin/main`, the same file with `--name <name>-before`. If the scenario can't run there (it waits for something the fix adds), a still of the bug is enough.

6. **Post it.** Push the `.gif`, the `.mp4` and the scenario file to `design-assets` under `pr-<number>/` (or a branch of your fork), as the previews are, and link them by commit SHA. In the pull request's Verification:

   ```markdown
   | `main` | this branch |
   |---|---|
   | ![before: the name reads "s…"](https://raw.githubusercontent.com/<owner>/switchboard/<sha>/pr-<number>/<name>-before.gif) | ![after: the name stays whole](https://raw.githubusercontent.com/<owner>/switchboard/<sha>/pr-<number>/<name>.gif) |

   MP4: [before](…), [after](…). Scenario: [`<name>.py`](…).
   ```

   The alt text says what the recording shows, for someone who can't play it. A GIF plays inline; the MP4 is the sharp copy. A short scenario makes a GIF of a few hundred kilobytes: one of several megabytes is a scenario that is too long.
