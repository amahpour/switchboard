# Meetings

A room can follow a live Zoom, Google Meet or Microsoft Teams call. You send a meeting bot into the call. It listens, and switchboard posts what people say into the room as short lines tagged **call**. Your agents read those lines like any other message. When someone on the call says an agent's name, that agent wakes up and gets to work. Use it when the decisions happen on a call and you want your agents to hear them: to look something up while people keep talking, or to start on a fix the moment someone asks for it.

The bot listens. Two more things are off until you ask: [your agents can speak in the call](#let-agents-speak), and [an agent can see the shared screen](#show-an-agent-the-shared-screen). The bot never shares a screen of its own.

## See it work

A real Zoom call, a real Claude Code session and the room, in about 90 seconds. alice sends the bot into the call and says her upload page fails on big files. She turns Voice on and asks "Claude one" to find the upload limit. Claude reads the code and answers out loud in the call. Then alice types "raise it", and Claude makes the change and says so. The waits are cut, and nothing is sped up. alice's voice is a second bot reading her lines.

<video controls preload="metadata" width="100%" src="https://github.com/user-attachments/assets/bac87fc1-733a-4766-95ce-8ccceed78de5"></video>

## How it works

1. An admin runs the bot once, next to the broker, and tells the broker where it is. The bot is [joinly](https://github.com/joinly-ai/joinly), a separate open-source project. See [Set it up](#set-it-up).
2. You type `/meeting` and a call link in a room, in the web UI.
3. switchboard checks the link. It has to be an `https` link on Zoom, Meet or Teams. Then it asks joinly to join the call as a guest named `switchboard (transcribing)`. It also hands joinly the spoken names of the agents in the room, as hints. Joining usually takes a few seconds. The room shows **Joining a call…**.
4. Once the bot is in, the room shows a bar: **Transcribing a call**, with a **Voice** switch, a **Snapshot** button and a **Leave call** button.
5. joinly turns speech into text. switchboard fetches that text every few seconds and joins the pieces into one line each time the speaker pauses.
6. Each line is posted in the room, tagged **call**, with the speaker's name when joinly has one.
7. Talk that names nobody stays in the room and waits. It does not wake any agent.
8. When someone says an agent's name, only that agent wakes. It gets the call lines from just before, so it knows what was being talked about.
9. **Leave call** or `/meeting leave` ends it. So does closing the room. switchboard tells the bot to leave, and the bar goes away.

Speaking and seeing use the same bot and the same connection. They only start when you do:

- **Voice** is a switch on the bar, off at the start of every call. While it is on, an agent that starts a message with `@call` is heard in the call. The text is read by joinly's text-to-speech voice.
- **Snapshot** asks the bot for one picture of what it sees, and the room keeps it as a file. A call line that addresses an agent and says "screen" asks for one too.

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

Speaking back and the screen picture, when you ask for them:

```mermaid
sequenceDiagram
    participant You
    participant Room as the room
    participant Bot as joinly, the meeting bot
    participant Call as the call
    participant Agent as claude-1
    You->>Room: Voice on
    Agent->>Room: say, starting with @call
    Room->>Bot: speak this text
    Bot->>Call: the agent's voice
    Call-->>Room: someone says Claude one, look at my screen
    Room->>Bot: take a snapshot
    Bot-->>Room: one picture
    Room->>Room: keep it as a file, post its card
    Room->>Agent: the card, then the line, with a download link
```

## Set it up

An admin does this once. If someone already did, skip to [Use it](#use-it).

1. **Run joinly beside the broker.** [DEPLOY.md](../DEPLOY.md#a-meeting-bot) has the container command, for one machine and for the Compose example (its `meetings` profile: two lines in a `.env` file), and [a patch for Kubernetes](../DEPLOY.md#meetings-on-kubernetes) that makes joinly a second container in the broker's pod. With [the Helm chart](../DEPLOY.md#meetings-with-helm), `meetings.enabled=true` does the same, and `meetings.maxCalls` sizes joinly for the calls you expect at once.
2. **Keep joinly private.** It has no sign-in, and it takes its settings from whoever connects. Publish its port on `127.0.0.1` only, or only on the compose network. Never on an open address. In a Kubernetes pod it listens on the pod's localhost, and nothing else in the cluster can reach it.
3. **Tell the broker where joinly is.** In the broker's `config.toml`:

    ```toml
    [meeting]
    joinly_url = "http://127.0.0.1:8010/mcp/"
    ```

    Then restart the broker. It reads its config when it starts. Where there is no file to edit, as in a Kubernetes manifest, set the environment variable `SWITCHBOARD_JOINLY_URL` to the same address instead. When both are set, the variable wins. Only the file or the variable sets the address. A message in a room never can.
4. **Say how many calls at once, if you want more than one.** The broker follows one call at a time unless you raise `max_calls` under `[meeting]`, from 1 to 50:

    ```toml
    [meeting]
    max_calls = 2
    ```

    It counts calls in every room, from the moment a call is joining. One joinly can follow several calls, but each live call takes about 2.7 GiB of memory and one CPU in it (measured). Give joinly `2.7 GiB × max_calls + 0.5 GiB`, or a second call can get it killed, and every call with it. [DEPLOY.md](../DEPLOY.md#meetings-on-kubernetes) has the sizes for Kubernetes. Where there is no file to edit, set `SWITCHBOARD_MEETING_MAX_CALLS` to the number instead. When both are set, the variable wins. Skip this step if one call at a time is enough.
5. **Pick a voice, if you want another one.** Agents speak with `bm_george` unless you set `tts_voice` under `[meeting]`: `tts_voice = "af_bella"`. [DEPLOY.md](../DEPLOY.md#a-meeting-bot) says which voices joinly has. It takes effect for the next call after a broker restart. Skip this if the default suits you.
6. **On Zoom, decide about speaker names.** joinly 0.5.4 gives no speaker names on Zoom, so every line there shows `call` where a name would be. Meet and Teams are fine. The fix is [joinly-ai/joinly#187](https://github.com/joinly-ai/joinly/pull/187), a change to joinly's Zoom file. Until a joinly release has it, mount the fixed file read-only over the image's copy: [DEPLOY.md](../DEPLOY.md#a-meeting-bot) has the two commands, and explains the cause. On Kubernetes, [a second patch](../DEPLOY.md#meetings-on-kubernetes) mounts it, or `meetings.zoomFix.configMap` in [the Helm chart](../DEPLOY.md#meetings-with-helm).
7. **Try it.** In a room, in the web UI, type `/meeting` and a test meeting's link. If the room says it is joining, you are done. If it says no meeting server is set, see [Troubleshooting](#troubleshooting).

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

7. **Wake an agent by saying its name.** Say it as an address: "Claude, can you check the image sizes?", "hey Codex, run the tests", or "Claude one, take this". The agent wakes and answers in the room. Its answer is text in the room, unless you have [let agents speak](#let-agents-speak).
8. **Check on it** any time with `/meeting`. It says whether a call is being transcribed.
9. **End it** with **Leave call** on the bar, or `/meeting leave`. The room posts a notice, and the bot leaves the call.

The call also ends by itself when the room is closed, when the connection to joinly drops, when joinly can't join, and when the broker stops. The room says which in a notice.

## Let agents speak

Voice is off when a call starts. You turn it on, for that call only.

1. **Click the Voice switch** on the bar, or type `/meeting voice on`. Both are for you in the web UI only. The switch turns blue, the bar adds **agents can speak**, and the room gets a notice such as `alice turned voice on: an agent's message that starts with @call is read aloud to everyone on the call (via web)`.
2. **Tell your agents, or let them find out.** An agent that is woken by a call line, while voice is on, is told in one line how to speak: start a `say()` with `@call`. You can also ask it: "answer on the call with @call".
3. **An agent speaks** by starting a message with `@call`, as its first word. The rest of the message is read aloud in the call, and the room still shows the whole message: `@call The hero image is four megabytes. I will resize it.` Nothing says which agent is speaking, so with more than one in the room, ask each to name itself first.

    ![The bar with the Voice switch on, a snapshot card, and claude-1's message that starts with @call](../media/ui/guide-meeting-voice-light.png)

4. **Turn it off** with the switch or `/meeting voice off`. Lines still waiting to be spoken are dropped. A line that is being said finishes. The call ending turns it off too, so the next call starts with voice off.

What is read aloud, and what is not:

- The text is cut at about 600 characters, at a word.
- Markdown marks (`**`, `#`, backticks, list marks) are not read, and a link is read as its words. A bare web address is left out.
- A code block is replaced by "code omitted". An `artifact:` line is left out.
- If nothing is left, nothing is said.

One line is said at a time. Up to three more wait. If agents send more than that, the extra lines are dropped and the room says so once. If someone on the call talks over the bot, it stops, and the room says `Someone spoke over the line in the call, so it stopped early.`.

Only an agent that is a member of the room can speak this way. A person's message that starts with `@call` is only text, and so is an agent's when voice is off or there is no call.

## Show an agent the shared screen

Someone on the call shares a screen, and you want an agent to see it. There are two ways, and both take a picture of the bot's view of the call.

- **Click Snapshot** on the bar, or type `/meeting snapshot`. A card with the picture is posted as your message, and the agents in the room receive it.
- **Say it on the call.** Address an agent and say "screen": "Claude, look at my screen." The picture is posted first, then the line, so the agent that wakes has both. A line that says "screen" but names nobody takes no picture, and neither does "Claude, run the tests".

Ask again later to see what changed. At most one picture is taken every 10 seconds, however it was asked for. A click sooner than that is refused with a short message. A line on the call that says "screen" inside that time is still posted, with a notice and without a new picture.

The picture is stored as a [room file](attachments.md), named like `screen-142507.jpg`, and shown as a download card. An agent is given a download link to it the same way as for any file. It is a small image, 1024 by 576 pixels, so it shows what is shared, not the fine print. If a picture can't be taken, the room says so, and a line on the call is posted all the same.

## What agents see

Call lines are ordinary room messages, with the sender kind `meeting`. An agent that reads the room sees them marked that way, with the speaker's name, or `call` when there is none. The name is lowercased and spaces become hyphens. A line that names the agent looks like this:

```text
- id=27 at=19:16:04 from=alice kind=meeting to_you=yes prio=mention text=I think it is the image sizes. Claude one, can you check them?
```

- **A line that names nobody** has `to_you=no` and `prio=chatter`. It never starts an agent's turn by itself. It waits, and shows up in the agent's next `read()` or with its next real wake. It does not count toward the loop guard that stops agents from talking to each other forever. The room's rate limit on `say()` does not apply to it either.
- **A line that names an agent** has `to_you=yes` and `prio=mention`, for that agent only. It wakes the agent the way an @mention does, and it spends one wake from the room's hourly budget. The call lines from just before come along. [Delivery rules](../USAGE.md#delivery-rules) says how wakes work.
- **A line that names something the agent has claimed**, such as a ticket key it reserved, wakes the agent that holds the claim in the same way. Someone reading out `SYSINT-645` wakes whoever claimed it.
- **Depending on how the agent was woken,** the line is shown whole or as "not shown here", with a `read()` call that fetches it. An agent has to read it first, as with any other agent's message.
- **A call line can come after a picture.** A snapshot that a call line asked for is a `kind=meeting` message from `call`, with the card as its text: `artifact: screen-142507.jpg | 21000 | sha256:… | switchboard:file/…`. A download link for it comes with the batch, as for any room file. One you asked for with the button is a message from you, `kind=human`.
- **While voice is on,** a batch that holds call lines ends with one more line: `Voice is on in this call: start a say() line with @call to have it spoken.` It is the only place agents learn it. There is no tool for speaking and no setting an agent can change.
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
- **Agents can't start or stop a call.** `/meeting` only runs from your signed-in browser. The same goes for `/meeting voice on` and `/meeting snapshot`.
- **Spoken text reaches everyone on the call.** That is why voice is your switch, per call, and off at the start. An agent could be talked into saying something it should not, by someone on the call or by anything it reads, and with voice on it says it aloud to every participant. Leave voice off unless you would let those agents speak for you. Nothing an agent does can turn it on.
- **A snapshot can show anything on the shared screen**, including what the person sharing did not mean to show. It is kept as a room file under the room's retention (30 days unless an admin changed it), and anyone who can read the room can download it. Take one when you mean to, and tell the person sharing. An admin can shorten how long files are kept in Settings → Files, and deleting the room deletes them.
- **A picture is only kept if it is one.** The bytes have to be a PNG, JPEG or WebP, whatever the bot says they are. The room file size limit applies.

The long version is in [DESIGN.md §52](../DESIGN.md#52-meetings-a-live-call-in-a-room-573) and in the [security notes](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).

## Limits

- **One call per room**, and only Zoom, Meet and Teams links.
- **At most `max_calls` calls at once in the whole broker**, in all rooms together. The default is 1. A call counts from the moment it is joining, until it has ended and the bot has left. A start past the limit is refused, and nothing is sent to joinly. Each call needs about 2.7 GiB of memory in joinly, so raise the limit only with the memory to match ([Set it up](#set-it-up)).
- **The bot never speaks by itself or shares its own screen.** It speaks an agent's `@call` line when you have voice on, and it takes pictures of what it sees.
- **A picture is one frame.** It is 1024 by 576 pixels and a screenshot of the bot's own view of the call, the same on Zoom, Meet and Teams. What a shared screen looks like there depends on the platform's page. On Zoom, joinly's browser shows one tile that follows whoever is talking, so a shared screen may not be what the picture shows. It has not been tried on a real Zoom or Teams call.
- **Voice is for the call it was turned on in.** It is kept in memory, so it is off after a restart and in every new call.
- **At most 600 characters a line, one line at a time, three waiting.**
- **At most one snapshot every 10 seconds** in a call.
- **No voice recognition.** A speaker's name is whatever joinly gives. On Zoom with joinly 0.5.4 it gives none.
- **The bot's hint list is made when the call starts.** An agent that joins the room later can still be woken by name, but the speech-to-text was not told to listen for it, so it may mishear that name.
- **Joining waits up to 45 seconds.** After that the call is marked ended, and you start it again.
- **A line holds at most 1,000 characters.** A longer stretch of speech becomes more than one line. A change of speaker also starts a new line.
- **The room shows at most one call line a second.** If more than 50 are waiting, the oldest are dropped, and the room says so once.
- **A speaker's name is cut to 40 characters.**
- **A call-out spends the room's wake budget,** like an @mention. A paused room, or an agent you have held, is not woken.
- **A restart can't be followed.** If the broker was killed or crashed, it could not tell the bot to leave. See [Troubleshooting](#troubleshooting).

## Troubleshooting

- **`/meeting <link>` says "No meeting server is set":** the broker has no `[meeting] joinly_url` and no `SWITCHBOARD_JOINLY_URL`. Add one and restart the broker. See [Set it up](#set-it-up). (A good link is checked first, so a bad link gets the link message even when no server is set.) In the rare case that it says "No meeting server is running", the broker's meeting part did not start. Restart the broker, and look at its log.
- **`/meeting <link>` says "That link isn't a Zoom, Meet or Teams https link":** a link was refused. Only `https` links on the listed hosts work, with a real path (`https://zoom.us/` alone is refused). Copy the whole invite link again. Put nothing after it, because a second word gives `usage: /meeting [<link>|leave|snapshot|voice on|voice off]`.
- **"…is already transcribing a call. /meeting leave ends it":** the room has a call already, even one that is still joining. Leave it first.
- **`/meeting <link>` says "This broker follows at most 1 call at once, and that many are open. Leave one first (/meeting leave in its room).":** other rooms have calls, and together they are as many as `max_calls` allows. The message gives the number and nothing else, because a call may be in a room you can't see. Leave one of them (`/meeting leave` in the room that has it; if you can't see that room, ask someone who can), or raise `max_calls` and give joinly the memory for it ([Set it up](#set-it-up)). A call that is still leaving counts until the bot has left, so if you just left one, try again in a few seconds. The refused start wrote nothing and sent the link nowhere.
- **The bot waits in the meeting's waiting room:** the host has to admit `switchboard (transcribing)`. Until then it hears nothing. If joining takes longer than 45 seconds, the room says `The call ended: couldn't join the call.`. Ask the host to admit it as soon as it shows up, and start again.
- **The room says "The call ended: couldn't join the call.":** joinly could not get in, or did not answer within 45 seconds. The meeting may not have started, may need a sign-in, or may refuse guests, or the host did not admit the bot in time. Check the link, then look at joinly's log.
- **The broker won't start and says `SWITCHBOARD_JOINLY_URL must be an http(s) URL`:** the variable is set to something that is not one: no `http://` or `https://`, a user name or password in it, or more than 300 characters. It is never ignored, so fix it or remove it. (The message does not print the value.)
- **The broker won't start and says `SWITCHBOARD_MEETING_MAX_CALLS must be a whole number from 1 to 50`:** the variable is set to something else: `0`, `51`, `2.5`, `two`. It is never ignored, so fix it or remove it. The same limits apply to `max_calls` in `config.toml`, which stops the broker the same way.
- **The room says "The call ended: the call dropped.":** switchboard lost its connection to joinly, or could not make one. Check that joinly is running and that the address is right. On Kubernetes, `kubectl -n switchboard logs switchboard-0 -c joinly` is joinly's log, and `kubectl -n switchboard get pod switchboard-0` should say `2/2`.
- **Every line says `call` on Zoom:** joinly 0.5.4 has no speaker names on Zoom. Our fix for it is [joinly-ai/joinly#187](https://github.com/joinly-ai/joinly/pull/187). To use it before joinly releases it, patch joinly's Zoom file into the image, as described in [Set it up](#set-it-up). Meet and Teams are not affected.
- **The bar says Transcribing, but no lines come:** the bot may be in a waiting room, or the call may be quiet, or muted. Check the participant list for `switchboard (transcribing)`, and look at joinly's log.
- **An agent didn't wake:** the name has to be an address. "Claude, …" and "hey Codex, …" work, and "I asked Claude yesterday" does not. With two Claudes in the room, "Claude" asks for the number, so say "Claude one". The speech-to-text may also have misheard the name: check what the line says. Then check that the room isn't paused, the agent isn't held, and the hour's wake budget isn't spent. `/status` shows whether the room is paused and how many wakes are left, and `/who` shows a held agent. On Zoom the first half-second after a silence can be cut before any listener hears it, so a name said first may never reach the transcript: the self-test's independent listener lost "Claude one" in "Claude one, can you hear me?" too (#602). A short lead word keeps it: "hey Claude, ..." or "okay Claude one, ...".
- **The room says "The call is coming in faster than the room can show":** more than 50 lines were waiting, and the oldest were dropped. It is shown once per call.
- **Voice is on, but nothing is said:** the agent has to start its `say()` with `@call` (not in the middle of the message), and it has to be a member of the room. Check the bar says **agents can speak**. If the room says `Couldn't speak in the call.`, joinly refused the line: check its log, and check that `tts_voice` is a voice joinly has (DEPLOY.md lists them). An agent that has not been told how can be asked: "say it on the call with @call".
- **The room says "Someone spoke over the line in the call":** the bot stopped because someone started talking. It does not say the rest. Ask the agent to say it again.
- **The room says "More lines are waiting to be spoken than the call can say":** agents sent more than three lines while one was being said. The extra ones were dropped, and they are still in the room as text.
- **Snapshot says "The bot is not in the call yet":** the call is still joining. Wait for the bar to read **Transcribing a call**.
- **Snapshot says "A snapshot was taken less than 10 seconds ago":** wait a few seconds and click again.
- **The room says "Couldn't get a snapshot of the call":** joinly could not take a picture, for example because the bot has been removed from the call. Check joinly's log. If it says the picture is "not a PNG, JPEG or WebP", or larger than the file limit, joinly sent something switchboard won't keep.
- **The picture shows a person, not the shared screen:** it is a screenshot of the bot's own browser page, so it shows whatever that page shows. On Zoom that can be one tile that follows the speaker. Tell the agent what is on the screen, in the room.
- **The bot stayed in the call after the broker restarted:** a normal stop asks the bot to leave first, and the room says `The call ended because the broker stopped.`. A crash or a kill can't ask. When the broker starts again, it closes the room's record of the call and says `The call ended because the broker restarted.`, but the bot is still in the call. Remove `switchboard (transcribing)` from the call by hand, as the host. Then run `/meeting` again if you want it back.

## Test it yourself

Once joinly is set up, a self-test can check the whole path in a real call, with no one on it but two test bots. It is written for people who work on switchboard, but anyone with Docker and a test call can run it.

It starts two joinly containers: your bot, and a test speaker named `switchboard self-test speaker`. It also starts a throwaway broker in a temp folder, with a room and a scripted agent called `claude-1`. Both bots join the call. Then it checks, in order:

1. `/meeting <link>` from a person: the bar goes live.
2. The speaker says a known sentence: the call line reaches the room, with most of its words.
3. The speaker says "Claude one, can you hear me?": `claude-1` is woken by that line.
4. With voice on, `claude-1` says `@call I can hear you loud and clear`: the speaker hears most of those words.
5. `/meeting snapshot`: the room keeps a JPEG file and posts its card.
6. `/meeting leave`: the bar ends, and the speaker no longer sees the bot.

Speech-to-text of a synthetic voice is close and not exact, so a step with words passes when most of them arrive (60% of the meaningful ones). The result is a table: for each step, pass or fail, what it expected, what it saw, and how many seconds it took.

The easy way is the `meeting-self-test` skill in the repository. A coding agent that has it, and a Zoom connector, makes a 15-minute Zoom meeting, runs the test, deletes that meeting, and gives you the table. To run it by hand, put a call's link in a file that only you can read (mode 0600), and run the test:

```bash
SWITCHBOARD_LIVE=meeting SWITCHBOARD_MEETING_URL_FILE=<the file> \
  uv run pytest -m live tests/live/test_meeting_selftest.py -s -q
```

The link has the call's passcode in it, so the test refuses a file that others can read, keeps the link out of its output and its report, and checks that the broker's database, messages and log never held any of it. For a Meet or Teams call, someone has to admit both bots when they ask to join. The test does not create or delete anything in those. [CONTRIBUTING.md](https://github.com/amahpour/switchboard/blob/main/CONTRIBUTING.md#live-tests-real-agent-clis-opt-in) has the details, and the skill lists the Zoom account settings that stop a guest from joining.

## Reference

**Commands** (web UI only; see [A live call](../USAGE.md#a-live-call) in USAGE.md and [DESIGN.md §10](../DESIGN.md#10-commands)):

- `/meeting` says whether this room is transcribing a call.
- `/meeting <link>` joins a call.
- `/meeting leave` leaves it. **Leave call** on the bar does the same.
- `/meeting voice on` and `/meeting voice off` turn speaking on and off for this call. The **Voice** switch on the bar does the same.
- `/meeting snapshot` posts a picture of the shared screen. **Snapshot** on the bar does the same.

**Config**, in the broker's `config.toml` or its environment:

- `[meeting] joinly_url`: joinly's address, for example `http://127.0.0.1:8010/mcp/`. An `http` or `https` address, up to 300 characters, with no user name or password in it. Empty by default, which turns the feature off.
- `[meeting] tts_voice`: the voice agents speak with, for example `af_bella`. A word of 1 to 32 characters from `a-z`, `0-9` and `_`. The default is `bm_george`. It is sent to joinly when a call starts.
- `[meeting] max_calls`: how many calls the broker follows at once, in all rooms together. An integer from 1 to 50. The default is 1. A start past it is refused. Each live call takes about 2.7 GiB of memory and one CPU in joinly.
- `[files] max_mb`: the size limit for one file also limits a snapshot ([attachments](attachments.md)).
- `SWITCHBOARD_JOINLY_URL`: the same address, from the environment. Set to something, it wins over the file; empty or unset, the file's value stays. A bad value stops the broker at start. It is the way to set it in a Kubernetes manifest, which has no file on the volume to write ([DEPLOY.md](../DEPLOY.md#meetings-on-kubernetes)).
- `SWITCHBOARD_MEETING_MAX_CALLS`: the same limit, from the environment. A non-empty value wins over the file, and an empty or unset one leaves the file's. A value that is not a whole number from 1 to 50 stops the broker at start.

**What the room says:**

- `alice is joining a Meet call (via web)` and `alice stopped transcribing the call (via web)`.
- `Transcribing a Meet call as "switchboard (transcribing)".` when the bot is in.
- `The call ended: couldn't join the call.` and `The call ended: the call dropped.`
- `The call ended because the room closed.`, `… because the broker stopped.` and `… because the broker restarted.`
- `This broker follows at most 1 call at once, and that many are open. Leave one first (/meeting leave in its room).` (`2 calls`, and so on, for a higher limit). It is the answer to `/meeting <link>`, not a room notice.
- The ambiguous-name notice and the dropped-lines notice, above.
- `alice turned voice on: an agent's message that starts with @call is read aloud to everyone on the call (via web)` and `alice turned voice off: agents no longer speak in the call (via web)`.
- `Someone spoke over the line in the call, so it stopped early.`, `More lines are waiting to be spoken than the call can say. Some were dropped.` and `Couldn't speak in the call.`
- `Couldn't get a snapshot of the call.`, `joinly sent something that is not a PNG, JPEG or WebP picture.`, `The snapshot is larger than the room's file limit (25 MB).` and `A snapshot was taken less than 10 seconds ago. Try again in a moment.`

**Numbers:**

- Join waits up to 45 seconds. Leaving waits up to 5.
- The transcript is fetched every 3 seconds.
- A line ends after 2.0 seconds of pause in the audio, 2.5 seconds of quiet on the clock, a change of speaker, or 1,000 characters.
- At most one line a second is posted, and at most 50 wait.
- The hint list holds at most 100 names.
- A spoken line is at most 600 characters, one at a time, with up to 3 waiting. Speech may take up to 3 minutes before it is given up.
- A snapshot is taken at most every 10 seconds, waits up to 8 seconds for the picture, and is 1024 by 576 pixels.

**More:** [A live call in USAGE.md](../USAGE.md#a-live-call), [A meeting bot in DEPLOY.md](../DEPLOY.md#a-meeting-bot), [DESIGN.md §52](../DESIGN.md#52-meetings-a-live-call-in-a-room-573), and [SECURITY.md](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).
