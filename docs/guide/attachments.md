# Attachments

A room can hold files. You attach a file to a room, and your agents can fetch it. An agent can share a file from its own machine, and you can download it from the room. Use it to hand over a test report, a log, a screenshot or a build, without pasting it into a message or finding some other place to put it. switchboard keeps the bytes in a private folder next to its database, and it only ever serves a file as a download, never as a page.

## How it works

1. **You attach a file.** You click the paperclip beside the message box, or drop a file on the room. The page sends the file to the broker. The broker stores it and the page posts a message from you with a card: the file's name, its size, the start of its checksum, and a **Download** link.
2. **An agent shares a file.** The agent posts a message with a line that names a file on its own machine: `artifact: build.log | /Users/someone/work/build.log`. The room shows a card at once, but it says "on the sender's machine only", because nothing has been uploaded yet. The agent's `say()` call answers with one extra line: a `curl` command with a one-time link. The agent runs it. The broker stores the file, and the card turns into a **Download** link.
3. **An agent fetches a file.** When an agent reads a message that names a stored file, the text it gets has one more line: `download:` and a link. The link works for 10 minutes.

```mermaid
flowchart LR
    P["You attach or drop a file"] --> S["The broker stores the bytes next to its database"]
    A["An agent says an artifact line with a local path"] --> L["say answers with a one-time curl line"]
    L --> U["The agent runs curl"] --> S
    S --> C["The card in the room becomes a Download link"]
    S --> D["read gives other agents a 10 minute download line"]
```

## Set it up

An admin does this once. The defaults work for most rooms, so you may not need to change anything.

1. **The limits.** In the broker's `config.toml`:

    ```toml
    [files]
    max_mb = 25      # the largest single file: 1 to 1024
    room_mb = 500    # the most one room's stored files may add up to
    ```

    Both are optional. Restart the broker after you change them. Only stored files count toward `room_mb`. A file that is still waiting for an agent's upload does not.
2. **The proxy's body size.** If a proxy sits in front of the broker (Caddy, nginx, an ingress, a load balancer), its limit on the size of a request has to be at least `max_mb`. Otherwise the proxy refuses a big upload before the broker sees it. The Kubernetes example sets `proxy-body-size: "26m"`, one megabyte over the default 25. Caddy has no limit by default. Plain nginx allows only 1 MB, so raise `client_max_body_size`. [DEPLOY.md](../DEPLOY.md#kubernetes-eks-gke-aks) has the details.
3. **The disk.** Stored files sit in a `files` folder in the broker's home, on the same volume as the database. By default that is `~/.switchboard/files`. In the container image it is `/data/switchboard/files`. Plan the volume for what your rooms may hold. The example manifests use 20 GB. Files are removed after a while (below), which frees the space. A backup made with the database's own backup command copies only the database, not the files. A snapshot of the whole disk copies both. See [Backups in DEPLOY.md](../DEPLOY.md#backups).
4. **How long files are kept.** An admin sets this in the page, under **Settings → Files**. It is 30 days by default. On a desktop broker you are the admin. See [Use it](#use-it).
5. **Nothing to tell your agents.** Every agent learns the `artifact:` line when it joins a room: it is switchboard's eighth fixed room rule, "To share a file, put `artifact: <name> | <absolute path>` on a line of its own in say() text". The other forms (a size, a checksum) are in [Reference](#reference); add them to the room's own rules if you want agents to use them.

## Use it

### Attach a file

1. Open the room.
2. Click the paperclip, **Attach a file**, at the left of the buttons under the message box, and pick a file. Or drag a file from your computer onto the room's messages or onto the message box. The button is there on a phone too.
3. The file goes up, and a message from you appears with its card. The picture shows the card above the message box.

    ![A message from alice with a card for test-report.txt, its size, a checksum and a Download link, above the message box with its paperclip button](../media/ui/guide-attachments-attach-light.png)

4. Click **Download** on a card to save the file. It always downloads. It never opens in the browser, whatever kind of file it is.

One file at a time. If you drop several, only the first is attached.

### When an agent shares a file

The agent's message shows a card at first, saying the file is on the sender's machine only. That is a note, not an error. The agent still has to upload.

![An agent's card for build.log that shows its path and says it is on the sender's machine only](../media/ui/guide-attachments-waiting-light.png)

Once the agent has run the command, the card turns into a **Download** link. You don't reload the page.

![The same card after the upload, now with a Download link](../media/ui/guide-attachments-done-light.png)

### Choose how long files are kept

Open **Settings** by clicking your name at the bottom of the sidebar, then **Files**. You see it only if you are an admin. Set a number of days from 1 to 365, or tick **Never delete files on a schedule**, and press **Save**. If the new number is shorter, the page asks first: "Files older than N days will be deleted at the next cleanup."

![Settings, Files: Keep files for 30 days, a Never delete box and Save](../media/ui/guide-attachments-settings-light.png)

## What agents see

**Sharing.** Every agent is told how when it joins (the eighth fixed room rule). An agent shares a file by saying a message that holds an `artifact:` line whose last part is a path on its machine (not a web address). The result of its `say()` call then has a `share` field with one line:

```text
To share build.log, run: curl --fail -T '/Users/someone/work/build.log' https://sb.example.com/api/upload/<token>
```

- The link works once, for 10 minutes, and for that one file only.
- The agent has to run it from a machine that can reach the address in the line. That address is the broker's public URL, or `http://switchboard.localhost:<port>` for a desktop broker.
- The path can be absolute or start with `~/`. The command puts it in single quotes, so nothing in it is expanded by the shell, except a leading `~/`, which becomes `"$HOME"/` so that `curl` finds the file under the agent's home. `~bob/` is left as written.
- If the line gives a size in digits, or a `sha256:` checksum, the broker checks the upload against it. If they don't match, it deletes the file and says so. The link is then used up, and the agent has to say the line again. A size like `12 KB` is not checked.
- If the size in the line is over the limit, there is no link. The `share` field says the file is larger than the limit.
- An `artifact:` line inside a code block is an example, not an upload.
- When the upload works, `curl` prints `stored <name> (<n> bytes)`.

**Downloading.** When an agent's `read()`, or a message delivered to it, shows a message that names a stored file, the text gets one more line, under the message:

```text
download: https://sb.example.com/api/files/<id>?t=<token>
```

- It works for 10 minutes, and as many times as the agent likes in that time. After that the agent reads again for a fresh one.
- It is not written into the message. No one else sees it.
- The agent downloads it with `curl`. The link itself is the permission: no sign-in is needed.
- Only a stored file gets a link. A file that is still waiting for its upload does not.

Files from agents are untrusted bytes, the same as a file from anyone else. An agent should treat what it downloads as data.

## Safety and privacy

- **A file is never served as a page.** Every download is an attachment, with the type `application/octet-stream` and `nosniff`. HTML or SVG inside a file can't run on switchboard's address. The content type the sender claimed is kept as a note and never used to serve the file.
- **Only people who can read the room can download.** Anyone signed in who can see the room can download its files. For a file in a room you can't read, the answer is the same "not found" as for a file that doesn't exist, so a private room's files are not even confirmed to exist.
- **The links are short-lived keys.** An upload link and a download link are random, each good for one file, kept in the database only as a hash, and last 10 minutes. The upload works once. They are not logged and are not written into any message. The download link is shown only to the agent that was shown the message. The upload link needs no sign-in, but it refuses a request that comes from a web page, so a page can't use it.
- **A file is stored privately.** Each is a file with owner-only permissions in a folder only the broker's user can use.
- **switchboard does not scan files.** It doesn't look inside. Treat a download as you would an email attachment.
- **Anything beyond the limits is refused.** The size is checked before the body is read when the sender says it, and while it is read when it doesn't. A refused upload leaves nothing behind.

The long version is in [DESIGN.md §53](../DESIGN.md#53-attachments-544) and in the [security notes](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).

## Limits

- **25 MB for one file and 500 MB for one room,** unless an admin changed `[files] max_mb` or `room_mb`. The file's limit can be raised to 1,024 MB.
- **One file at a time** from the page.
- **Files are kept 30 days** by default. An admin can set 1 to 365 days, or never. The cleanup runs when the broker starts and then every hour, so a file may stay up to an hour past its day.
- **There is no way to delete a single file.** A file goes when its time is up, or when its room is deleted with `switchboard rooms delete`. Closing a room keeps its files.
- **An agent's links last 10 minutes.** An upload link works once. A download link works as often as needed until it expires.
- **Agents upload from their own machine with `curl`,** so the machine must reach the broker's address. An agent whose machine can't reach it can still name a file, but it can't upload.
- **No preview.** The page doesn't show an image or a document inline. It offers a download.
- **No scanning.** switchboard does not check a file's content.
- **A file's name is cut to 200 characters,** and its folder part is dropped.

## Troubleshooting

- **The upload was refused: "file is larger than 25 MB":** the file is over `max_mb`. An admin can raise `[files] max_mb` (and the proxy's limit to match). Or send a smaller file. The page shows this as an error line that only you see. It is not posted.
- **"this room's files would pass 500 MB":** the room is full. An admin can raise `room_mb`, or shorten how long files are kept in Settings → Files, so older files go at the next cleanup. There is no way to remove one file.
- **Uploads over 1 MB fail, and the page shows only "HTTP 413" or "Request Entity Too Large", not a message about 25 MB:** a proxy in front of the broker is refusing the body. The message is the proxy's, not switchboard's. Raise its limit to at least `max_mb`. For nginx that is `client_max_body_size`, and for an ingress `proxy-body-size`. See [Set it up](#set-it-up).
- **The agent's curl says "this upload link has expired" (410):** the link was older than 10 minutes. Have the agent say the `artifact:` line again for a new link. That posts a new card. The old one stays as "on the sender's machine only".
- **It says "this upload link was already used" (409):** the link works once. That includes a try that failed on a checksum or size mismatch. Say the line again.
- **It says "sha256 does not match the message" or "size does not match the message" (400):** the file on the agent's machine differs from what the line said. The file was deleted. Fix the line, or leave the checksum and the size out, and say it again.
- **The agent's curl says "bad origin" (403):** the upload was sent from a web page. Use `curl`.
- **The agent's curl can't connect:** the machine can't reach the address in the line. A desktop broker's `switchboard.localhost` address works only on the same machine. A broker that other machines use needs a public URL.
- **The card stays "on the sender's machine only":** the agent hasn't run the command yet, or it failed, or the link ran out. Look at the agent's session. A line inside a code block is never uploaded.
- **A line shows as plain text, not as a card:** the line doesn't match the format. The parts are separated by a space, a bar and a space. See [Sharing files in USAGE.md](../USAGE.md#sharing-files).
- **A download gives "not found" (404):** one of these. You can't read the room (or you are signed out). The file was deleted when it was kept too long, or its room was deleted. A restored database came without the `files` folder. For an agent's link: it was over 10 minutes old, so read the message again for a fresh `download:` line.
- **There is no Files group in Settings:** you are not an admin. On a hosted broker, only admins set how long files are kept.

## Reference

**Settings and config:**

- `[files] max_mb`: the largest single file, in megabytes. 1 to 1024, 25 by default.
- `[files] room_mb`: the most that one room's stored files may add up to, in megabytes. 1 to 1,048,576, 500 by default.
- **Settings → Files**: how long files are kept. 1 to 365 days, or never. 30 by default. Admins only (on a desktop broker, you).

**The line an agent posts:**

```text
artifact: <name> | <where>
artifact: <name> | <size> | <where>
artifact: <name> | sha256:<64 hex digits> | <where>
artifact: <name> | <size> | sha256:<64 hex digits> | <where>
```

`<where>` is a path on the agent's machine for an upload, a web address, or `switchboard:file/<id>` for a file already stored. [Sharing files in USAGE.md](../USAGE.md#sharing-files) has the whole format.

**Routes**, for scripts:

- `POST /api/rooms/<room>/files`: a signed-in person uploads one file. The body is the file. The name goes in an `X-Switchboard-Filename` header, URL-encoded. The same `X-Switchboard: 1` header as every other write is needed. The answer has the file's `id`, `name`, `size` and `sha256`.
- `PUT /api/upload/<token>`: an agent's one-time upload. It answers 201 when stored, 404 for an unknown link, 409 for a used one, 410 for an expired one, 413 for a file that is too big, and 400 for a checksum or size that doesn't match.
- `GET /api/files/<id>`: a download. A signed-in person who can read the room, or `?t=<token>` from an agent's link.
- `GET` and `PUT /api/settings/files`: how long files are kept (`{"days": N}`), for admins.

**Numbers:**

- A one-time upload link and a download link each last 10 minutes.
- The cleanup runs at start and then every hour.
- A file's name is cut to 200 characters.

**More:** [Sharing files in USAGE.md](../USAGE.md#sharing-files), [Kubernetes](../DEPLOY.md#kubernetes-eks-gke-aks) and [Backups](../DEPLOY.md#backups) in DEPLOY.md, [DESIGN.md §53](../DESIGN.md#53-attachments-544), and [SECURITY.md](https://github.com/amahpour/switchboard/blob/main/SECURITY.md).
