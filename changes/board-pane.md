### Added

- **Draw together on a room's Whiteboard.** People draw by hand and see agent shapes arrive live, in light, dark and on the phone. The drawing stays in a sandboxed page served by switchboard, with local fonts and no CDN, links, images or embeds. Agents can request a picture from an open board with `read` and `text="image"`; no browser starts on the server.

### Fixed

- Bound arrows to existing shapes now end at their targets' actual edges without changing the target sizes.

- Bound labels are measured and wrapped in the browser, so the broker’s rough one-line text box does not clip their words.
