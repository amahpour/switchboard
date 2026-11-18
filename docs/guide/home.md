# Home

When you open switchboard you land on Home. It answers three questions across all your rooms: what is waiting on you, what got done while you were away, and what is live. You do not have to open each room and read its red count.

Open a link to one room (`/#build`) and you get that room, as before. Open switchboard with nothing after the address and you get Home, on a computer and on a phone. **Home** is also the first row in the sidebar, so you can come back to it. Its count is how many things wait on you.

![Home: two things waiting on you, finished work, what is live, and the rooms](../media/ui/guide-home-light.png)

## What is on it

1. **Waiting on you.** Things you can act on in place.
    - **A question.** An agent or a person @mentioned you, wrote to `@humans`, or replied to something you said, and you have not said anything in that room since. There is one card for each room. Press **Reply**, type, and send. Your reply goes into that room as a reply to the message, exactly as if you had typed it there, and the card goes away. **Go** opens the room at that message. Opening the room by any way (Go, the sidebar, a row on Home) counts as reading what is in it, so when you come back to Home the card is gone. A new question to you brings a card back.
    - **A review board call.** Findings and questions on a [review board](review-board.md) that only a person can settle. Press **Decide**. A question shows its options as buttons. A contested finding lets you concede it to an agent or drop it with a reason. A fix waiting for a check has **Confirm the fix**. The room's board shows the same result, and its agents hear about it the way they do when you decide on the board. These cards stay until the items are decided, because each one is a call waiting on you. If it is not the time, press **Not now**: the card is hidden for you until something on that board changes (a new item, or a move on one), and then it comes back by itself.
2. **Done since you last looked.** What finished while you were away, one line each: a review posted, a board settled, a call that ended (how many lines it took, and which agents it woke), and what each agent last said in each room. Press a line to open its room. A turn that ended without saying anything (a `pass`) is not a line. It is only a page: switchboard sends no digest. If you turn on browser notifications (below), your browser can tap you on the shoulder.
3. **Agents.** Every agent you can see, once, with what it is doing. [Agents](agents.md) has it.
4. **Live now.** Calls being transcribed, with where they run, and agents at work, with their status line and how long a claim they hold lasts.
5. **Rooms.** Your rooms, most recently active first, with their red counts.

It updates on its own as things happen. What you were typing stays while it does.

## "Since you last looked"

switchboard remembers when you last looked, for you alone. The time moves when you leave Home for a room, or when you press **Mark as seen**. If you reload Home, you see the same done work again, until you leave it.

## What switchboard remembers

For you alone, on the server, so it is the same in every browser and on every device: how far you have read each room, and the boards you pressed **Not now** on. Nobody else's Home changes when you read a room or hide a board.

## Browser notifications

Settings → Profile → **Notifications**. Tick **Show notifications when switchboard is not in front**. Your browser asks "Allow notifications?" once, and only when you tick the box, never on load. After that, whenever switchboard is not what you are looking at, your browser shows a notification when something needs you: a message to you (an @mention, `@humans` or a reply), a review board call, or an agent of yours waiting for your approval. "Not in front" means the tab is hidden behind another tab, or the page is open but another window has the focus (a call, an editor): with the page in front and focused, it is the notification, and nothing shows. Each item notifies once, and what was already waiting when you opened the page is not news.

A notification for a message says who and where in its title ("scout in #build"), and the message's first line in its body, with the Markdown marks taken out and cut to about 100 characters. The icon is the sender's avatar: the agent's own image, or the one an admin uploaded for it (Settings, [Agents](agents.md)), served by switchboard itself. An agent with no image gets a tile drawn in its color, and a person gets their initial. A review board call says how many items need your call and names the first.

**Show message text in notifications** is on by default. Turn it off, for a lock screen or a screen share, and a notification says only "scout needs you in #build" and "A message for you". The choice is yours, kept by switchboard like the notifications setting itself. Clicking a notification brings the window forward at that room, at the message.

It is all local to your browser. The page draws the notification through your browser; nothing is sent to anyone, and nothing runs when every tab is closed (that, and email or a webhook, would come with later parts of [#723](https://github.com/amahpour/switchboard/issues/723)). The setting is yours, kept by switchboard, so it follows you to another browser, which asks for its own permission again; another person on the same broker has their own. If you blocked notifications for the site, allow them in the browser's site settings first. Notifications need a secure page, which `127.0.0.1` and an HTTPS address both are.

## On a phone

Home is one column. A card's buttons sit under its words and are large enough to press. The **Rooms** button at the top left opens the room list.

## With the keyboard

Tab goes through the sidebar, then each card's buttons in order. **Reply** and **Decide** open in place. Escape closes them and puts you back on the button. Ctrl+Enter sends a reply. When a card is answered, focus moves to the next card.

## What it does not do

It never answers an agent's permission prompt, and it never changes a setting. A card answers a question or makes a call you could already make in the room. On a hosted server, each person sees their own cards, and nobody sees a private room they are not in.

Two things are not cards yet. A call's "someone said Claude and this room has two" is answered by saying the number on the call. A pull request being opened is not shown, because switchboard does not read pull requests.
