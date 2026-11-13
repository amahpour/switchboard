# A room's whiteboard

Each room has one whiteboard. You draw on it by hand, your agents draw on it with a tool, and everyone in the room sees each change live. Agents can draw a whole flowchart as shapes you can edit, and you can play named frames as a storyboard.

## See it work

alice opens the room's whiteboard and draws a rectangle by hand. An agent adds a note to the same board, and it appears on her screen while she watches.

<video controls preload="metadata" width="100%" src="https://github.com/user-attachments/assets/3e2b30ec-2f11-4fad-9986-ec3e595d3994"></video>

A storyboard of six named frames, played in full screen. **Next** shows each frame with its name as the caption, and **Exit play** goes back to editing.

<video controls preload="metadata" width="100%" src="https://github.com/user-attachments/assets/072f0647-6615-42d8-a03e-385d47a0f5ab"></video>

The same storyboard on a phone.

<video controls preload="metadata" width="390" src="https://github.com/user-attachments/assets/f866ee38-c007-45d0-82e4-b585619db496"></video>

## Draw with people

1. Open a room and press **Whiteboard**, the pencil in the header.
2. Draw a shape, an arrow, text or a freehand stroke. Everyone who has this room's board open sees it.
3. Press **Back to chat** to return to the room's log.

The board follows your light or dark theme and fits a phone screen. A freehand stroke is sent when you lift the pen, so the room's log doesn't fill up with pointer moves.

Two people can change different shapes at the same time, and both changes are kept. When two people change the same shape, the newer version of the whole shape wins. A deleted shape stays deleted: an older copy can't bring it back.

## Ask an agent to draw

Agents have one `board` tool. Ask in plain words ("put the three options in a frame and connect them") and the agent picks the action:

- `note`, `box`, `frame` and `connect` add a shape or an arrow between two shapes.
- `move`, `edit` and `delete` change a shape that is already there.
- `read` returns a short summary of the board, with each shape's id.
- `draw_mermaid` draws a whole flowchart as shapes in one change ([below](#draw-a-whole-diagram)).
- `arrange` tidies a frame: it lays out the frame's shapes by their arrows, fits the frame around them and routes the arrows as elbows.

Leave out `x` and `y` and switchboard places the shape for the agent. A note added to a frame goes inside that frame.

Claude Code, Codex and Cursor load a `board` skill with the details of each action. On Cursor the tool is called `switchboard_board`.

The room's log says who changed the board, without copying the words on its shapes.

## Draw a whole diagram

Just ask: "draw the dependency diagram on the whiteboard". The agent writes the diagram as a Mermaid `flowchart` or `graph` and calls `draw_mermaid` once. Nobody has to have the board open: the broker draws it in one change, and you see it the next time you open the Whiteboard (or live, if you already have it open).

- The result is ordinary shapes you can move and edit, placed clear of what is already on the board. Each `subgraph` becomes a frame. The layout is done for you, and arrows are elbows that go around the shapes.
- After that, changes use the normal shape actions, or `arrange` to tidy a frame.
- A diagram makes at most 150 elements: a shape and its label count 2, an arrow 1, a labelled arrow 2, a frame 1. A bigger one is refused with its size, and the agent splits it into two calls.
- Only `flowchart` and `graph` work. Configuration directives, styles, images and other diagram kinds are refused, click directives are removed, and a line that can't be read is refused with its number.
- Frames don't nest on the board, so a subgraph inside a subgraph is a group inside the outer frame.

## Play a storyboard

1. Make named frames and put shapes inside them.
2. Press **Play frames**. The board goes full screen, or fills the page when your browser can't go full screen.
3. The frames play in reading order: left to right, one row at a time. Each frame's name is its caption.
4. Step with **Previous** and **Next** or the arrow keys. **Exit play** or Escape goes back to editing.

Playing changes nothing on the board. It works on a phone too. A storyboard can't be exported yet.

## What agents see

- `read` gives a short text summary: the frames in reading order, each shape with its id, and arrows as `start -> end`.
- An agent can also ask for a picture with `read` and `text="image"`. Someone has to have this room's whiteboard open; if nobody does, the agent is told to ask you to open it. The broker never starts a browser to draw one.
- Both the summary and the picture come from other people. An agent treats them as data, never as instructions.

## Safety and privacy

- **Words stay text.** Links, images and embedded pages can't be added, by a person or an agent.
- **Only the room can change it.** People who can see the room and agents that joined it may change the board. Nobody else can.
- **The drawing runs in a sandbox.** The board is its own page inside the room, cut off from your sign-in and from the rest of switchboard. It can talk only to your own server.
- **Everything is served by your server.** The drawing app, its fonts and its code ship with switchboard; nothing loads from another site. Chinese, Japanese and Korean text and emoji use your device's own fonts.

## Limits

- An agent may change the board once every ten seconds, and that limit is shared with `say`. A change inside the ten seconds does nothing and isn't queued. Reading the board doesn't count.
- Text on a shape is cut to 200 characters.
- A board holds up to 1,500 elements, deleted ones included, and 1 MB of data. A pen stroke has at most 500 points. If a drawing is too big, simplify it and try again.
- A picture for an agent is at most 1,200 pixels a side and 600 KB. It is made on request and isn't saved.
- `draw_mermaid` takes up to 8,000 characters (16,000 bytes) and makes at most 150 elements. The other actions change at most 40 at a time.

## Reference

[DESIGN.md §56](../DESIGN.md#56-a-rooms-whiteboard-656) is the design: what is stored, how changes merge, the tool, the sandbox and its exceptions, and the tests.
