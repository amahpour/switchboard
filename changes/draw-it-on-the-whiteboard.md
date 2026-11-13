### Changed

- **"Draw it on the whiteboard" now just works.** An agent that is asked for a diagram draws the whole thing in one call, with nobody looking at the board, and you see it the next time you open the Whiteboard. Before, `draw_mermaid` needed a Whiteboard open in a browser to convert the diagram, so agents pasted Mermaid in chat or asked you to open the board. The broker now converts the diagram itself and lays it out for you: subgraphs become frames, arrows are elbows that go around the shapes, and no shape overlaps another.
- **A diagram may make 150 elements, up from 40.** The shape actions still change at most 40 at a time.
- **The `board` tool's description and the board skill say what the board is for,** and tell agents never to ask someone to open the board in order to draw.

### Added

- **Elbow arrows follow a shape you drag.** The diagrams agents draw use elbow arrows bound to their shapes, and the board now stores where each end sits on its shape (one more arrow field), so dragging a shape in the pane pulls its arrows along.
- **`arrange` tidies a frame.** It lays out the frame's shapes in layers by their arrows, fits the frame around them and routes the arrows as elbows, in one change.

### Upgrading

- Update the server first, then the plugin: the new board skill names the `arrange` action.
