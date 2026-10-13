### Fixed

- **Parallel browser tests use less memory.** Added stand-in agents close after each test, and parallel e2e tests share one seeded world per module instead of starting it on every worker.
