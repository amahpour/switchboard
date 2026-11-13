### Upgrading

- **Update the server before the plugin.** The new board inputs (`style`, `w`, `h`) and the `resize` and `text` actions need this server; an older one doesn't know them.

### Added

- **Agents can colour, size and style what they draw.** The `board` tool takes a `style` (Excalidraw's own style-panel words, palette colour names only), `w` and `h`, and new `resize` and `text` actions. A box or note grows taller to fit its label, a frame grows to hold what is added to it, and `connect` can draw elbow arrows that go round other shapes and follow a shape you drag. A frame takes no style, because the board draws every frame the same.
- **Agents hear about layout problems.** Every board change, `draw_mermaid` and `arrange` included, returns the overlaps, overflowing labels, shapes outside their frame and arrows through shapes it caused, and `read` lists them for the whole board, along with each shape's size and colour. The board skill now teaches drawing as a craft and loads for any request to draw, diagram or visualize.
- **A Mermaid diagram keeps its colours.** `style A fill:red,stroke:#c0392b`, `classDef`, `class` and `A:::name` colour the drawn shapes, mapped to the nearest colour in the board's palette. Only `fill` and `stroke` are read; anything else in a style, and `linkStyle`, is refused with the line number.
