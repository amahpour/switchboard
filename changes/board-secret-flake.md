### Fixed

- **A secret typed on the whiteboard no longer stays on the author's screen after a pause and Escape.** A person who typed a text with a secret in it, stopped for a moment and then pressed Escape still saw the secret in their own tab, though the broker had cut it out of the saved board: the change Escape made was never sent, so the broker's copy never replaced it. It stayed until the person's next edit. Now that change is sent like any other, and the tab shows what the broker saved (#933).
