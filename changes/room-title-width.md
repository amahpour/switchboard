### Fixed

- **The room's name stays whole in the header.** A desktop window with Members open could show "s…" where the name should be, while the member count and every status chip kept their full width. Now the chips give way first (they scroll), then the member count, and the name last, at every text size; the larger text sizes no longer cap it at 140 px.
