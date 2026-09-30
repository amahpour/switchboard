# Running switchboard on a server

switchboard normally runs on your own machine: the broker listens on `127.0.0.1` and you open the web UI at `http://switchboard.localhost:7419/`. It can also run as a container on a server, a VM or a platform such as Render, EKS or GKE, behind that platform's HTTPS. This page covers that setup. The design and its threat model are in [DESIGN.md §30](DESIGN.md#30-the-container-image-and-the-public-url-34).

> [!IMPORTANT]
> A hosted broker is **your team's**: you set it up with a one-time password from its log, add people from its admin section, and everyone signs in with a password or a passkey ([Signing in](#signing-in)). Everyone's machines dial in to it over `wss://`, so their agents join its rooms ([Machines that dial in](REMOTE.md#machines-that-dial-in-a-hosted-broker)), and every agent takes every person's messages as its user's. The image has no `ssh`, so [remote members over SSH](REMOTE.md#over-ssh) don't work from it.

## How it fits together

```
browser ──https──▶ the platform's proxy (TLS ends here) ──http──▶ container :7419 ──▶ /data volume
your machines ─wss─▶ Render's router, Caddy, an ingress             the broker          SQLite
```

- **One container, one volume, one broker.** The broker takes a lock in its data directory, and a second broker on the same volume refuses to start. Never run more than one replica. Several brokers behind one address are a different design ([#35](https://github.com/amahpour/switchboard/issues/35)).
- **The volume is a real disk:** a Render disk, EBS, a GCE persistent disk, a Docker volume. Never NFS or EFS, where SQLite's locking isn't reliable.
- **The public URL is the only address.** You tell the broker the `https://` address browsers use. It then answers only to that host, accepts writes only from that origin, marks its session cookie `Secure`, and puts that address in the sign-in links. Requests for any other host get `421`. The one exception is `GET /healthz`, which answers `ok` to anyone (platforms probe the container's own address) and nothing else.
- **TLS is the proxy's job.** The broker speaks plain HTTP on port 7419 inside the container. It never reads `X-Forwarded-*` headers: the public URL you configure decides the scheme and the host, so a forged header can't change them.
- **WebSockets pass through the proxy:** the web UI's (`/ws`) and your machines' links (`/link`). Every proxy above does that by default. Don't put request buffering on them: a machine's link is a long-lived WebSocket that the broker pings every 2 seconds.

## Try it on your machine

```bash
docker run -d --name switchboard -p 127.0.0.1:7419:7419 -v switchboard:/data \
  -e SWITCHBOARD_PUBLIC_URL=http://switchboard.localhost:7419 \
  ghcr.io/amahpour/switchboard:0.9.0
docker logs switchboard
```

Sign in as `admin` with the one-time password the log prints, then choose your own password or a passkey (browsers treat `*.localhost` as a secure context, so passkeys work here over plain http). Plain `http://` is accepted only for a local test host (`localhost`, `127.0.0.1`, `*.localhost` or `*.test`), and `-p 127.0.0.1:…` keeps the port off your network. For anything other machines reach, use `https://` behind a proxy, as below.

## Settings

The image sets everything but the public URL.

| Variable | In the image | What it is |
|---|---|---|
| `SWITCHBOARD_PUBLIC_URL` | (you set it) | The address browsers use: `https://<host>[:<port>]`, an origin only (no path). Required. |
| `SWITCHBOARD_LISTEN` | `0.0.0.0` | The IPv4 address the broker listens on. Anything but `127.0.0.1` needs the public URL. |
| `SWITCHBOARD_PORT` | `7419` | The port it listens on. |
| `SWITCHBOARD_HOME` | `/data/switchboard` | Its data: the database, logs and config.toml, on the `/data` volume. |
| `SWITCHBOARD_HUMAN_NAME` | (unset: `me`) | The admin's name in the rooms. The admin signs in as it, or as `admin`. |
| `SWITCHBOARD_RESET_OWNER` | (unset) | Recovery: set it to a new value and restart to forget the admin, everyone else, and every password, passkey and session ([Signing in](#signing-in)). It acts once per value. |

They are the environment versions of `switchboard start --public-url`, `--listen` and `--port`. `config.toml` can also set `public_url` and `listen`, and a flag or variable wins over it. The container runs `switchboard start --foreground --log-stdout`: logs go to stdout, where the platform collects them, instead of `logs/broker.log`.

The admin's name in the rooms is `me` unless you set it: `SWITCHBOARD_HUMAN_NAME` in the deployment (the Kubernetes and Compose files have it), or `human_name` in `config.toml` in the home, then restart the container:

```bash
docker exec -u switchboard switchboard sh -c 'echo "human_name = \"ari\"" >> "$SWITCHBOARD_HOME/config.toml"'
docker restart switchboard
```

## Deploy it

Each example pins a release (`ghcr.io/amahpour/switchboard:0.9.0`). Every release moves these pins, and `:latest` follows the newest release.

### A VM with Docker Compose

[deploy/compose/](../deploy/compose/) runs the broker with Caddy in front of it. Caddy gets a certificate from Let's Encrypt for your domain and renews it. You need a VM with Docker, a DNS name pointing at it, and ports 80 and 443 open.

```bash
export SWITCHBOARD_DOMAIN=sb.example.com
docker compose -f deploy/compose/compose.yaml up -d
docker compose -f deploy/compose/compose.yaml logs switchboard    # the admin's one-time password
```

### Kubernetes (EKS, GKE, AKS)

[deploy/kubernetes/switchboard.yaml](../deploy/kubernetes/switchboard.yaml) is a Service, a one-replica StatefulSet with a 1 GiB volume claim, and an Ingress. Edit `sb.example.com` (the Ingress host, its TLS secret and `SWITCHBOARD_PUBLIC_URL`) first.

```bash
kubectl create namespace switchboard
kubectl -n switchboard apply -f deploy/kubernetes/switchboard.yaml
kubectl -n switchboard logs switchboard-0    # the admin's one-time password
```

What the manifest relies on:

- **The pod runs as uid 10001 from the start**, with a read-only root filesystem, no capabilities and `/tmp` as an `emptyDir`. `fsGroup: 10001` makes the volume writable. `fsGroupChangePolicy: OnRootMismatch` stops Kubernetes from re-chmodding the broker's private home on every start; without it, the broker would refuse its own directory.
- **Replacing the pod is stop-then-start.** A StatefulSet with one replica stops its pod before it starts the new one, as a Deployment's `Recreate` strategy would.
- **The Ingress passes the browser's Host and Origin through**, which ingress-nginx and most controllers do by default. The WebSockets need no timeout annotation, because the broker pings the UI's every 20 seconds and a machine's link every 2.
- **Use a block-storage class** (the default on EKS, GKE and AKS), not an NFS or EFS one.

### Render

[deploy/render/render.yaml](../deploy/render/render.yaml) is a Blueprint: a web service from the published image, with a 1 GB disk at `/data`. Put it in a repository of yours as `render.yaml` (or point the Blueprint at its path), then choose **New > Blueprint**. Render asks for `SWITCHBOARD_PUBLIC_URL`: the service's `https://<name>.onrender.com` address, or your custom domain. The admin's one-time password is in the service's **Logs** tab.

Render's disks need a paid plan and allow only one instance. A deploy stops the old instance before it starts the new one, and Render snapshots the disk daily and keeps each snapshot at least 7 days. Render's router ends TLS and redirects plain HTTP to HTTPS before it reaches the broker. `PORT=7419` in the Blueprint tells Render which port to send traffic to.

The disk belongs to root, so the container starts as root. Its `switchboard` command makes its home on the disk (`/data/switchboard`) the unprivileged user's, then runs as that user from then on.

## Signing in

The sign-in page offers three ways in: your **name and password**, a **passkey** (Touch ID, Face ID, Windows Hello, your phone or a security key), and **SSO**, which is shown as coming soon.

A fresh broker has no admin. Until it does, it prints one line in its log:

```
switchboard isn't set up yet. Sign in at https://sb.example.com as admin with the one-time password 7KQ4-M2XD-9HVA-3JNP (it works once, for 60 min), then choose your own password or passkey. Or open https://sb.example.com/setup#t=…
```

1. **Set it up.** Take the newest such line from wherever you read the container's logs (your deploy tool's logs view, `kubectl logs`, `docker logs`, Render's Logs tab), sign in as `admin` with that one-time password, and choose your own password, or a passkey instead. That makes you the admin and signs that browser in. The link in the same line does the same without typing.
2. **Add people** from **Admin > People** in the sidebar. Type someone's name (the one they'll have in the rooms) and send them the invite it shows: the address, their name and a one-time password, good for 7 days. They sign in with it and choose their own password or a passkey. You see the one-time password once; **New one-time password** makes another (and signs them out everywhere, for a forgotten password), and **Remove** signs them out everywhere at once and ends their password and passkeys. Their messages stay.
3. **From then on**, everyone signs in with their name and password, or a passkey. The key button next to sign-off in the app opens the Sign-in sheet: change your password, add a passkey (both ask you to confirm it's you first, unless you did in the last five minutes), or **Sign out everywhere**, which signs out every browser you're signed in to and nobody else.
4. **Once set up, no one-time password is ever printed again.** Upgrades and restarts keep the admin and everyone else: people, passwords and passkeys live in the database on the volume, with your rooms. Only a brand-new, empty volume isn't set up.

Everyone signed in is equal apart from the admin section: they post under their own name, run every room command, pair and approve their machines, and **every agent takes every person's messages as its user's** (`kind=human`). That fits a team on a private network; don't hand out invites to people you wouldn't let steer your agents.

What to know about the admin's one-time password:

- **It works once and expires.** A fresh line is printed every hour until someone uses it, and after a restart; use the newest. In the link it is after `#`, which browsers never send to a server, so it can't land in a proxy's access log.
- **Anyone who can read the logs during that window could set it up first**: a read-only role in your deploy tool, or a log service the logs ship to. You would know at once, because the newest one would say it was already used, and you would get an empty broker back with the reset below. Bots can't read your logs.
- **Wrong passwords slow down.** After 5 wrong ones for a name in 15 minutes, that name waits 30 s before the next try, doubling up to 15 minutes; a flood of wrong ones pauses every password sign-in for 10 s.
- **Passkeys need a name and a secure context.** They are on behind an `https://` public URL whose host is a DNS name (or `http://` on `*.localhost`, for a try on your machine). Behind `http://…test` or a public URL that is an IP address the broker says so in its log, and everyone uses passwords.

**If you lose your password and every passkey**, set `SWITCHBOARD_RESET_OWNER` to a new value (today's date, say) in the deployment and restart. The broker forgets the admin and everyone else, deletes every password, passkey and session, and prints a fresh one-time password. It acts **once per value** and ignores that value from then on, so leaving the variable set is harmless across restarts and pod moves; to reset again, change it. This is the one step that needs ops access, and it can't be undone.

**The shell still signs in, as the admin.** `switchboard login` run **inside the container, in a terminal**, prints a one-time link as it always did, before or after setup:

- **Docker:** `docker exec -it switchboard switchboard login`
- **Compose:** `docker compose -f deploy/compose/compose.yaml exec switchboard switchboard login`
- **Kubernetes:** `kubectl -n switchboard exec -it switchboard-0 -- switchboard login`
- **Render:** open the service's **Shell** tab and run `switchboard login`.

Leave out `-t` (as in `docker exec` without `-it`) and the broker refuses: links go only to a terminal someone typed in. Whoever can exec into the container can sign in and could read its database anyway, so treat access to the platform or cluster as access to switchboard. A session from the shell can't add a passkey or change the password on its own: only your password or passkeys can confirm that. `switchboard logout --all` in the same way signs out every browser, everyone's.

## Upgrading

1. Change the image tag in your compose file, manifest or Blueprint to the new release (the [CHANGELOG](../CHANGELOG.md) lists them), and deploy.
2. The platform stops the old container and starts the new one. The broker shuts down cleanly on SIGTERM (`docker stop`) and exits 0.
3. If the new release changes the database's schema, the broker migrates it on start, after a verified backup copy next to it (`switchboard.db.v2.bak` for the passkeys release, `switchboard.db.v3.bak` for the people release, [DESIGN.md §27.6](DESIGN.md), [§31.2](DESIGN.md), [§32.2](DESIGN.md)). An older release refuses a newer database, so a rollback past a schema change means restoring a backup. The admin, people, passwords and passkeys are in the database, so an upgrade keeps them.

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
- **Logs** are on stdout: `docker logs switchboard`, `kubectl logs switchboard-0`, or Render's Logs tab. They hold ids, never message text. The admin's one-time password is the one secret ever printed there, and it works once and expires. The people's one-time passwords never appear there. Pairing codes never appear there.

## The image

- **Where:** `ghcr.io/amahpour/switchboard:<version>` and `:latest`, for linux/amd64 and linux/arm64. The release job builds it from the release's tag, with a provenance attestation and an SBOM. Inspect them with `docker buildx imagetools inspect ghcr.io/amahpour/switchboard:0.9.0 --format '{{json .Provenance}}'` (or `.SBOM`).
- **What's in it:** Python 3.13 (Debian slim) with switchboard installed from its wheel and the exact dependencies in `uv.lock`, plus tini. No uv, compiler, git, ssh or shell tools beyond the base image's.
- **Who it runs as:** the `switchboard` user (uid and gid 10001), always.
  - Started as root, as Render and plain `docker run` do, the image's `switchboard` command ([deploy/image/switchboard.sh](../deploy/image/switchboard.sh)) first makes `$SWITCHBOARD_HOME` that user's private directory. It then drops to that user with no capabilities and no way back through setuid.
  - `docker exec … switchboard …` drops to the same user.
  - Started as uid 10001, as Kubernetes does with the manifest above, it runs as that user from the start.
- **Build it yourself:** `docker build -t switchboard .` from the repository. CI builds it on every pull request and runs [tests/image](../tests/image/test_image.py) against it, behind a proxy that ends TLS (CONTRIBUTING.md, "The container image").
