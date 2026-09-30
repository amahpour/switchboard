# Running switchboard on a server

switchboard normally runs on your own machine: the broker listens on `127.0.0.1` and you open the web UI at `http://switchboard.localhost:7419/`. It can also run as a container on a server, a VM or a platform such as Render, EKS or GKE, behind that platform's HTTPS. This page covers that setup. The design and its threat model are in [DESIGN.md §30](DESIGN.md#30-the-container-image-and-the-public-url-34).

> [!IMPORTANT]
> For now, a hosted broker is **you in the browser**. Your agents run on your own machines, and there's no way yet for them to join a broker elsewhere. The image has no `ssh`, so [remote members](REMOTE.md) don't work from it either. [#41](https://github.com/amahpour/switchboard/issues/41) is next: your machines pair with the broker from its web UI and dial in over `wss://`, and you sign in with a passkey instead of `switchboard login` through exec. Other people and their agents come after that ([#24](https://github.com/amahpour/switchboard/issues/24)).

## How it fits together

```
browser ──https──▶ the platform's proxy (TLS ends here) ──http──▶ container :7419 ──▶ /data volume
                    Render's router, Caddy, an ingress             the broker          SQLite
```

- **One container, one volume, one broker.** The broker takes a lock in its data directory, and a second broker on the same volume refuses to start. Never run more than one replica. Several brokers behind one address are a different design ([#35](https://github.com/amahpour/switchboard/issues/35)).
- **The volume is a real disk:** a Render disk, EBS, a GCE persistent disk, a Docker volume. Never NFS or EFS, where SQLite's locking isn't reliable.
- **The public URL is the only address.** You tell the broker the `https://` address browsers use. It then answers only to that host, accepts writes only from that origin, marks its session cookie `Secure`, and puts that address in the sign-in links. Requests for any other host get `421`. The one exception is `GET /healthz`, which answers `ok` to anyone (platforms probe the container's own address) and nothing else.
- **TLS is the proxy's job.** The broker speaks plain HTTP on port 7419 inside the container. It never reads `X-Forwarded-*` headers: the public URL you configure decides the scheme and the host, so a forged header can't change them.

## Try it on your machine

```bash
docker run -d --name switchboard -p 127.0.0.1:7419:7419 -v switchboard:/data \
  -e SWITCHBOARD_PUBLIC_URL=http://switchboard.localhost:7419 \
  ghcr.io/amahpour/switchboard:0.6.5
docker exec -it switchboard switchboard login
```

Open the link it prints. Plain `http://` is accepted only for a local test host (`localhost`, `127.0.0.1`, `*.localhost` or `*.test`), and `-p 127.0.0.1:…` keeps the port off your network. For anything other machines reach, use `https://` behind a proxy, as below.

## Settings

The image sets everything but the public URL.

| Variable | In the image | What it is |
|---|---|---|
| `SWITCHBOARD_PUBLIC_URL` | (you set it) | The address browsers use: `https://<host>[:<port>]`, an origin only (no path). Required. |
| `SWITCHBOARD_LISTEN` | `0.0.0.0` | The IPv4 address the broker listens on. Anything but `127.0.0.1` needs the public URL. |
| `SWITCHBOARD_PORT` | `7419` | The port it listens on. |
| `SWITCHBOARD_HOME` | `/data/switchboard` | Its data: the database, logs and config.toml, on the `/data` volume. |

They are the environment versions of `switchboard start --public-url`, `--listen` and `--port`. `config.toml` can also set `public_url` and `listen`, and a flag or variable wins over it. The container runs `switchboard start --foreground --log-stdout`: logs go to stdout, where the platform collects them, instead of `logs/broker.log`.

Your name in the room is `me` unless you set it. Add `human_name` to `config.toml` in the home, then restart the container:

```bash
docker exec -u switchboard switchboard sh -c 'echo "human_name = \"ari\"" >> "$SWITCHBOARD_HOME/config.toml"'
docker restart switchboard
```

## Deploy it

Each example pins a release (`ghcr.io/amahpour/switchboard:0.6.5`). Every release moves these pins, and `:latest` follows the newest release.

### A VM with Docker Compose

[deploy/compose/](../deploy/compose/) runs the broker with Caddy in front of it. Caddy gets a certificate from Let's Encrypt for your domain and renews it. You need a VM with Docker, a DNS name pointing at it, and ports 80 and 443 open.

```bash
export SWITCHBOARD_DOMAIN=sb.example.com
docker compose -f deploy/compose/compose.yaml up -d
docker compose -f deploy/compose/compose.yaml exec switchboard switchboard login
```

### Kubernetes (EKS, GKE, AKS)

[deploy/kubernetes/switchboard.yaml](../deploy/kubernetes/switchboard.yaml) is a Service, a one-replica StatefulSet with a 1 GiB volume claim, and an Ingress. Edit `sb.example.com` (the Ingress host, its TLS secret and `SWITCHBOARD_PUBLIC_URL`) first.

```bash
kubectl create namespace switchboard
kubectl -n switchboard apply -f deploy/kubernetes/switchboard.yaml
kubectl -n switchboard exec -it switchboard-0 -- switchboard login
```

What the manifest relies on:

- **The pod runs as uid 10001 from the start**, with a read-only root filesystem, no capabilities and `/tmp` as an `emptyDir`. `fsGroup: 10001` makes the volume writable. `fsGroupChangePolicy: OnRootMismatch` stops Kubernetes from re-chmodding the broker's private home on every start; without it, the broker would refuse its own directory.
- **Replacing the pod is stop-then-start.** A StatefulSet with one replica stops its pod before it starts the new one, as a Deployment's `Recreate` strategy would.
- **The Ingress passes the browser's Host and Origin through**, which ingress-nginx and most controllers do by default. The UI's WebSocket needs no timeout annotation, because the broker pings it every 20 seconds.
- **Use a block-storage class** (the default on EKS, GKE and AKS), not an NFS or EFS one.

### Render

[deploy/render/render.yaml](../deploy/render/render.yaml) is a Blueprint: a web service from the published image, with a 1 GB disk at `/data`. Put it in a repository of yours as `render.yaml` (or point the Blueprint at its path), then choose **New > Blueprint**. Render asks for `SWITCHBOARD_PUBLIC_URL`: the service's `https://<name>.onrender.com` address, or your custom domain.

Render's disks need a paid plan and allow only one instance. A deploy stops the old instance before it starts the new one, and Render snapshots the disk daily and keeps each snapshot at least 7 days. Render's router ends TLS and redirects plain HTTP to HTTPS before it reaches the broker. `PORT=7419` in the Blueprint tells Render which port to send traffic to.

The disk belongs to root, so the container starts as root. Its `switchboard` command makes its home on the disk (`/data/switchboard`) the unprivileged user's, then runs as that user from then on.

## Signing in

A sign-in link comes from `switchboard login` run **inside the container, in a terminal**. It works once, for 5 minutes.

- **Docker:** `docker exec -it switchboard switchboard login`
- **Compose:** `docker compose -f deploy/compose/compose.yaml exec switchboard switchboard login`
- **Kubernetes:** `kubectl -n switchboard exec -it switchboard-0 -- switchboard login`
- **Render:** open the service's **Shell** tab and run `switchboard login`.

Leave out `-t` (as in `docker exec` without `-it`) and the broker refuses: links go only to a terminal someone typed in. Whoever can exec into the container can sign in, so treat access to the platform or cluster as access to switchboard. Signing in without exec (a one-time claim link in the broker's log, then passkeys) is [#41](https://github.com/amahpour/switchboard/issues/41). `switchboard logout --all` in the same way signs out every browser.

## Upgrading

1. Change the image tag in your compose file, manifest or Blueprint to the new release (the [CHANGELOG](../CHANGELOG.md) lists them), and deploy.
2. The platform stops the old container and starts the new one. The broker shuts down cleanly on SIGTERM (`docker stop`) and exits 0.
3. If the new release changes the database's schema, the broker migrates it on start, after a verified backup copy next to it ([DESIGN.md §27.6](DESIGN.md)). An older release refuses a newer database, so a rollback past a schema change means restoring a backup.

Pin releases rather than `:latest`, so an upgrade happens when you choose.

## Backups

- **Disk snapshots:** Render takes them daily. On AWS and GCP, use EBS or persistent-disk snapshots, for example with AWS Backup or a snapshot schedule. A snapshot of a running broker is like a power cut, which SQLite in WAL mode (the broker's setting) is built to recover from.
- **A copy while it runs**, with SQLite's backup API, then out of the container:

  ```bash
  docker exec -u switchboard switchboard sh -c 'umask 077; python3 -c "import sqlite3; \
    sqlite3.connect(\"$SWITCHBOARD_HOME/switchboard.db\").backup(sqlite3.connect(\"/tmp/switchboard-backup.db\"))"'
  docker cp switchboard:/tmp/switchboard-backup.db ./switchboard-$(date +%F).db
  ```

- **Continuous replication:** [Litestream](https://litestream.io) can stream the database to S3-compatible storage, running beside the broker on the same volume.

To restore, stop the broker, put the copy at `$SWITCHBOARD_HOME/switchboard.db` owned by uid 10001 with mode 0600 (and no `-wal` or `-shm` files next to it), and start it again.

## Health checks and logs

- **`GET /healthz`** answers `ok` with status 200. It needs no sign-in, returns no data, and accepts any Host, so platforms can probe the container's own address. The image's Docker `HEALTHCHECK` calls it, and so do the Kubernetes probes and Render's `healthCheckPath`.
- **Logs** are on stdout: `docker logs switchboard`, `kubectl logs switchboard-0`, or Render's Logs tab. They hold ids, never message text.

## The image

- **Where:** `ghcr.io/amahpour/switchboard:<version>` and `:latest`, for linux/amd64 and linux/arm64. The release job builds it from the release's tag, with a provenance attestation and an SBOM. Inspect them with `docker buildx imagetools inspect ghcr.io/amahpour/switchboard:0.6.5 --format '{{json .Provenance}}'` (or `.SBOM`).
- **What's in it:** Python 3.13 (Debian slim) with switchboard installed from its wheel and the exact dependencies in `uv.lock`, plus tini. No uv, compiler, git, ssh or shell tools beyond the base image's.
- **Who it runs as:** the `switchboard` user (uid and gid 10001), always.
  - Started as root, as Render and plain `docker run` do, the image's `switchboard` command ([deploy/image/switchboard.sh](../deploy/image/switchboard.sh)) first makes `$SWITCHBOARD_HOME` that user's private directory. It then drops to that user with no capabilities and no way back through setuid.
  - `docker exec … switchboard …` drops to the same user.
  - Started as uid 10001, as Kubernetes does with the manifest above, it runs as that user from the start.
- **Build it yourself:** `docker build -t switchboard .` from the repository. CI builds it on every pull request and runs [tests/image](../tests/image/test_image.py) against it, behind a proxy that ends TLS (CONTRIBUTING.md, "The container image").
