### Fixed

- **`switchboard report` no longer counts review-board moves as catch-up requests.** Every move on a board (open, raise, concede, fix, a person's answer, close) records an event of the kind 0.2.0's `/review` command used, so the "/catchup" row counted each one. It counts only real requests now.
