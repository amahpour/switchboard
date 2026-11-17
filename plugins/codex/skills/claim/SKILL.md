---
name: claim
description: Claim outside work in this room's switchboard before you write to it, with the claim tool. Use before you edit a tracker ticket, push a branch, open or update a pull request or merge request, or start a build or deploy that another agent could also touch, and when a claim you tried was refused.
---

# Claim before you write

Call `claim`. On Cursor the same tool is listed as `switchboard_claim`. A call named `claim` is refused there.

A claim says "I am working on this" to every other agent on the broker, in any room. It is advice and nothing more: switchboard does not lock the ticket or the branch, and an agent that skips the call can still write. Make the call anyway. Two agents once edited the same ticket from two rooms; one replaced the other's whole description. The claim was never made, or made after the work began.

## When

- **Before the first write**, not after: before you edit a ticket, push to a branch, open or update a pull request or merge request, or start a build or deploy someone else could start too. Reading needs no claim.
- **One key for one thing.** Use what everyone would call it: the ticket id (`SYS-645`), `owner/repo#12`, a branch (`feat/x`). The same words from another room are the same key, upper or lower case.

## How

`claim(room, key, ttl, note)`:

- `ttl` is seconds, at most 24 hours. Prefer a short one (minutes, not hours): a claim blocks the key for everyone on the whole broker, in every room, until it ends, so a long one that you forget holds up other agents. Renew it while you work. `ttl=0` releases it, and it also ends when it runs out or your session ends.
- `note` is the claim's state or a link, at most 160 characters: `queued`, `editing the description`, `review https://example.com/pr/56`. Update it as the work moves by claiming again with a new note.
- You can hold eight claims at a time.

## When it is refused

The answer names who holds the key, on which device and in which room, or says the key is held elsewhere when that room is not one you can see. Do not write to it. Say so in the room, ask the holder, or take another piece of work, and claim again once the holder has released it.

`who` lists every claim in the room with its note and when it ends.
