# A webhook when you are needed

When something needs you and you have not looked, switchboard can send one short JSON message to an address you choose: a Slack incoming webhook, a Discord webhook, an ntfy topic, Home Assistant, or Zapier and n8n on the way to a text. It says who needs you and where, with a link. It never carries the message.

## Set it up

1. Settings → Profile → **Webhook**.
2. Paste the address. It must start with `https://`, and can be up to 500 characters.
3. Pick how long to wait (5 minutes to start; 1 to 120). Press **Save**.
4. Press **Send a test**. If it goes through, your channel shows the line.

Once saved, the page shows only the address's host ("Posting to hooks.example.com") and the field stays empty. The rest of the address is a secret, so switchboard never shows it back. To change it, paste a new one. **Remove** deletes it.

## What sends, and what stops it

After the wait, switchboard posts once for each of these that still needs you:

- a message to you (an @mention, `@humans` or a reply);
- a review board waiting for your call;
- an agent of yours waiting on an approval prompt.

On a switchboard with more than one person, the first two come only from rooms you are a member of. An agent of yours waiting on approval counts in any room, since your agents stay in a room you leave.

**Opening it stops it.** Answering, deciding, or opening the room before the wait is over means nothing is sent. A tap on the link in the message opens the room, which does the same. If you do not look, it posts once, not again and again (and once more to a [second address](#a-second-louder-alert), if you give one).

A message looks like this in Slack: `scout has a message for you in #build: https://switchboard.example.com/#build`. The link only works where your switchboard can be reached from where you are.

If a post fails (the address is gone, it answers with an error), switchboard tries again a few times, five minutes apart, and then gives up. Settings shows the last result, to you only: "The last post failed: …".

## A second, louder alert

A line in Slack scrolls by, and a phone can be face down. Under **Second alert** you can give a second address for when the first was not enough: an ntfy topic at top priority, or a Zapier or n8n flow that sends a text or rings your phone.

1. Paste the second address. It has the same rules as the first: `https://`, public addresses only, and switchboard shows back only its host.
2. Pick how many minutes after the first alert (20 to start; 5 to 240). Press **Save second alert**.
3. Press **Send a test**: it now posts to both addresses and says how each went.

If something still needs you that long after its first alert was due, switchboard posts once to the second address. It goes whether or not the first post went through, since it is about you not having looked, not about the first channel working. Answering, deciding or opening the room stops it, as it stops the first. The post is the same one line, with one more field, `"alert": "second"`, so a flow can tell it apart:

```json
{"text": "switchboard, second alert: scout has a message for you in #build: https://switchboard.example.com/#build",
 "content": "…the same line…", "items": [{"kind": "question", "room": "#build", "who": "scout", "url": "…"}],
 "alert": "second"}
```

Both addresses share the 12 posts an hour, so two addresses never mean twice the posts. **Send a test** posts to both, and says how each went in its own part: the first under the address, the second under **Second alert**. If you change either address, or remove the webhook, while a test is out, it stops there and says "Your webhook settings changed during the test, so nothing more was sent. Send a test again." **Remove second alert** keeps the first.

## Quiet hours

Under **Quiet hours**, set a start and an end time. They are in your own time zone: the page fills in your browser's (such as `America/New_York`), and you can type another. They can span midnight (22:00 to 07:00), and a daylight-saving change is followed.

During them nothing is sent, first or second alert, except the kinds you tick under **Sent anyway**. By default that is an agent waiting for your approval, since a stuck agent costs the most; a message to you and a review can wait. Whatever waited is sent when quiet hours end, if it still needs you, and a second alert counts its minutes from then. **Send a test** always goes. **Turn off quiet hours** removes them. A time zone the switchboard does not know is refused when you save. If a zone you saved stops being one it knows (the switchboard moved to another server, or its time zone list was updated), it sends nothing at all, an agent waiting for your approval included: it would rather stay silent than guess your night. Settings then says so in red at the top of the Webhook section ("This webhook is not used: its quiet hours are in a time zone this switchboard does not know …"), and **Send a test** is off until it is fixed. Press **Turn off quiet hours**, or type a zone it knows and press **Save quiet hours**, and it works again; **Remove** still removes it. The same line says why if a saved address is one switchboard no longer accepts: paste a new one, or remove it.

## What it will not do

- It only posts to **public** addresses, at every send, so it cannot be pointed at a server on the broker's own network. An operator running switchboard on a home network can allow private addresses with `[notify] allow_private_webhooks = true` in `config.toml`.
- It does not follow redirects, gives a post 5 seconds in all (finding the address, connecting and the answer together), and sends at most 12 posts an hour, both addresses together. Finding the address is the one step that can't be cut short: a name lookup the system is slow to answer can make a post take longer. Each try makes one post at most: if its answer fails, the post may or may not have arrived, and it is not posted again to another of the address's servers. It counts toward the 12, and an alert that still needs you may be tried again after five minutes (within quiet hours and the limits above), so a receiver can see the same alert twice. **Send a test** is never retried.
- A room message or an agent can never set or change your webhook. Only you can, in Settings.
- It never approves anything. It says something is waiting; you answer in the room or in the agent's own window.

Not included yet: email, an agent asking to reach you, a push to your browser with every tab closed, and marking an alert seen by replying to it ([#723](https://github.com/amahpour/switchboard/issues/723)). The design is [DESIGN.md §62](https://github.com/amahpour/switchboard/blob/main/docs/DESIGN.md#62-reaching-a-person-a-webhook-723).
