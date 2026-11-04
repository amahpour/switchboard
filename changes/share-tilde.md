### Fixed

- **An agent can share a file under its home folder with a `~/` path.** The one-time upload command quoted the path whole, so for `artifact: build.log | ~/work/build.log` the shell never expanded the `~` and `curl` found no such file. A leading `~/` now becomes `"$HOME"/`, and the rest of the path stays quoted, so nothing else in it is expanded.
