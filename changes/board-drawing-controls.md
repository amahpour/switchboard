### Upgrading

- **Update the server before the plugin.** The new board inputs (`style`, `w`, `h`) and the `resize` and `text` actions need this server; an older one doesn't know them.

### Added

- **Agents can colour, size and style what they draw.** The `board` tool takes a `style` (Excalidraw's own style-panel words, palette colour names only), `w` and `h`, and new `resize` and `text` actions. A box or note grows taller to fit its label, a frame grows to hold what is added to it, and `connect` can draw elbow arrows. A frame takes no style, because the board draws every frame the same.
- **Agents hear about layout problems.** Every board change returns the overlaps, overflowing labels, shapes outside their frame and arrows through shapes it caused, and `read` lists them for the whole board, along with each shape's size and colour. The board skill now teaches drawing as a craft and loads for any request to draw, diagram or visualize.

### Known limitations

- **An elbow arrow doesn't follow a shape you drag in the board.** It is drawn routed, and the server keeps what it connects, but the board page doesn't keep the attachment.
