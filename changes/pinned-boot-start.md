### Fixed

- **Keep a running dialer and live local sessions visible after a Linux clock step.** On WSL2 or another machine where `/proc/stat`'s boot time moves, `status`, `stop`, `start`, `remote remove` and `remote join` now check the same process start time the dialer recorded. A broker restart no longer ends live local sessions for that reason, and the broker's stop fallback still recognizes its pidfile.
