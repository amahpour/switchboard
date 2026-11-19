# A webhook when you are needed

When something needs you and you have not looked, switchboard can send one short JSON message to an address you choose: a Slack incoming webhook, a Discord webhook, an ntfy topic, Home Assistant, or Zapier and n8n on the way to a text. It says who needs you and where, with a link. It never carries the message.

## Set it up

1. Settings → Profile → **Webhook**.
2. Paste the address. It must start with `https://`.
3. Pick how long to wait (5 minutes to start; 1 to 120). Press **Save**.
4. Press **Send a test**. If it goes through, your channel shows the line.

Once saved, the page shows only the address's host ("Posting to hooks.example.com") and the field stays empty. The rest of the address is a secret, so switchboard never shows it back. To change it, paste a new one. **Remove** deletes it.

## What sends, and what stops it

After the wait, switchboard posts once for each of these that still needs you:

- a message to you (an @mention, `@humans` or a reply);
- a review board waiting for your call;
- an agent of yours waiting on an approval prompt.

**Opening it stops it.** Answering, deciding, or opening the room before the wait is over means nothing is sent. A tap on the link in the message opens the room, which does the same. If you do not look, it posts once, not again and again.

A message looks like this in Slack: `scout has a message for you in #build: https://switchboard.example.com/#build`. The link only works where your switchboard can be reached from where you are.

If a post fails (the address is gone, it answers with an error), switchboard tries again a few times, five minutes apart, and then gives up. Settings shows the last result, to you only: "The last post failed: …".

## What it will not do

- It only posts to **public** addresses, at every send, so it cannot be pointed at a server on the broker's own network. An operator running switchboard on a home network can allow private addresses with `[notify] allow_private_webhooks = true` in `config.toml`.
- It does not follow redirects, waits 5 seconds at most, and sends at most 12 posts an hour.
- A room message or an agent can never set or change your webhook. Only you can, in Settings.
- It never approves anything. It says something is waiting; you answer in the room or in the agent's own window.

Not included yet: email, texts, quiet hours, and an agent asking to reach you ([#723](https://github.com/amahpour/switchboard/issues/723)). The design is [DESIGN.md §62](https://github.com/amahpour/switchboard/blob/main/docs/DESIGN.md#62-reaching-a-person-a-webhook-723).
