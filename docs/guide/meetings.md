# Meetings

A room can follow a live Zoom, Google Meet or Microsoft Teams call. You send a meeting bot into the call. It listens, and switchboard posts what people say into the room as short lines tagged **call**. Your agents read those lines like any other message. When someone on the call says an agent's name, that agent wakes up and gets to work. Use it when the decisions happen on a call and you want your agents to hear them: to look something up while people keep talking, or to start on a fix the moment someone asks for it.

The bot only listens. It does not speak in the call, and it does not share a screen.

## How it works

1. An admin runs the bot once, next to the broker, and tells the broker where it is. The bot is [joinly](https://github.com/joinly-ai/joinly), a separate open-source project. See [Set it up](#set-it-up).
2. You type `/meeting` and a call link in a room, in the web UI.
3. switchboard checks the link. It has to be an `https` link on Zoom, Meet or Teams. Then it asks joinly to join the call as a guest named `switchboard (transcribing)`. It also hands joinly the spoken names of the agents in the room, as hints. Joining usually takes a few seconds. The room shows **Joining a call…**.
4. Once the bot is in, the room shows a bar: **Transcribing a call**, with a **Leave call** button.
5. joinly turns speech into text. switchboard fetches that text every few seconds and joins the pieces into one line each time the speaker pauses.
6. Each line is posted in the room, tagged **call**, with the speaker's name when joinly has one.
7. Talk that names nobody stays in the room and waits. It does not wake any agent.
8. When someone says an agent's name, only that agent wakes. It gets the call lines from just before, so it knows what was being talked about.
9. **Leave call** or `/meeting leave` ends it. So does closing the room. switchboard tells the bot to leave, and the bar goes away.

```mermaid
sequenceDiagram
    participant You
    participant Room as the room
    participant Bot as joinly, the meeting bot
    participant Call as the call
    participant Agent as claude-1
    You->>Room: /meeting and the call link
    Room->>Bot: join as a guest
    Bot->>Call: joins
    Call-->>Bot: people talk
    Bot-->>Room: text, every few seconds
    Room->>Room: join the pieces into call lines
    Note over Room,Agent: talk that names nobody just waits in the room
    Room->>Agent: someone says Claude one, wake up
    Agent->>Room: say with an answer
    You->>Room: Leave call
    Room->>Bot: leave
```

## Set it up

An admin does this once. If someone already did, skip to [Use it](#use-it).

1. **Run joinly beside the broker.** [DEPLOY.md](../DEPLOY.md#a-meeting-bot) has the container command, for one machine and for a compose network.
2. **Keep joinly private.** It has no sign-in, and it takes its settings from whoever connects. Publish its port on `127.0.0.1` only, or only on the compose network. Never on an open address.
3. **Tell the broker where joinly is.** In the broker's `config.toml`:

    ```toml
    [meeting]
    joinly_url = "http://127.0.0.1:8010/mcp/"
    ```

    Then restart the broker. It reads its config when it starts. Only this file sets the address. A message in a room never can.
4. **On Zoom, decide about speaker names.** joinly 0.5.4 gives no speaker names on Zoom, so every line there shows `call` where a name would be. Meet and Teams are fine. The fix is [joinly-ai/joinly#187](https://github.com/joinly-ai/joinly/pull/187), a change to joinly's Zoom file. Until a joinly release has it, mount the fixed file read-only over the image's copy: [DEPLOY.md](../DEPLOY.md#a-meeting-bot) has the two commands, and explains the cause.
5. **Try it.** In a room, in the web UI, type `/meeting` and a test meeting's link. If the room says it is joining, you are done. If it says no meeting server is set, see [Troubleshooting](#troubleshooting).

Who can start a call: any signed-in person who can see the room, in the web UI. Agents can't. If an agent says `/meeting …` in a room, it is only text. The command line can't either, because a call lets anyone on it reach your agents, and that is your decision. This holds even for the plain `/meeting` that asks whether a call is live.

## Use it

You start and end a call from the room's message box.

1. **Open the room** whose agents should hear the call. The agents that are in the room when you start are the ones the bot is told to listen for.
2. **Type `/meeting`.** The command list shows it, tagged **web only**. Follow it with the call's invite link, as it was given to you, passcode and all. Press Enter.

    ![The command list open on /meeting, with its web only tag](../media/ui/guide-meeting-command-light.png)

3. **Watch the bar.** The room says who started the call, and the bar reads **Joining a call…** until the bot is in.

    ![The bar while the bot joins the call, with its Leave call button](../media/ui/guide-meeting-joining-light.png)

4. **Admit the bot, if the meeting has a waiting room.** It shows in the participant list as `switchboard (transcribing)`. Until it is let in, it hears nothing.
5. **Tell the people on the call.** The bot's name says it is transcribing, but say it out loud too. Everything people say goes into a room that your agents read.
6. **Talk.** Lines show up a few seconds after each pause. The pictures here are from a Meet call, so the lines carry speaker names.

    ![A live call: a bar reading Transcribing a call, two call lines with their speakers, and claude-1's answer](../media/ui/guide-meeting-call-light.png)

7. **Wake an agent by saying its name.** Say it as an address: "Claude, can you check the image sizes?", "hey Codex, run the tests", or "Claude one, take this". The agent wakes and answers in the room. Its answer is text in the room. It can't speak into the call.
8. **Check on it** any time with `/meeting`. It says whether a call is being transcribed.
9. **End it** with **Leave call** on the bar, or `/meeting leave`. The room posts a notice, and the bot leaves the call.

The call also ends by itself when the room is closed, when the connection to joinly drops, when joinly can't join, and when the broker stops. The room says which in a notice.

## What agents see

Call lines are ordinary room messages, with the sender kind `meeting`. An agent that reads the room sees them marked that way, with the speaker's name, or `call` when there is none. The name is lowercased and spaces become hyphens. A line that names the agent looks like this:

```text
- id=27 at=19:16:04 from=alice kind=meeting to_you=yes prio=mention text=I think it is the image sizes. Claude one, can you check them?
```

- **A line that names nobody** has `to_you=no` and `prio=chatter`. It never starts an agent's turn by itself. It waits, and shows up in the agent's next `read()` or with its next real wake. It does not count toward the loop guard that stops agents from talking to each other forever. The room's rate limit on `say()` does not apply to it either.
- **A line that names an agent** has `to_you=yes` and `prio=mention`, for that agent only. It wakes the agent the way an @mention does, and it spends one wake from the room's hourly budget. The call lines from just before come along. [Delivery rules](../USAGE.md#delivery-rules) says how wakes work.
- **A line that names something the agent has claimed**, such as a ticket key it reserved, wakes the agent that holds the claim in the same way. Someone reading out `SYSINT-645` wakes whoever claimed it.
- **Depending on how the agent was woken,** the line is shown whole or as "not shown here", with a `read()` call that fetches it. An agent has to read it first, as with any other agent's message.
- **It is untrusted text.** Agents are told, when they join, that only messages from your user (`kind=human`) are instructions, and that peers are not. A call line counts as a peer. Anyone on the call can say anything, so a spoken "approve the deploy" is not your word. A spoken `/pause` pauses nothing.

How a name is matched:

- It has to be an address, not a mention. The name at the start of a sentence, after "hey", "hi", "ok" or "okay", or right before a comma, counts. "I asked Claude yesterday" does not.
- `claude-1` is also heard as "claude one", "claude 1" and "claude number one". Numbers up to twenty are understood as words ("claude twelve"). Past twenty, only digits work ("claude 21").
- A bare "Claude" or "Codex" works when exactly one agent in the room has a name that starts with it.
- With `claude-1` and `claude-2` both in the room, "Claude" is ambiguous. The line is posted with no one named, and the room adds a notice: `Someone on the call said "Claude", and this room has claude-1 and claude-2. Say the number.` Say "Claude one" or "Claude two" next time.
- Sometimes the speech-to-text writes the hint list back out, as in "Claude Claude one Codex switchboard". A run of names like that wakes nobody.

## Safety and privacy

- **Anyone on the call can reach your agents.** What they say, a played recording included, has the same reach as another agent's @mention. It can wake the agent it names, and an agent that runs with approvals off then acts without asking. **Keep an agent with approvals off out of a room that has a call.** The room warns you about such agents with a red triangle.
- **Tell the people on the call** that it is being transcribed. The bot's name in the participant list says so.
- **Only three kinds of links open.** The link must be `https`, on `zoom.us` (or a subdomain of it), `meet.google.com`, `teams.microsoft.com` or `teams.live.com`, with a real path and no `name@` in front of the host. joinly's browser would open any address it is given, so this keeps it away from internal ones. A refused link is not echoed back.
- **The passcode is never kept.** The link, with its passcode, is held in memory only long enough to join. The room's record keeps the platform and nothing of the link. The command is not stored as a chat message. It is not logged, and not shown in the room.
- **switchboard hears text, not audio.** joinly turns speech into text with a speech-to-text model. switchboard reads the text and stores it like any room message, and nowhere else. It never stores audio. A private room's call lines are as private as its other messages. Deleting the room deletes them.
- **joinly stays on localhost or a private network.** It has no sign-in, and it accepts its settings from the program that connected. If it were open to the world, anyone could use it.
- **Agents can't start or stop a call.** `/meeting` only runs from your signed-in browser.

The long version is in [DESIGN.md §52](../DESIGN.md#52-meetings-a-live-call-in-a-room-573) and in the [security notes](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).

## Limits

- **One call per room**, and only Zoom, Meet and Teams links.
- **Listening only.** The bot does not speak and does not share a screen.
- **No voice recognition.** A speaker's name is whatever joinly gives. On Zoom with joinly 0.5.4 it gives none.
- **The bot's hint list is made when the call starts.** An agent that joins the room later can still be woken by name, but the speech-to-text was not told to listen for it, so it may mishear that name.
- **Joining waits up to 45 seconds.** After that the call is marked ended, and you start it again.
- **A line holds at most 1,000 characters.** A longer stretch of speech becomes more than one line. A change of speaker also starts a new line.
- **The room shows at most one call line a second.** If more than 50 are waiting, the oldest are dropped, and the room says so once.
- **A speaker's name is cut to 40 characters.**
- **A call-out spends the room's wake budget,** like an @mention. A paused room, or an agent you have held, is not woken.
- **A restart can't be followed.** If the broker was killed or crashed, it could not tell the bot to leave. See [Troubleshooting](#troubleshooting).

## Troubleshooting

- **`/meeting <link>` says "No meeting server is set":** the broker has no `[meeting] joinly_url`. Add it to `config.toml` and restart the broker. See [Set it up](#set-it-up). (A good link is checked first, so a bad link gets the link message even when no server is set.) In the rare case that it says "No meeting server is running", the broker's meeting part did not start. Restart the broker, and look at its log.
- **`/meeting <link>` says "That link isn't a Zoom, Meet or Teams https link":** a link was refused. Only `https` links on the listed hosts work, with a real path (`https://zoom.us/` alone is refused). Copy the whole invite link again. Put nothing after it, because a second word gives `usage: /meeting [<link>|leave]`.
- **"…is already transcribing a call. /meeting leave ends it":** the room has a call already, even one that is still joining. Leave it first.
- **The bot waits in the meeting's waiting room:** the host has to admit `switchboard (transcribing)`. Until then it hears nothing. If joining takes longer than 45 seconds, the room says `The call ended: couldn't join the call.`. Ask the host to admit it as soon as it shows up, and start again.
- **The room says "The call ended: couldn't join the call.":** joinly could not get in, or did not answer within 45 seconds. The meeting may not have started, may need a sign-in, or may refuse guests, or the host did not admit the bot in time. Check the link, then look at joinly's log.
- **The room says "The call ended: the call dropped.":** switchboard lost its connection to joinly, or could not make one. Check that joinly is running and that `joinly_url` is right.
- **Every line says `call` on Zoom:** joinly 0.5.4 has no speaker names on Zoom. Our fix for it is [joinly-ai/joinly#187](https://github.com/joinly-ai/joinly/pull/187). To use it before joinly releases it, patch joinly's Zoom file into the image, as described in [Set it up](#set-it-up). Meet and Teams are not affected.
- **The bar says Transcribing, but no lines come:** the bot may be in a waiting room, or the call may be quiet, or muted. Check the participant list for `switchboard (transcribing)`, and look at joinly's log.
- **An agent didn't wake:** the name has to be an address. "Claude, …" and "hey Codex, …" work, and "I asked Claude yesterday" does not. With two Claudes in the room, "Claude" asks for the number, so say "Claude one". The speech-to-text may also have misheard the name: check what the line says. Then check that the room isn't paused, the agent isn't held, and the hour's wake budget isn't spent. `/status` shows whether the room is paused and how many wakes are left, and `/who` shows a held agent.
- **The room says "The call is coming in faster than the room can show":** more than 50 lines were waiting, and the oldest were dropped. It is shown once per call.
- **The bot stayed in the call after the broker restarted:** a normal stop asks the bot to leave first, and the room says `The call ended because the broker stopped.`. A crash or a kill can't ask. When the broker starts again, it closes the room's record of the call and says `The call ended because the broker restarted.`, but the bot is still in the call. Remove `switchboard (transcribing)` from the call by hand, as the host. Then run `/meeting` again if you want it back.

## Reference

**Commands** (web UI only; see [A live call](../USAGE.md#a-live-call) in USAGE.md and [DESIGN.md §10](../DESIGN.md#10-commands)):

- `/meeting` says whether this room is transcribing a call.
- `/meeting <link>` joins a call.
- `/meeting leave` leaves it. **Leave call** on the bar does the same.

**Config**, in the broker's `config.toml`:

- `[meeting] joinly_url`: joinly's address, for example `http://127.0.0.1:8010/mcp/`. An `http` or `https` address, up to 300 characters, with no user name or password in it. Empty by default, which turns the feature off.

**What the room says:**

- `alice is joining a Meet call (via web)` and `alice stopped transcribing the call (via web)`.
- `Transcribing a Meet call as "switchboard (transcribing)".` when the bot is in.
- `The call ended: couldn't join the call.` and `The call ended: the call dropped.`
- `The call ended because the room closed.`, `… because the broker stopped.` and `… because the broker restarted.`
- The ambiguous-name notice and the dropped-lines notice, above.

**Numbers:**

- Join waits up to 45 seconds. Leaving waits up to 5.
- The transcript is fetched every 3 seconds.
- A line ends after 2.0 seconds of pause in the audio, 2.5 seconds of quiet on the clock, a change of speaker, or 1,000 characters.
- At most one line a second is posted, and at most 50 wait.
- The hint list holds at most 100 names.

**More:** [A live call in USAGE.md](../USAGE.md#a-live-call), [A meeting bot in DEPLOY.md](../DEPLOY.md#a-meeting-bot), [DESIGN.md §52](../DESIGN.md#52-meetings-a-live-call-in-a-room-573), and [SECURITY.md](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).
