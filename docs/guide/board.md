# A room's whiteboard

Each room has one whiteboard. You draw on it by hand, your agents draw on it with a tool, and everyone in the room sees each change live. Agents can turn a Mermaid flowchart into shapes you can edit, and you can play named frames as a storyboard.

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

- `note`, `box`, `frame` and `connect` add a shape or an arrow between two shapes; `text` adds a heading or a caption with no box.
- `move`, `resize`, `edit` and `delete` change a shape that is already there.
- `read` returns a short summary of the board, with each shape's id, its size, its colours when they aren't the default, and any layout problems.
- `draw_mermaid` turns a Mermaid flowchart into shapes ([below](#turn-a-flow-into-shapes)).

Leave out `x` and `y` and switchboard places the shape for the agent. A note added to a frame goes inside that frame, and the frame grows to hold it. A box or note grows taller to fit its words.

## Colours and styles

An agent can set a shape's look with the words of Excalidraw's own style panel, so you see exactly what it set: `stroke` and `background` (red, pink, grape, violet, blue, cyan, teal, green, yellow, orange, gray, black, white, transparent), `fill`, `stroke-width`, `stroke-style`, `sloppiness`, `edges`, `opacity`, `font-size`, `align`, and for an arrow `arrow-type` (sharp, curved, elbow) and its two arrowheads. The colours are the picker's own, so you can recolour by hand and the agent reads your choice back by name. There are no raw colour codes. A frame takes no style: the board draws every frame the same. An elbow arrow doesn't follow a shape you drag afterwards.

## Drawings you can read

After each change an agent is told about layout problems it caused: shapes that overlap, a label that may spill out of its shape, a shape outside its frame, an arrow passing through a shape that isn't one of its ends, and arrow labels on top of each other. The board skill asks the agent to fix them before it says it is done, and to keep one idea to a frame, so a big picture becomes a storyboard.

Claude Code, Codex and Cursor load a `board` skill with the details of each action. On Cursor the tool is called `switchboard_board`.

The room's log says who changed the board, without copying the words on its shapes.

## Turn a flow into shapes

Ask an agent to use `draw_mermaid` with a Mermaid `flowchart` or `graph`. Keep a board open while it converts: the conversion runs in that page.

- The result is ordinary shapes you can move and edit, placed clear of what is already on the board.
- After that, changes use the normal shape actions.
- Only `flowchart` and `graph` work. Configuration directives, styles, images and other diagram kinds are refused, and click directives are removed.
- A flow that can't fit adds nothing.

## Play a storyboard

1. Make named frames and put shapes inside them.
2. Press **Play frames**. The board goes full screen, or fills the page when your browser can't go full screen.
3. The frames play in reading order: left to right, one row at a time. Each frame's name is its caption.
4. Step with **Previous** and **Next** or the arrow keys. **Exit play** or Escape goes back to editing.

Playing changes nothing on the board. It works on a phone too. A storyboard can't be exported yet.

## What agents see

- `read` gives a short text summary: the frames in reading order, each shape with its id, size and non-default colours, arrows as `start -> end`, and then the layout problems it found.
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
- `draw_mermaid` takes up to 8,000 characters (16,000 bytes) and makes at most 40 elements.

## Reference

[DESIGN.md §56](../DESIGN.md#56-a-rooms-whiteboard-656) is the design: what is stored, how changes merge, the tool, the sandbox and its exceptions, and the tests.
