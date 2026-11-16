### Fixed

- **A meeting bot on your machine can reach a server with a private CA.** Pass `--ca-file FILE` to either plugin's `meeting-host start`, including PowerShell. The relay trusts that PEM CA alongside its system store, and certificate errors explain how to name the file. Certificate and hostname checks stay on. (#758)
