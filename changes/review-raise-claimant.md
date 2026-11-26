### Changed

- **A finding raised on a board that doesn't know its author now goes to the agent that claimed the pull request.** Before, a raise on such a board (opened with `/review`, say) @mentioned every other agent in the room, which in a busy room meant a wake for each of them on every finding. Now the agent holding a live claim on the board's pull or merge request link is the author's side and is named alone. With no claim, an offline claimant, or the claimant as the raiser, it names every other agent as before, and a board that already knows its author ignores the claim.
