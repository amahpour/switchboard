### Changed

An inline message addressed to an agent arrives whole when its cleaned text is at most 3,000 characters and fits the batch. Chatter keeps the shorter preview, so agents need fewer `read()` calls to see messages meant for them.
