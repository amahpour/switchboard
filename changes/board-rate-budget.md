### Fixed

- **Drawing and talking no longer block each other.** Each agent now has its own board budget across rooms: five changes at once, refilling one every two seconds, separate from `say`. A refused edit says how long to wait and is never queued. `draw_mermaid` and `arrange` each count once; board reads and pictures use no editing budget. The board skill describes the new budget in the Claude Code, Codex and Cursor plugins.
