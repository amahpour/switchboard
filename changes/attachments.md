### Upgrading

- **On Kubernetes, raise the volume and allow a larger request body, or uploads over 1 MB fail.** The StatefulSet's claim is 20Gi and the Ingress sets `nginx.ingress.kubernetes.io/proxy-body-size: "26m"`. A claim created at 1Gi does not grow because the manifest changed: raise it, and add the annotation, before people attach files. ingress-nginx's own default cuts a body at 1 MB.
- **On Render, the disk in the Blueprint is 20 GB.** A disk already created stays the size you gave it. Files a room holds sit on that disk with the database, and they are in the daily snapshot. Give the disk room for them, and for the backup.
- **Caddy has no default body limit.** A proxy that does, needs a limit at least `[files] max_mb` (25 unless you set it).

### Added

- **A room can hold a file.** Attach one from the button beside the composer, or drop it on the room. An agent that posts an artifact line with a local path gets a one-time `curl` line, valid 10 minutes. A person who can read the room downloads it from the card. An agent that is shown the message gets a short-lived download link in that result, not in the message. Files are kept 30 days unless an admin sets another number, or Never, in Settings → Files. Deleting the room deletes them. One file is at most 25 MB, and a room's files at most 500 MB, both in `config.toml` under `[files]`.
