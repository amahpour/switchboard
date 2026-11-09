### Changed

- **The broker's default Kubernetes memory limit is now 1Gi, up from 512Mi.** The Helm chart and standalone manifest keep their 192Mi memory and 100m CPU requests. This gives connected agents more headroom; the meeting bot's separate resources are unchanged. Existing Helm overrides still win. DEPLOY.md now explains sizing, what to monitor and the limits of the measurements; `scripts/bench_broker.py` repeats the HTTP/MCP throughput and open-wait RSS/CPU measurements against temporary brokers.
