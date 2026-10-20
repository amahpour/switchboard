### Fixed

- **A page with more than 64 rooms open now stays connected.** The page said hello to the broker once, naming every room, and the broker refuses a hello that names more than 64 and closes the socket, so with 65 rooms the page showed "Disconnected: reconnecting" for as long as it had them. It now says hello in batches of 64.
