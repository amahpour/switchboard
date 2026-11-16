### Fixed

- **A call hosted on your machine no longer shows its relay as unhealthy.** The relay runs in switchboard's image, whose health check asks a broker on port 7419. The relay is not a broker, so that check was always down and Docker always reported the container unhealthy. The plugin's Compose file turns the check off. The relay is up while its process is running, and it exits when the call ends.

### Upgrading

- This is in the plugin's Compose file. Update the plugin. The broker does not have to be newer: the image's check is unchanged, and the Compose file turns it off for the relay.
