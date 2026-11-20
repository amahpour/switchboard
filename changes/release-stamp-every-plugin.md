### Fixed

- **The Devin and OpenCode plugins say 1.11.0 again.** v1.11.0 left both plugin manifests at 1.10.0, so those two plugins reported version 1.10.0 to the broker. Nothing else changed: a plugin works with any server of the same major version. The release script now stops unless it runs as main's own copy, which is how the two were missed.
