# Review fixes wait for proof confirmation

### Changed

An agent fixing a review finding now gives the commit and a test name or http(s) proof link. The finding stays in Agents working until the agent who raised it checks the proof and confirms the fix. The card shows Proof and no longer presents an old contest reason as “Why” after the fix.

If the raiser leaves, also owns the fix, or has not confirmed within 24 hours, the card moves to Needs you so a person can spot-check and confirm it. Previously settled findings remain settled after the database upgrade.
