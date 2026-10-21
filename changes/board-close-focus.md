### Fixed

- **A keyboard user's place on the review board's Close board button is kept when the board redraws.** Every agent status change and every card move redraws the board, and a person who had tabbed to **Close board** was dropped to the top of the page, so the next Tab started over. The button now keeps its focus through a redraw, as the cards, the divider and Hand off already did.
