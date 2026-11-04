### Fixed

- **The Kubernetes example starts on a stock cluster.** Its Service is named `switchboard`, so Kubernetes put `SWITCHBOARD_PORT=tcp://<cluster-ip>:7419` in the pod, over the image's own port, and the broker exited on it and restarted in a loop. The example now sets `enableServiceLinks: false`. If your own manifest names its Service `switchboard`, add that line too. The broker's error now says where such a value came from and what to set.
