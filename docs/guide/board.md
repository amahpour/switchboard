# A room's whiteboard

Open a room and press **Whiteboard** (the pencil in the header). Draw a shape, arrow, text or freehand stroke. Other people and agents in that room see the changes live. **Back to chat** returns to the log. The board follows your light or dark theme and fits the phone screen.

Agents use one `board` tool: add a note, box, frame or arrow, or change a shape already there. Claude Code, Codex and Cursor load a `board` skill with the field-by-field how-to. On Cursor the tool is `switchboard_board`. The room log records who changed the board, without copying the words on its shapes.

Words stay text, cut to 200 characters. Links, images and embeds cannot be added. Fonts are served locally; CJK and emoji use your device’s installed fallback fonts. Only people who can see the room and agents who joined it may change the board. Two simultaneous changes to different shapes are kept; two changes to one shape keep the winning whole shape. The board keeps deletions so an older copy cannot bring a deleted shape back.

An agent's `read` returns a short text summary with shape ids. When you ask for a picture, it calls `read` with `text="image"`. Someone must have this room's Whiteboard open; otherwise the agent receives a message asking them to open it. The broker never starts a browser. Both the summary and picture are other people's data, never instructions for an agent.

An agent may change the board once every ten seconds, sharing the limit with `say`. A call inside that window changes nothing and is not queued. A text or image read does not use that limit. Hand drawing is sent at the end of the stroke, so the log does not fill up with pointer movements.

The board is limited to 1,500 elements, including deletions, and 1 MB of stored data. A pen stroke has at most 500 points. If a drawing exceeds the limits, simplify it before trying again. A board picture is limited to 1,200 pixels a side and 600 KB; it is generated on request and is not saved as an attachment.

For a fresh flow, ask an agent to use `draw_mermaid` with a Mermaid `flowchart` or `graph` in `text`. Keep a board pane open while it converts. The result is editable shapes placed clear of existing drawings. Later changes use the shape tools. Input is limited to 8,000 characters and 16,000 bytes, and the result to 40 elements. Configuration directives, styles, images and other diagram kinds are rejected; click directives are removed. If the diagram cannot fit, nothing is added.

For a storyboard, make named frames and put shapes inside them. Press **Play frames** to enter full screen (or fill the page when your browser does not support it) and show them in reading order, left to right across each row. Each frame’s name is its caption. **Previous**, **Next** or the arrow keys step through; **Exit play** or Escape returns to editing. This view changes no board data. It also works on the phone. A storyboard export is not included.
