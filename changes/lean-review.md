### Changed

- **The review tool uses one `where` field for a finding's path and lines.** For example, `where="shop/cart.py:40-58"` replaces separate `file` and `lines` fields, and the shorter tool list takes less room in an agent's context. A running older MCP server can still send the separate fields to the broker for this release.
