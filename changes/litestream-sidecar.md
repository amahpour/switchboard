### Added

- **Optional Litestream database replication for Kubernetes and Compose.** Helm can add a pinned, non-root S3 sidecar and a separately enabled restore init container. The plain manifests include commented examples. Both options are off by default. Credentials come from an existing Secret or workload identity; attachments still need archive backups, snapshots or a separate sync. The deployment guide covers recovery before broker startup and choosing between replicas and complete archives.
