# Running switchboard on a server

switchboard normally runs on your own machine: the broker listens on `127.0.0.1` and you open the web UI at `http://switchboard.localhost:7419/`. It can also run as a container on a server, a VM or a platform such as Render, EKS or GKE, behind that platform's HTTPS. This page covers that setup. The design and its threat model are in [DESIGN.md §30](DESIGN.md#30-the-container-image-and-the-public-url-34).

> [!IMPORTANT]
> A hosted broker is **your team's**: you set it up with a one-time password from its log, add people from its admin section, and everyone signs in with a password or a passkey ([Signing in](#signing-in)). Everyone's agents connect to it over HTTPS with an agent token from **Settings → Agents** ([Agents over HTTPS](USAGE.md#agents-over-https-a-preview)), so they join its rooms with nothing of switchboard's installed on their machines, and every agent takes every person's messages as its user's.

## How it fits together

```
browser ──https──▶ the platform's proxy (TLS ends here) ──http──▶ container :7419 ──▶ /data volume
your agents ──https──▶ Render's router, Caddy, an ingress           the broker          SQLite
```

- **One container, one volume, one broker.** The broker takes a lock in its data directory, and a second broker on the same volume refuses to start. Never run more than one replica. Several brokers behind one address are a different design ([#35](https://github.com/amahpour/switchboard/issues/35)).
- **The volume is a real disk:** a Render disk, EBS, a GCE persistent disk, a Docker volume. Never NFS or EFS, where SQLite's locking isn't reliable.
- **The public URL is the only address.** You tell the broker the `https://` address browsers use. It then answers only to that host, accepts writes only from that origin, marks its session cookie `Secure`, and puts that address in the sign-in links. Requests for any other host get `421`. The one exception is `GET /healthz`, which answers `ok` to anyone (platforms probe the container's own address) and nothing else.
- **TLS is the proxy's job.** The broker speaks plain HTTP on port 7419 inside the container. It never reads `X-Forwarded-*` headers: the public URL you configure decides the scheme and the host, so a forged header can't change them.
- **Redact `X-Switchboard-Token` as you do `Authorization`, if your proxy logs request headers.** Claude Code's plugin sends a token typed into its settings in that header, because an `Authorization` header (even an empty one) keeps Claude Code from offering to sign in through the browser instead. The broker logs no header, and a proxy logs none by default (Caddy's `reverse_proxy` above, an ingress, Render's router); a proxy configured to log request headers would otherwise keep the token. Upgrade the broker before the plugin: an older one ignores the header.
- **WebSockets and long calls pass through the proxy:** the web UI's WebSocket (`/ws`) and your agents' calls to `/mcp` (a listening agent's call stays open until a message comes, up to a day). Every proxy above does that by default. Don't put request buffering on them, or a timeout shorter than the call: the broker pings the web UI's WebSocket every 20 seconds.

## Try it on your machine

```bash
docker run -d --name switchboard -p 127.0.0.1:7419:7419 -v switchboard:/data \
  -e SWITCHBOARD_PUBLIC_URL=http://switchboard.localhost:7419 \
  ghcr.io/amahpour/switchboard:0.56.1
docker logs switchboard
```

Sign in as `admin` with the one-time password the log prints, then give your name and email and choose your own password or a passkey (browsers treat `*.localhost` as a secure context, so passkeys work here over plain http). Plain `http://` is accepted only for a local test host (`localhost`, `127.0.0.1`, `*.localhost` or `*.test`), and `-p 127.0.0.1:…` keeps the port off your network. For anything other machines reach, use `https://` behind a proxy, as below.

## Settings

The image sets everything but the public URL.

| Variable | In the image | What it is |
|---|---|---|
| `SWITCHBOARD_PUBLIC_URL` | (you set it) | The address browsers use: `https://<host>[:<port>]`, an origin only (no path). Required. |
| `SWITCHBOARD_LISTEN` | `0.0.0.0` | The IPv4 address the broker listens on. Anything but `127.0.0.1` needs the public URL. |
| `SWITCHBOARD_PORT` | `7419` | The port it listens on. |
| `SWITCHBOARD_HOME` | `/data/switchboard` | Its data: the database, logs and config.toml, on the `/data` volume. |
| `SWITCHBOARD_HUMAN_NAME` | (unset: `me`) | The admin's name in the rooms. The admin signs in as it, or as `admin`. While it's set, it wins over a rename in Settings, which then shows the name read-only. |
| `SWITCHBOARD_OIDC_CLIENT_ID` | (unset) | Sign in with Google: your OAuth client's ID ([Sign in with Google](#sign-in-with-google)). Nothing changes until it and the secret are both set. |
| `SWITCHBOARD_OIDC_CLIENT_SECRET` | (unset) | That client's secret, from your platform's secrets (a Kubernetes Secret, Render's secret environment). Or `SWITCHBOARD_OIDC_CLIENT_SECRET_FILE`, a file holding it. |
| `SWITCHBOARD_JOINLY_URL` | (unset) | The meeting bot's address, for example `http://127.0.0.1:8000/mcp/` ([A meeting bot](#a-meeting-bot), [Meetings on Kubernetes](#meetings-on-kubernetes)). It sets `[meeting] joinly_url` over `config.toml`, and an empty value leaves the file's. An `http` or `https` address, up to 300 characters, with no user name or password; a bad one stops the broker at start with a message that names the variable. |
| `SWITCHBOARD_MEETING_MAX_CALLS` | (unset: 1) | How many calls the broker follows at once, in all rooms together: a whole number from 1 to 50. It sets `[meeting] max_calls` over `config.toml`, and an empty value leaves the file's. Each live call takes about 2.7 GiB in the meeting bot, so size the bot first ([A meeting bot](#a-meeting-bot), [Meetings on Kubernetes](#meetings-on-kubernetes)). A bad value stops the broker at start with a message that names the variable. |
| `SWITCHBOARD_MEETING_STT_MODEL` | (unset: joinly's own) | The speech-to-text model the meeting bot transcribes with: `base`, `small`, `distil-large-v3` or `large-v3-turbo`. It sets `[meeting] stt_model` over `config.toml`, and an empty value leaves the file's. Unset, joinly keeps the model inside its image. A name makes joinly download the model on the first call ([The speech model, and a GPU](#the-speech-model-and-a-gpu)). Anything else stops the broker at start, with a message that names the variable and the four models. |
| `SWITCHBOARD_RESET_OWNER` | (unset) | Recovery: set it to a new value and restart to forget the admin, everyone else, and every password, passkey and session ([Signing in](#signing-in)). It acts once per value. |

`SWITCHBOARD_PUBLIC_URL`, `SWITCHBOARD_LISTEN` and `SWITCHBOARD_PORT` are the environment versions of `switchboard start --public-url`, `--listen` and `--port`. `config.toml` can also set `public_url` and `listen`, and a flag or variable wins over it. `SWITCHBOARD_JOINLY_URL`, `SWITCHBOARD_MEETING_MAX_CALLS` and `SWITCHBOARD_MEETING_STT_MODEL` are the environment versions of `[meeting] joinly_url`, `[meeting] max_calls` and `[meeting] stt_model`, for a manifest that has no file on the volume to write; they win over the file too. The container runs `switchboard start --foreground --log-stdout`: logs go to stdout, where the platform collects them, instead of `logs/broker.log`.

The admin's name in the rooms is `me` unless you set it. The easiest way is **Settings > Profile** in the web UI, which renames you at once and keeps it across restarts. `SWITCHBOARD_HUMAN_NAME` in the deployment (the Kubernetes and Compose files have it) fixes the name instead, and Settings can't change it while it's set; leave it out to rename from Settings. Or set `human_name` in `config.toml` in the home, then restart the container:

```bash
docker exec -u switchboard switchboard sh -c 'echo "human_name = \"ari\"" >> "$SWITCHBOARD_HOME/config.toml"'
docker restart switchboard
```

## Sizing

The broker is one process. Its busy request path uses one core; adding ingress
replicas or proxies adds no broker capacity. Several brokers need a different
design ([#35](https://github.com/amahpour/switchboard/issues/35)). Size memory for
connected agents, and leave CPU headroom for hooks while agents work.

Measured on 2026-11-09 with switchboard 0.56.1, Python 3.13.15 and an Intel Core
Ultra 7 265: only the broker was pinned to one core (`taskset`); a separate
process drove `FakeAgent` clients over loopback HTTP/MCP. Each row used a fresh
test-mode broker and temporary database, with `rate_limit_s = 0` and
`budget_per_hour = 1000000000`. Rooms held five agents. Each agent sent its next
call as soon as the last returned. **One cloud vCPU may be slower.** These are
achieved rates from one load-client process, not production capacity guarantees.

| Load | Agents | Calls/s | p50 / p99 (ms) | Broker CPU (cores) |
|---|---:|---:|---:|---:|
| `read` (`limit=20`) | 20 | 1064 | 18 / 52 | 0.99 |
| plugin hook (`PostToolUse`) | 20 | 1029 | 19 / 54 | 0.99 |
| `say` (rooms of 5) | 20 | 391 | 41 / 525 | 0.99 |
| `read` (`limit=20`) | 60 | 791 | 57 / 607 | 0.97 |
| plugin hook (`PostToolUse`) | 60 | 847 | 54 / 652 | 0.98 |
| `say` (rooms of 5) | 60 | 386 | 126 / 1062 | 0.99 |

`read` polls with `limit=20` after seeding the room; repeated reads normally have
no new messages. The hook is a synthetic `PostToolUse` through the same hidden
MCP tool the plugins call, not a running model or plugin. `say` posts a short
message and includes delivery classification for the other four room members.
Latency includes the client and HTTP/MCP round trip. A larger agent count can
add queueing without increasing throughput; use CPU and latency together when
choosing how many agents may work at once. Delivery throttles normally keep chat
traffic below these rates, but hooks arrive after every tool call.

With agents holding `wait()` open, after checking that every agent was idle:

| Agents holding `wait()` | Broker RSS (MiB) | Broker CPU (cores) | File descriptors |
|---:|---:|---:|---:|
| 0 | 105.8 | 0.002 | 13 |
| 100 | 129.9 | 0.004 | 213 |
| 500 | 221.9 | 0.008 | 1,013 |
| 1,000 | 338.3 | 0.026 | 2,013 |

This run added about **0.23 MiB and two file descriptors per connected agent**.
For mostly waiting agents with enough file descriptors, memory is the first
sizing constraint; for active agents, one core can run out first, as the
throughput table shows. An RSS-only linear extrapolation from the measured 0-to-1,000 range reaches `512Mi` near
1,750 waits and `1Gi` near 3,950. **Those larger counts were not tested, and are
not safe connection quotas:** container memory includes more than RSS and active
traffic needs additional headroom. The measurements support raising the default
to 1Gi, rather than treating either estimate as an OOM threshold.

The Helm chart and Kubernetes manifest now give the **broker container a `1Gi`
memory limit** (formerly `512Mi`). This caps the broker's total charged container
memory, including page cache; it is not a cap on the whole pod or the number of
agents. `192Mi` memory and `100m` CPU remain the scheduling requests, not maximums.
There is no broker CPU limit. For a busy deployment, raise requests to measured
steady use so the scheduler reserves enough. joinly's meeting-bot container has
its own much larger memory limit, sized separately from live calls ([below](#meetings-with-helm)).

Watch these together:

- **RSS and container memory versus the limit.** Alert before the limit, leaving
  space for traffic bursts, file uploads and database cache.
- **CPU and request latency.** Sustained use near one core and rising p99 indicate
  request saturation. The measured machine's rate is not a cloud vCPU sizing target.
- **Connected agents and open waits.** Track live connections alongside the active
  members shown by `switchboard status` / `who`; memberships can outlive a connection.
  Also watch the process's file-descriptor limit as connections grow.
- **OOM restarts.** In Kubernetes, check restart counts and the last termination
  reason (`OOMKilled`), for example with
  `kubectl -n switchboard describe pod switchboard-0`. A restart clears waits and makes agents reconnect.

Re-run the small harness from a Linux checkout with the test dependencies:

```bash
uv run --frozen python scripts/bench_broker.py --broker-cpu 0 \
  --agents 20 60 --wait-agents 0 100 500 1000 --seconds 5
```

Choose a CPU allowed by the machine's affinity set, or omit `--broker-cpu` for an
unpinned run. On a shared machine, take the same suite lock as the tests
([CONTRIBUTING.md](https://github.com/amahpour/switchboard/blob/main/CONTRIBUTING.md)).
The script prints JSON, measures the broker process alone through Linux `/proc`,
and removes its brokers and homes afterward. It uses only the standard library,
the installed broker dependencies and the repo's `tests/fakes` clients. It runs
locally inside a cluster too; it does not send load to an existing broker.

The five-second samples exclude TLS, browser WebSockets, long histories, large
messages, file transfers, real plugin lifecycle traffic and meetings. RSS is a
snapshot, not peak container memory. Measure a representative workload on your
own storage and CPU before relying on either limit.

## Deploy it

Each example pins a release (`ghcr.io/amahpour/switchboard:0.56.1`). Every release moves these pins, and `:latest` follows the newest release.

### A VM with Docker Compose

[deploy/compose/](https://github.com/amahpour/switchboard/tree/main/deploy/compose/) runs the broker with Caddy in front of it. Caddy gets a certificate from Let's Encrypt for your domain and renews it. You need a VM with Docker, a DNS name pointing at it, and ports 80 and 443 open.

```bash
export SWITCHBOARD_DOMAIN=sb.example.com
docker compose -f deploy/compose/compose.yaml up -d
docker compose -f deploy/compose/compose.yaml logs switchboard    # the admin's one-time password
```

### Kubernetes (EKS, GKE, AKS)

[deploy/kubernetes/switchboard.yaml](https://github.com/amahpour/switchboard/blob/main/deploy/kubernetes/switchboard.yaml) is a Service, a one-replica StatefulSet with a 20 GiB volume claim, and an Ingress. Edit `sb.example.com` (the Ingress host, its TLS secret and `SWITCHBOARD_PUBLIC_URL`) first. Where you deploy with Helm or Argo CD, use [the Helm chart](#helm) instead: it renders the same objects from values. The claim is 20 GiB because a room's files sit on the same volume as the database. The Ingress sets `nginx.ingress.kubernetes.io/proxy-body-size: "26m"`, one megabyte over the default 25 MB file cap, so an upload is not cut off by the proxy. A proxy in front of the broker needs a body limit at least `[files] max_mb`. Caddy, in the Compose example, has no default body limit.

```bash
kubectl create namespace switchboard
kubectl -n switchboard apply -f deploy/kubernetes/switchboard.yaml
kubectl -n switchboard logs switchboard-0    # the admin's one-time password
```

What the manifest relies on:

- **The pod runs as uid 10001 from the start**, with a read-only root filesystem, no capabilities and `/tmp` as an `emptyDir`. `fsGroup: 10001` makes the volume writable. `fsGroupChangePolicy: OnRootMismatch` stops Kubernetes from re-chmodding the broker's private home on every start; without it, the broker would refuse its own directory.
- **Service links are off** (`enableServiceLinks: false`). Kubernetes gives each pod a variable per Service in its namespace, and a Service named `switchboard` makes one called `SWITCHBOARD_PORT=tcp://<cluster-ip>:7419`, which overrides the image's own `SWITCHBOARD_PORT` and stops the broker: `'tcp://…' is not a port number` (#595). Keep the setting in your own manifest if you name the Service `switchboard`.
- **Replacing the pod is stop-then-start.** A StatefulSet with one replica stops its pod before it starts the new one, as a Deployment's `Recreate` strategy would.
- **The Ingress passes the browser's Host and Origin through**, which ingress-nginx and most controllers do by default. The WebSocket needs no timeout annotation, because the broker pings the UI's every 20 seconds.
- **The Ingress keeps a quiet request open for 16 minutes** (`proxy-read-timeout` and `proxy-send-timeout`, 960 s), for agents over HTTPS: Claude Code's listener waits up to 15 minutes at `/mcp/listen` before it starts its next wait, and ingress-nginx's own default closes a quiet request after 60 seconds. An agent's tool calls and Codex's Stop hook need nothing, because the broker sends them a keepalive every 15 seconds. A proxy that cuts the wait sooner doesn't lose a message: the listener just starts its next wait at once. Another controller has its own setting for this, such as a load balancer's idle timeout.
- **A Gateway API route needs its timeouts set by hand.** Kubernetes' Gateway API has no annotations: set `timeouts: {request: 960s, backendRequest: 960s}` on the `HTTPRoute` rule that serves `/mcp`. An Envoy-based gateway ends a request that runs longer than its route's timeout whatever is being sent on it, and the default can be much shorter than one of Claude Code's 15-minute listener rounds. `wait()` runs into the same limit: it is one request too, held up to 600 seconds for Claude Code with its current plugin and 280 for Codex (`wait_cap_s` in `config.toml` changes that, but never past what each harness's own client allows, [USAGE.md](USAGE.md) has the limits, and a route's timeout has to stay above whatever it is set to), and a route that cuts sooner fails it with `upstream request timeout`. A cut listener round is harmless (the listener posts again at once), but a short limit makes every round a cut. If an idle agent over HTTPS stops being woken, the broker's log has a warning for a listener it turned away (`listener refused for session …`, with the reason), and the plugin's `listen.log` (in its data folder, or `~/.cache/switchboard`) has a line for each time the listener stopped on something unexpected (#436).
- **Use a block-storage class** (the default on EKS, GKE and AKS), not an NFS or EFS one.
- **Following live calls adds a second container to the pod:** a patch of this StatefulSet in [deploy/kubernetes/meetings/](https://github.com/amahpour/switchboard/tree/main/deploy/kubernetes/meetings/), which leaves this manifest as it is. See [Meetings on Kubernetes](#meetings-on-kubernetes), or `meetings.enabled` in [the Helm chart](#helm).

### Helm

[deploy/helm/switchboard/](https://github.com/amahpour/switchboard/tree/main/deploy/helm/switchboard/) is a chart that renders what [the Kubernetes manifest](#kubernetes-eks-gke-aks) and [the meeting bot's patches](#meetings-on-kubernetes) do: a Service, a StatefulSet with one replica and a ReadWriteOnce claim, and an Ingress, with the meeting bot as an option. It is the same hardening, with the numbers as values. CI renders it with Helm 4.3.1 and compares the result with the manifests, so the two can't drift.

There is no chart repository yet. Install from a checkout of the release you want (its tag carries the chart, and the chart installs the image of that release), or let Argo CD read it from git ([below](#helm-and-argo-cd)).

Keep your values in a file, so an upgrade can read the same one:

```yaml
# switchboard-values.yaml
publicUrl: https://sb.example.com
humanName: alice
```

```bash
git clone --depth 1 --branch vX.Y.Z https://github.com/amahpour/switchboard.git    # X.Y.Z: the release (CHANGELOG)
helm install switchboard switchboard/deploy/helm/switchboard \
  --namespace switchboard --create-namespace -f switchboard-values.yaml
kubectl -n switchboard logs switchboard-0    # the admin's one-time password
```

`helm install` prints the same two steps as notes. The release is called `switchboard` so that the pod is `switchboard-0`, as every command on this page has it; another release name `foo` gives `foo-switchboard-0` (`nameOverride` and `fullnameOverride` change that). `publicUrl` is required: the chart does not render without it, and says what to set. It must be an origin, with no path.

**The values that matter** (all of them are in [values.yaml](https://github.com/amahpour/switchboard/blob/main/deploy/helm/switchboard/values.yaml), with comments; a key the chart doesn't know is refused, so a typo fails instead of being ignored):

| Value | Default | What it does |
|---|---|---|
| `publicUrl` | (required) | `SWITCHBOARD_PUBLIC_URL`: the `https://` address browsers use. |
| `humanName` | (unset) | `SWITCHBOARD_HUMAN_NAME`: the admin's name in the rooms. Unset, Settings can rename the admin. |
| `image.repository`, `image.tag`, `image.pullPolicy` | the published image, the chart's `appVersion`, `IfNotPresent` | The broker's image. An empty tag is the release the chart belongs to, never `latest`. |
| `persistence.size`, `persistence.storageClass` | `20Gi`, the cluster's default | The claim. Use block storage (EBS, a persistent disk, Azure Disk), never NFS or EFS. |
| `ingress.enabled`, `ingress.className`, `ingress.host`, `ingress.tls.enabled`, `ingress.tls.secretName` | on, `nginx`, `publicUrl`'s host, on, `switchboard-tls` | The Ingress. The broker answers to the public URL's host alone, so that is the default host. |
| `ingress.annotations` | the three ingress-nginx ones | `proxy-read-timeout` and `proxy-send-timeout` of 960 s, which keep an agent's 15-minute wait open, and `proxy-body-size` of `26m`, one megabyte over `[files] max_mb` ([why](#kubernetes-eks-gke-aks)). |
| `resources` | 100m and 192Mi requested, 1Gi limit | The broker container's ([Sizing](#sizing)). |
| `meetings.enabled` | `false` | Adds joinly beside the broker ([below](#meetings-with-helm)). |
| `meetings.maxCalls` | `1` | How many calls may run at once, 1 to 50. It sizes joinly. |
| `meetings.image.tag`, `meetings.image.digest`, `meetings.image.cudaDigest` | `v0.5.4` and the two index digests | joinly's release. `digest` is the CPU image and `cudaDigest` is the CUDA image. Move all three together ([Upgrading](#upgrading)). |
| `meetings.zoomFix.configMap` | (none) | A ConfigMap with the fixed `zoom.py`, mounted over joinly's copy. |
| `meetings.prerollFix.configMap` | (none) | A ConfigMap with the fixed `transcription.py`, mounted over joinly's copy ([why](#a-meeting-bot)). |
| `meetings.sttModel` | (unset) | `SWITCHBOARD_MEETING_STT_MODEL`: `base`, `small`, `distil-large-v3` or `large-v3-turbo`. Anything else fails at `helm template`. A name also gives joinly a folder to download into ([below](#meetings-with-helm)). |
| `meetings.gpu.enabled` | `false` | joinly on one NVIDIA GPU: its `-cuda` image and `nvidia.com/gpu: 1` ([below](#meetings-with-helm)). |
| `meetings.gpu.nodeSelector`, `meetings.gpu.tolerations` | `{}`, `[]` | Written into the pod while the GPU is on. |

There is no replica count: one broker, one volume, as the manifest has it ([How it fits together](#how-it-fits-together)). The pods are selected by `app.kubernetes.io/name`, `instance` and `component: broker`. The component is there so that a second kind of pod can be added later without taking over the broker's selector, which a StatefulSet can't change once it exists.

**What the chart changes from the manifest**, and nothing else (a test compares the two):

- **Labels.** Helm's standard set on every object (`helm.sh/chart`, `app.kubernetes.io/instance`, `version`, `managed-by`) and the `component: broker` label. The manifest has only `app.kubernetes.io/name`.
- **The pull policy is written out** (`IfNotPresent`, Kubernetes' own default for a release tag), so a private mirror can set it.
- **The claim template states its `apiVersion` and `kind`.** The API server writes them into the stored object anyway, and a tool that compares the chart's output with it (Argo CD) would otherwise see a difference.
- **The Ingress host is taken from `publicUrl`** unless `ingress.host` says otherwise. It must be the same host: the broker answers `421` to any other.
- **With the meeting bot on,** joinly's memory and CPU come from `meetings.maxCalls` instead of the patch's fixed 1 CPU, 2 GiB requested and 4 GiB limit ([below](#meetings-with-helm)), the broker also gets `SWITCHBOARD_MEETING_MAX_CALLS`, and the broker's container is listed first (kustomize puts joinly first). `meetings.sttModel` and `meetings.gpu` add what [the GPU patch](#meetings-on-kubernetes) describes, and nothing when they are unset.
- **Everything else is the manifest's:** `enableServiceLinks: false` ([#595](https://github.com/amahpour/switchboard/issues/595)), uid 10001 from the start with a read-only root and no capabilities, the probes, the stop-then-start replacement, and the Ingress annotations. [What the manifest relies on](#kubernetes-eks-gke-aks) applies to the chart as it is.

**cert-manager.** Values you give are merged into the defaults, so adding an annotation keeps the three ingress-nginx ones:

```yaml
publicUrl: https://sb.example.com
ingress:
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
```

**Another ingress controller.** A value set to `null` is dropped, so move to your controller by nulling the ingress-nginx annotations and adding its own settings. Its idle or request timeout must outlast 15 minutes, as [the Kubernetes section](#kubernetes-eks-gke-aks) says, and its body limit must be at least `[files] max_mb`.

```yaml
ingress:
  className: my-controller
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: null
    nginx.ingress.kubernetes.io/proxy-send-timeout: null
    nginx.ingress.kubernetes.io/proxy-body-size: null
    # your controller's own, for the timeout and the body size
```

With a Gateway API route, set `ingress.enabled: false` and write the `HTTPRoute` yourself, with the timeouts that section gives. It sends to the Service named like the pod without its `-0` (`switchboard` for the release above), port 7419.

**Upgrading.** Clone the new release's tag the same way and run `helm upgrade` with the same values file, or point Argo CD at the new tag. The image follows the chart, so there is nothing to edit; set `image.tag` only to stay on another release ([Upgrading](#upgrading) has what a release can change). The pod is replaced stop-then-start, and the broker migrates its database on start.

```bash
git clone --depth 1 --branch vX.Y.Z https://github.com/amahpour/switchboard.git switchboard-vX.Y.Z
helm upgrade switchboard switchboard-vX.Y.Z/deploy/helm/switchboard --namespace switchboard -f switchboard-values.yaml
```

Two things to know:

- **The volume's size and class are fixed at install.** A StatefulSet's `volumeClaimTemplates` can't change once it exists, so `helm upgrade --set persistence.size=50Gi` fails with `spec.volumeClaimTemplates: Invalid value` (seen on a test cluster). To grow the volume, edit the claim (`kubectl -n switchboard edit pvc data-switchboard-0`, if the storage class allows expansion) and leave the value as it was.
- **`helm uninstall` keeps the volume.** The StatefulSet makes the claim, not the chart, and Kubernetes leaves it when the StatefulSet goes. That protects your rooms, and it means a reinstall under the same name finds the old data. Delete the claim yourself to remove them (`kubectl -n switchboard delete pvc data-switchboard-0`).

#### Meetings with Helm

`meetings.enabled: true` (or `--set meetings.enabled=true`) adds joinly as a second container of the broker's pod, with what [Meetings on Kubernetes](#meetings-on-kubernetes) describes: it listens on the pod's `127.0.0.1` only with no Service and no declared port, runs as uid 1001 with a read-only root and no capabilities, is probed from inside, has no readiness probe (so it can't take the broker out of its Service), and the pod's `default-container` is the broker, so `kubectl logs switchboard-0` still reads the broker's log. The chart renders all of that unchanged. The first start pulls a 3.7 GB image, so the pod waits in `ContainerCreating` for a few minutes, and the node needs a way out to the internet.

`meetings.maxCalls` is how many calls may be joining or live at once, and it sets two things from one number. The broker gets `SWITCHBOARD_MEETING_MAX_CALLS`, its `[meeting] max_calls`, which refuses a call past it (a broker release without that setting ignores the variable). And joinly's resources are worked out from it, because one joinly follows every call, each call holds about 2.7 GiB and 1 CPU while it is live (measured, [#604](https://github.com/amahpour/switchboard/issues/604)), and a joinly past its memory limit is killed:

> request = 2.7 GiB × `maxCalls` + 0.5 GiB, rounded up to a multiple of 0.5 GiB. Limit = request + 0.5 GiB. CPU request = `maxCalls`. No CPU limit, because throttling an audio pipeline makes the transcript fall behind.

| `maxCalls` | joinly memory requested | memory limit | CPU requested |
|---|---|---|---|
| 1 | 3.5 GiB | 4 GiB | 1 |
| 2 | 6 GiB | 6.5 GiB | 2 |
| 3 | 9 GiB | 9.5 GiB | 3 |
| 5 | 14 GiB | 14.5 GiB | 5 |
| 10 | 27.5 GiB | 28 GiB | 10 |

That is on top of the broker's. The pod must fit on one node, so the table is also a node-size guide. Changing `maxCalls` replaces the pod, which ends any call in progress: do it when no call is on. The chart refuses a value outside 1 to 50, as the broker would at start.

**Speaker names on Zoom.** Make the ConfigMap first, from the pinned file ([the commands](#meetings-on-kubernetes)), then name it in your values. The pod can't start until the ConfigMap exists, so with Argo CD create it outside the chart or in an earlier sync wave.

```bash
kubectl -n switchboard create configmap joinly-zoom-fix --from-file=zoom.py=./zoom.py
```

```yaml
# switchboard-values.yaml, then helm upgrade
meetings:
  enabled: true
  maxCalls: 2
  zoomFix:
    configMap: joinly-zoom-fix
```

Remove `zoomFix` once a joinly release has the fix, and move `meetings.image.tag`, `meetings.image.digest` and `meetings.image.cudaDigest` to that release.

**First words of a call line.** joinly 0.5.4 clips the first word of what it hears, and the fix ([joinly-ai/joinly#188](https://github.com/joinly-ai/joinly/pull/188)) is a second file ([why, and what it did](#a-meeting-bot)). Make its ConfigMap first, from the pinned file ([the commands](#meetings-on-kubernetes)), then name it. The pod can't start until it exists. joinly needs no setting for it, and the broker sends none, so a chart without `prerollFix` works as before.

```yaml
# switchboard-values.yaml, then helm upgrade
meetings:
  enabled: true
  prerollFix:
    configMap: joinly-preroll-fix
```

Remove `prerollFix` once a joinly release has the fix too. It is independent of `zoomFix`: use either or both.

**A GPU and a better model.** `meetings.gpu.enabled` runs joinly on one NVIDIA GPU, and `meetings.sttModel` names the model ([why `large-v3-turbo`](#the-speech-model-and-a-gpu)):

```yaml
# switchboard-values.yaml, then helm upgrade
meetings:
  enabled: true
  sttModel: large-v3-turbo
  gpu:
    enabled: true
    nodeSelector:
      nvidia.com/gpu.present: "true"
    tolerations:
      - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
```

- **The image is the CUDA build of the same release.** `v0.5.4` becomes `v0.5.4-cuda`, and the digest is `meetings.image.cudaDigest`, not the CPU digest. A tag that already ends in `-cuda` is kept as it is. Moving only the tag leaves the previous digest on the new tag, which is a different image: move `meetings.image.digest` and `meetings.image.cudaDigest` with the tag ([Upgrading](#upgrading)).
- **joinly asks for one GPU, in its limits.** The count is fixed at one: one joinly serves every call. Memory and CPU are what `maxCalls` works out, as without a GPU. The cluster needs the NVIDIA device plugin (or the GPU Operator), or the pod stays `Pending`.
- **The pod goes where the GPU is.** The broker shares the pod, so `nodeSelector` and `tolerations` are written into it as you give them, and only while `gpu.enabled` is true. Quote a label value that looks like a boolean (`"true"`), as above, or Kubernetes refuses it.
- **A model you name gets a folder to be downloaded into:** a 4 GiB `emptyDir` at `/home/app/.cache/huggingface`, because joinly's root is read-only. It is gone when the pod is replaced, which every `helm upgrade` that changes the pod does, so the next pod downloads the model again and the first call waits for it (about 1.6 GB for `large-v3-turbo`). With `sttModel` unset, nothing is mounted, joinly uses the model in its image, and nothing is downloaded. A cache that outlives the pod is not in the chart: the claims of a StatefulSet are fixed once it exists, and the broker's volume is the broker's. The chart needs the bot on (`meetings.enabled`) for either value to render anything.
- **The sizes are for `base`.** The table above was measured with it, and a bigger model adds to each call ([Memory](#the-speech-model-and-a-gpu)). The chart does not add it for you.
- **Checked by tests that render the chart,** not on a GPU node: the image, the limit, the selector, the tolerations, the variable and the folder, and the schema refusing a model the broker would refuse.

**Check that it works** with the commands under [Meetings on Kubernetes](#meetings-on-kubernetes): `kubectl -n switchboard get pod switchboard-0` says `2/2`, the broker's container reaches `http://127.0.0.1:8000/health`, and a throwaway pod gets `Connection refused` at the pod's IP on port 8000.

#### Helm and Argo CD

An Argo CD `Application` can read the chart straight from this repository. Pin `targetRevision` to a release tag, so that an upgrade happens when you change it: the tag carries the chart, and the chart installs that release's image.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: switchboard
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/amahpour/switchboard.git
    targetRevision: vX.Y.Z              # the release's tag (CHANGELOG)
    path: deploy/helm/switchboard
    helm:
      releaseName: switchboard          # makes the pod switchboard-0
      values: |
        publicUrl: https://sb.example.com
        humanName: alice
        meetings:
          enabled: true
          maxCalls: 2
          zoomFix:
            configMap: joinly-zoom-fix  # made beforehand; drop these two lines to skip the Zoom fix
          prerollFix:
            configMap: joinly-preroll-fix  # made beforehand too; drop these two lines to skip the first-word fix
  destination:
    server: https://kubernetes.default.svc
    namespace: switchboard
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
```

- **Keep a hand-made patch out of it.** A `kubectl patch` of the StatefulSet is reverted at the next sync. The chart is the way to add the meeting bot under Argo CD, and a plain-manifest user uses kustomize ([Meetings on Kubernetes](#meetings-on-kubernetes)).
- **The sync is stop-then-start.** The StatefulSet replaces its one pod by stopping it first, so a sync that changes the pod is a short outage, and a call in progress ends.
- **The volume is the StatefulSet's, not the chart's.** The claim holds your rooms and is made by the StatefulSet, so it is not in the chart's output. Take a snapshot ([Backups](#backups)) before you delete an `Application` or turn on pruning for the first time, and check afterward that `kubectl -n switchboard get pvc` still lists the claim.
- **The admin's one-time password** is in the pod's log, as above (`kubectl -n switchboard logs switchboard-0`), or in the log view of Argo CD's UI.
### Render

[deploy/render/render.yaml](https://github.com/amahpour/switchboard/blob/main/deploy/render/render.yaml) is a Blueprint: a web service from the published image, with a 20 GB disk at `/data`. Put it in a repository of yours as `render.yaml` (or point the Blueprint at its path), then choose **New > Blueprint**. Render asks for `SWITCHBOARD_PUBLIC_URL`: the service's `https://<name>.onrender.com` address, or your custom domain. The admin's one-time password is in the service's **Logs** tab. Files a room holds count against that disk, and against its snapshots.

Render's disks need a paid plan and allow only one instance. A deploy stops the old instance before it starts the new one, and Render snapshots the disk daily and keeps each snapshot at least 7 days. Render's router ends TLS and redirects plain HTTP to HTTPS before it reaches the broker. `PORT=7419` in the Blueprint tells Render which port to send traffic to.

The disk belongs to root, so the container starts as root. Its `switchboard` command makes its home on the disk (`/data/switchboard`) the unprivileged user's, then runs as that user from then on.

## Signing in

The sign-in page offers three ways in: your **name and password**, a **passkey** (Touch ID, Face ID, Windows Hello, your phone or a security key), and **Sign in with Google** once you set it up ([below](#sign-in-with-google)); until then it shows SSO as coming soon.

A fresh broker has no admin. Until it does, it prints one line in its log:

```
switchboard isn't set up yet. Sign in at https://sb.example.com as admin with the one-time password 7KQ4-M2XD-9HVA-3JNP (it works once, for 60 min), then choose your own password or passkey. Or open https://sb.example.com/setup#t=…
```

1. **Set it up.** Take the newest such line from wherever you read the container's logs (your deploy tool's logs view, `kubectl logs`, `docker logs`, Render's Logs tab), sign in as `admin` with that one-time password, give your name and email, and choose your own password, or a passkey instead. That makes you the admin and signs that browser in; you sign in with that email from then on (`admin` still works). The link in the same line does the same without typing.
2. **Add people** from **Admin > People** in the sidebar. Give their first and last name and their email; their name in the rooms (what others @mention) fills in from the email, and you can change it. The rooms show them by their real name, with that username beside it. Send them the invite it shows: the address, the email they sign in with and a one-time password, good for 7 days. They sign in with it and choose their own password or a passkey. Their card's **First name**, **Last name** and **Email** can be changed later; from then on the new email signs them in, and the rooms show the new name at once. You see the one-time password once; **New one-time password** makes another (and signs them out everywhere, for a forgotten password), and **Remove** signs them out everywhere at once, ends their password and passkeys, and revokes their agent tokens, which ends their agents. Their messages stay. **Make admin** on their card makes them an admin, after you confirm it's you: they can then do everything you can, including managing people, except remove you or take your admin away. **Remove admin** takes it back and does not ask you to confirm it's you. Your own card says **Owner**.
3. **From then on**, everyone signs in with their email and password, or a passkey. A name signs no one in: someone with no email set can't sign in until you set one, and **Admin > People** says how many people that is and lists them. `admin` always works for you. The key button next to sign-off in the app opens the Sign-in sheet: change your password, add a passkey (both ask you to confirm it's you first, unless you did in the last five minutes), or **Sign out everywhere**, which signs out every browser you're signed in to and nobody else.
4. **Once set up, no one-time password is ever printed again.** Upgrades and restarts keep the admin and everyone else: people, passwords and passkeys live in the database on the volume, with your rooms. Only a brand-new, empty volume isn't set up.

Everyone signed in posts under their own name, runs every room command, makes their own agent tokens, and **every agent takes every person's messages as its user's** (`kind=human`). An admin can also manage people, change the agents' avatars and see every private room's name, and any admin can take over any other account but the owner's by setting its email or giving it a new one-time password. That fits a team on a private network; don't hand out invites, or admin, to people you wouldn't let steer your agents.

What to know about the admin's one-time password:

- **It works once and expires.** A fresh line is printed every hour until someone uses it, and after a restart; use the newest. In the link it is after `#`, which browsers never send to a server, so it can't land in a proxy's access log.
- **Anyone who can read the logs during that window could set it up first**: a read-only role in your deploy tool, or a log service the logs ship to. You would know at once, because the newest one would say it was already used, and you would get an empty broker back with the reset below. Bots can't read your logs.
- **Wrong passwords slow down.** After 5 wrong ones for an account in 15 minutes, it waits 30 s before the next try, doubling up to 15 minutes; each later wrong try keeps that wait until a correct password or 24 hours quiet. The admin's aliases share one limit. A browser that has signed in before gets its own limit for 30 days (until the broker restarts), so a stranger's guesses cannot lock it out. During a flood, new browsers' password checks queue behind a small limit and past it are told to try again in a few seconds; browsers that have signed in before check on a worker of their own. A chosen password must also avoid the common-password blocklist.
- **Passkeys need a name and a secure context.** They are on behind an `https://` public URL whose host is a DNS name (or `http://` on `*.localhost`, for a try on your machine). Behind `http://…test` or a public URL that is an IP address the broker says so in its log, and everyone uses passwords.

**If you lose your password and every passkey**, set `SWITCHBOARD_RESET_OWNER` to a new value (today's date, say) in the deployment and restart. The broker forgets the admin and everyone else, deletes every password, passkey and session, and prints a fresh one-time password. It acts **once per value** and ignores that value from then on, so leaving the variable set is harmless across restarts and pod moves; to reset again, change it. This is the one step that needs ops access, and it can't be undone.

**The shell still signs in, as the admin.** `switchboard login` run **inside the container, in a terminal**, prints a one-time link as it always did, before or after setup:

- **Docker:** `docker exec -it switchboard switchboard login`
- **Compose:** `docker compose -f deploy/compose/compose.yaml exec switchboard switchboard login`
- **Kubernetes:** `kubectl -n switchboard exec -it switchboard-0 -- switchboard login`
- **Render:** open the service's **Shell** tab and run `switchboard login`.

Leave out `-t` (as in `docker exec` without `-it`) and the broker refuses: links go only to a terminal someone typed in. Whoever can exec into the container can sign in and could read its database anyway, so treat access to the platform or cluster as access to switchboard. A session from the shell can't add a passkey or change the password on its own: only your password or passkeys can confirm that. `switchboard logout --all` in the same way signs out every browser, everyone's.

### Sign in with Google

People you added sign in with their Google account instead of a password. Only them: the Google account must be the email you set for them in **Admin > People**, and an account nobody here has is turned away, whatever its domain.

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create an **OAuth client ID** of type **Web application**. Under **Authorized redirect URIs**, add `https://<your public URL>/auth/oidc/callback`, exactly. The consent screen needs only the default `openid` and `email` scopes; set it to **Internal** for a Google Workspace organisation.
2. Put its client ID in `SWITCHBOARD_OIDC_CLIENT_ID`, and its secret in `SWITCHBOARD_OIDC_CLIENT_SECRET` from your platform's secrets (in Kubernetes, a Secret referenced with `secretKeyRef`; or mount it as a file and set `SWITCHBOARD_OIDC_CLIENT_SECRET_FILE`). Restart. The log says `sign-in with google is on`.
3. In **Admin > People**, set each person's **Email** to the email of their Google account, and your own on your row. They choose **Sign in with Google** on the sign-in page from then on. Someone you invited who signs in with Google never needs their one-time password: it stops working.

The broker reaches out to Google (`accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com`) over HTTPS when someone signs in, and so must their browser. Removing a person ends their Google sign-in too. Any OpenID Connect provider works the same way with `SWITCHBOARD_OIDC_ISSUER` set to its issuer URL (Microsoft Entra ID, Okta, GitLab, Keycloak); the button then says "Sign in with SSO".

## Upgrading

1. Change switchboard's image tag in your compose file, manifest or Blueprint to the new release (the [CHANGELOG](https://github.com/amahpour/switchboard/blob/main/CHANGELOG.md) lists them), and deploy. With [the Helm chart](#helm), move to the new release's tag and run `helm upgrade`: the chart installs the image of its own release. That image stays a tag (`ghcr.io/amahpour/switchboard:X.Y.Z`). A release writes the tag before the image exists, so it has no digest yet.
2. The platform stops the old container and starts the new one. The broker shuts down cleanly on SIGTERM (`docker stop`) and exits 0.
3. If the new release changes the database's schema, the broker migrates it on start, after a verified backup copy next to it (`switchboard.db.v2.bak` for the passkeys release, `switchboard.db.v3.bak` for the people release, [DESIGN.md §27.6](DESIGN.md), [§31.2](DESIGN.md), [§32.2](DESIGN.md)). An older release refuses a newer database, so a rollback past a schema change means restoring a backup. The admin, people, passwords and passkeys are in the database, so an upgrade keeps them. The email sign-in release (schema 12, [DESIGN.md §39](DESIGN.md#39-identity-by-email-192)) keeps everyone signing in as before until you set their email in **Admin > People**; then they sign in with the email instead of their name, so tell them first.

Pin switchboard's releases by tag rather than `:latest`, so an upgrade happens when you choose.

**Third-party images are `name:tag@sha256:<digest>`.** joinly (the CPU build and the CUDA build, each with its own digest), Caddy, the sandbox's Debian, the `busybox` probe and the Tart Ubuntu image are pinned that way. A moved tag cannot change what is pulled. To move one:

1. Read the new tag's digest without pulling it: `docker buildx imagetools inspect <name:tag>`. Use the `Digest:` of the index (`application/vnd.oci.image.index.v1+json`, or a manifest list). That is the multi-platform pin, so a Mac and a Windows machine resolve the same image. If the tag is a single manifest rather than a list, that manifest's digest is the pin.
2. Replace `name:tag@sha256:…` everywhere that image is named: the compose files, the Kubernetes manifest, the Helm values (`meetings.image.tag`, `meetings.image.digest` and `meetings.image.cudaDigest` move together; appending `-cuda` to a digest-pinned name is not the CUDA image), the host scripts and the docs.

The CPU digest on the CUDA tag, or the other way round, is a different image. Do not copy one digest onto the other.

## Backups

- **Disk snapshots:** Render takes them daily. On AWS and GCP, use EBS or persistent-disk snapshots, for example with AWS Backup or a snapshot schedule. A snapshot of a running broker is like a power cut, which SQLite in WAL mode (the broker's setting) is built to recover from.
- **A copy while it runs**, with SQLite's backup API, then out of the container:

  ```bash
  docker exec -u switchboard switchboard sh -c 'umask 077; python3 -c "import sqlite3; \
    sqlite3.connect(\"$SWITCHBOARD_HOME/switchboard.db\").backup(sqlite3.connect(\"/tmp/switchboard-backup.db\"))"'
  docker cp switchboard:/tmp/switchboard-backup.db ./switchboard-$(date +%F).db
  ```

- **Continuous replication:** [Litestream](https://litestream.io) can stream the database to S3-compatible storage, running beside the broker on the same volume.
- **Attached files are not in the database.** A room's files live in `$SWITCHBOARD_HOME/files/` beside it ([the attachments guide](guide/attachments.md)), so a copy of `switchboard.db` alone restores the cards without the files. A disk snapshot has both; with the backup API, copy that folder too (`docker cp switchboard:/data/switchboard/files ./files-$(date +%F)`: the image keeps its home in `/data/switchboard`).

To restore, stop the broker, put the copy at `$SWITCHBOARD_HOME/switchboard.db` owned by uid 10001 with mode 0600 (and no `-wal` or `-shm` files next to it), and start it again.

## A meeting bot

A room can follow a Zoom, Meet or Teams call ([DESIGN.md §52](DESIGN.md#52-meetings-a-live-call-in-a-room-573)). The bot is [joinly](https://github.com/joinly-ai/joinly), run beside the broker. It has no sign-in and takes its settings from whoever connects, so publish it on localhost, or only on the compose network, never on an open address.

On one machine:

```bash
docker run -d --name joinly -p 127.0.0.1:8010:8000 ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0
```

Then in the broker's `config.toml`:

```toml
[meeting]
joinly_url = "http://127.0.0.1:8010/mcp/"
```

Restart the broker. Or set the environment variable `SWITCHBOARD_JOINLY_URL` to the same address, which is easier where there is no file to edit (a Kubernetes manifest); when both are set, the variable wins. `/meeting <link>` with no server set names this page. `/meeting` alone only says whether a call is live: it does not look for the server.

**How many calls at once.** One joinly follows several calls at the same time, a session and a browser for each, but it has no limit of its own and a call is heavy. Measured with two real Zoom calls open at once: about 2.7 GiB of memory and one CPU for each live call (5.5 GiB and about 2 CPUs for the two), and more while people talk. So the broker follows **one** call at a time unless you say more, and refuses a start past the limit before anything reaches joinly. The limit is `max_calls` under `[meeting]`, from 1 to 50, or the environment variable `SWITCHBOARD_MEETING_MAX_CALLS`, which wins over the file:

```toml
[meeting]
joinly_url = "http://127.0.0.1:8010/mcp/"
max_calls = 2
```

Give joinly `memory ≈ 2.7 GiB × max_calls + 0.5 GiB`, and a CPU for each call. A broker told to follow more calls than the bot's memory holds would have joinly killed for going over it, and every call on it would end.

| `max_calls` | Memory for joinly | CPU |
|---|---|---|
| 1 (the default) | 3.2 GiB, so `docker run --memory 4g` | 1 |
| 2 | 5.9 GiB, so `--memory 6g` | 2 |
| 3 | 8.6 GiB, so `--memory 9g` | 3 |
| 5 | 14 GiB, so `--memory 14g` | 5 |

The count is over every room and includes a call that is still joining. A call ended by `/meeting leave` stops counting once the bot has left it. A refused start says `This broker follows at most 1 call at once, and that many are open. Leave one first (/meeting leave in its room).` and writes nothing. It names no room, because the person who asked may not be able to see it.

**The voice.** When a person lets agents speak in a call (`/meeting voice on`), joinly says their lines with Kokoro, its text-to-speech model. The voice is a setting in the same table:

```toml
[meeting]
joinly_url = "http://127.0.0.1:8010/mcp/"
tts_voice = "bm_george"
```

`tts_voice` is a short word of lowercase letters, digits and `_`, and `bm_george` is the default. It is sent to joinly each time a call starts, as that call's own setting, so changing it needs a broker restart and a new call, and joinly's container needs nothing. It goes to whatever text-to-speech service joinly runs as its `voice` argument, and the image runs Kokoro. joinly 0.5.4 ships these voices: `af_` and `am_` (American English, women and men), `bf_` and `bm_` (British English), and others for other languages (`ef_dora`, `ff_siwis`, `if_sara`, `jf_`, `pf_`, `zf_` and more). A name joinly does not have makes the speech fail, and the room says it could not speak. Speaking and snapshots need nothing else in the container. A snapshot is a file, so it counts against `[files]` (25 MB a file and 500 MB a room by default, [DESIGN.md §53](DESIGN.md#53-attachments-544)); joinly's frame is a small JPEG, far under that.

Beside the server's container, add a service on the same network and do not publish its port. Point `joinly_url` at that service, for example `http://joinly:8000/mcp/`. The broker reaches it; nothing outside the network does.

**With the Compose example** ([deploy/compose/](https://github.com/amahpour/switchboard/tree/main/deploy/compose/)), that service is already there, behind the `meetings` profile: it publishes no port, and only the broker reaches it. Turn it on with a `.env` file beside `compose.yaml`, then `docker compose -f deploy/compose/compose.yaml up -d` as before:

```bash
COMPOSE_PROFILES=meetings
SWITCHBOARD_JOINLY_URL=http://joinly:8000/mcp/
```

Without those two lines the bot doesn't start, and `/meeting` says how to set it up. For a GPU, the `meetings-gpu` profile does the same with a CUDA build ([below](#the-speech-model-and-a-gpu)). Each live call takes about 2.7 GiB and 1 CPU on that machine. For Zoom speaker names, download the Zoom fix below to `deploy/compose/zoom.py`, and for the first-word fix download that one to `deploy/compose/transcription.py`. Then uncomment the line of each under `volumes:` in `joinly` in `compose.yaml` (a missing file would stop the bot).

**Speaker names on Zoom.** joinly 0.5.4 gives no speaker name on Zoom, so every line there shows as `call`; Meet and Teams are unaffected. Its browser has no graphics acceleration, so Zoom shows one tile that follows whoever is talking, and joinly's lookup doesn't know that tile. The fix is [joinly-ai/joinly#187](https://github.com/joinly-ai/joinly/pull/187); until a joinly release has it, the names come through only with that file patched into the image. Its `zoom.py` differs from 0.5.4's by the fix alone, so you can mount it over the image's copy, read-only:

```bash
curl -fsSLo zoom.py https://raw.githubusercontent.com/amahpour/joinly/9fbd07ceccbbfc358d697378c3cb1d7b911b17a5/joinly/providers/browser/platforms/zoom.py
docker run -d --name joinly -p 127.0.0.1:8010:8000 \
  -v "$PWD/zoom.py:/app/.venv/lib/python3.12/site-packages/joinly/providers/browser/platforms/zoom.py:ro" \
  ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0
```

Drop the mount once a joinly release has the fix, and move to that release. The file's sha256 is `9bf581c20a9a7911afb9f2ac1447a5d0800cdca24485dd215099d5d82afad6ff`; check it after you download it (`sha256sum zoom.py`), because a mount makes it run inside the bot.

**First words of a call line.** joinly 0.5.4 starts each piece of speech partway into its first word. Its voice detection flags speech late (a median of 73 ms after it begins in a recorded call, and up to 378 ms) and keeps only 32 ms from before that, so the speech-to-text model gets the first word cut off. The room's agent names are its hotwords, and a clipped "Our" or "Hey" can come out as "Claude". A line then wakes Claude for nothing, or the name that opens a call-out is lost ([#620](https://github.com/amahpour/switchboard/issues/620)). The fix keeps 0.3 s of the audio from before each utterance and passes it on with it. It adds no waiting, since that audio has already arrived. The fix is [joinly-ai/joinly#188](https://github.com/joinly-ai/joinly/pull/188), a change to joinly's transcription controller; until a joinly release has it, the file comes from [our fork](https://github.com/amahpour/joinly/commit/1fe384d9deba32f38fabd5bfc77fc5975adef6a7). On the recorded call of [#617](https://github.com/amahpour/switchboard/issues/617) (24 lines, 17 of them call-outs, `base` on four CPUs, joinly's own code run over the recording), it took the right agent from 14.8 of 17 call-outs to 16, the false wakes from 1 to 0, and the words wrong from 8.8% to 3.5%, for about 0.01 s more delay. The broker also asks joinly for longer pieces of speech (0.35 s of quiet and 1 s of audio before a piece is cut, arguments every joinly 0.5.4 has), and with both every call-out woke the right agent, for about 0.14 s more delay in all. Its file differs from 0.5.4's by the fix alone, so you can mount it over the image's copy, read-only, in the same `docker run` as the Zoom file (use either or both):

```bash
curl -fsSLo transcription.py https://raw.githubusercontent.com/amahpour/joinly/1fe384d9deba32f38fabd5bfc77fc5975adef6a7/joinly/controllers/transcription/default.py
docker run -d --name joinly -p 127.0.0.1:8010:8000 \
  -v "$PWD/transcription.py:/app/.venv/lib/python3.12/site-packages/joinly/controllers/transcription/default.py:ro" \
  ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0
```

switchboard sends joinly nothing for it: the new `utterance_preroll_seconds` argument has the fix as its default (0 turns it off), and a setting that stock joinly doesn't know would break every call for anyone who hasn't mounted the file. So a joinly without the file works as before, and with it the fix is on. As with the Zoom fix, drop the mount once a joinly release has it, and move to that release. The file's sha256 is `6053ca7c6b100952c91e14f2552b82eaf92bcde6a809d627f040479dc6d39d97`.

### The speech model, and a GPU

joinly turns the call's speech into text with Whisper. Out of the box it runs `base`, a small model, on the CPU. That is quick, and it sometimes mishears a name. You can choose a bigger model, and run joinly on a GPU so that the bigger model keeps up with the call.

**What the numbers say** ([#617](https://github.com/amahpour/switchboard/issues/617)). Twenty-four lines were spoken into a real Zoom call and recorded as the bot heard them. Seventeen of them called an agent by name. The same recordings went through each model with joinly's own settings. The CPU runs were capped at 4 CPUs. The GPU was a GTX 1660 Super, an older card with 6 GB. A line is a few seconds of speech.

| Model | Where | Words wrong | Time to transcribe a line (median) |
|---|---|---|---|
| `base` (the default) | CPU | 4.3% | 0.44 s |
| `distil-large-v3` | CPU | 4.3% | 4.8 s |
| `large-v3-turbo` | CPU | 2.4% | 4.5 s |
| `base` | GPU | 4.3% | 0.30 s |
| `distil-large-v3` | GPU | 4.8% | 1.3 s |
| `large-v3-turbo` | GPU | 2.4% | 1.3 s |

- **`large-v3-turbo` made the fewest word errors:** 2.4%, against 4.3% for `base`.
- **On a CPU the large models are too slow for a live call.** They ran at 0.83 to 0.90 of real time, so a backlog builds as soon as two people talk. They need a GPU.
- **`distil-large-v3`, which joinly picks by itself on a GPU, was no better than `base`** on these lines. If you run a GPU, name `large-v3-turbo`.
- **Every model woke the right agent for every call-out in the recordings.** In a live call, `base` missed two of the seventeen and woke an agent once for nothing. Those came from how joinly cuts speech into pieces before it transcribes, not from the model, so a bigger model is not a cure for them.
- **Take it as a guide.** The voices were synthetic, clean and one at a time. Real people with accents, noise and crosstalk will differ. One older card was measured: turbo took about 1.3 s a line on it, three times what `base` takes on a CPU. A newer card should be faster, and nobody has measured one. The delay in a live call on a GPU was not measured either.

**Choose a model.** The default stays `base` on a CPU. To use another, set it in the broker's `config.toml`, then restart the broker:

```toml
[meeting]
joinly_url = "http://127.0.0.1:8010/mcp/"
stt_model = "large-v3-turbo"
```

`stt_model` is one of `base`, `small`, `distil-large-v3` and `large-v3-turbo`. Unset or empty, it leaves the choice to joinly: `base` on a CPU, `distil-large-v3` on a GPU. Anything else stops the broker at start, and the message names the four. The environment variable `SWITCHBOARD_MEETING_STT_MODEL` sets the same thing where there is no file to edit; when both are set, the variable wins, and a bad value stops the broker the same way. It is sent to joinly each time a call starts, so a change needs a broker restart and a new call. Only the file or the variable sets it: a message in a room never can. The list is short on purpose. joinly's Whisper library would also take the name of any repository on Hugging Face, or a path, and switchboard does not leave what gets downloaded to a typo.

**The model is downloaded on the first call.** With no model named, joinly loads the one inside its image and does not look for it on the network. Once a model is named, joinly 0.5.4 turns that off (`local_files_only` is false in its `whisper.py`) and fetches the model from Hugging Face. `large-v3-turbo` is about 1.6 GB. joinly loads the model when a call's session opens, before the bot joins, so the first call waits for the download. Fetch it first, and keep it:

- **The cache is `/home/app/.cache/huggingface` in the container.** Put a volume there, so that the download survives a new container. A folder you mount yourself has to belong to uid 1001, the user joinly runs as; a volume Docker makes does.
- **Fetch it ahead** with joinly's own library. This only downloads: it does not load the model, so it needs no GPU and little memory.

  ```bash
  docker run --rm -v joinly-models:/home/app/.cache/huggingface \
    --entrypoint /app/.venv/bin/python ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0 \
    -c "from faster_whisper.utils import download_model; print(download_model('large-v3-turbo'))"
  ```

- **Then start joinly with the same volume**, and the first call finds the model there:

  ```bash
  docker run -d --name joinly -p 127.0.0.1:8010:8000 \
    -v joinly-models:/home/app/.cache/huggingface \
    ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0
  ```

- **Mount a cache only for a model you name.** A volume at that path takes the place of the image's own copy of its model, and with no model named joinly uses local files only.
- **Naming `base` goes to the network too.** What counts is that a name is given, not which one. With `stt_model = "base"`, joinly looks at Hugging Face for the model on every call, because it loads the model for each call. With the files already in the image, nothing is downloaded, and if Hugging Face can't be reached it uses the copy it has (checked with the network off). To stop the look once the model is in place, set `HF_HUB_OFFLINE=1` on joinly.

**Memory.** Every size on this page was measured with `base`. joinly loads a model for each call, so each live call holds its own copy. In the proof of concept a process holding `large-v3-turbo` peaked at about 1.7 GB, against 0.5 GB for `base`. A live call with a big model was not measured, so give joinly more than the tables say, and watch the first call. On a GPU the weights sit in the card's memory, one copy for each live call, and that was not measured at all.

**A GPU.** joinly publishes a build for NVIDIA GPUs, `ghcr.io/joinly-ai/joinly:v0.5.4-cuda@sha256:cc79e10311f95252d7684c4969c4467ea581c78de9b331096ac27a4a5a98532e`. It is about 6 GB to pull, and for `amd64` only. It sets `JOINLY_DEVICE=cuda` itself, so nothing else is needed, and its own model is `distil-large-v3`, so name `large-v3-turbo`. The host needs an NVIDIA GPU, a driver recent enough for CUDA 12.6 (which the image is built on), and the NVIDIA Container Toolkit.

```bash
docker run -d --name joinly --gpus 1 -p 127.0.0.1:8010:8000 \
  -v joinly-gpu-models:/home/app/.cache/huggingface \
  ghcr.io/joinly-ai/joinly:v0.5.4-cuda@sha256:cc79e10311f95252d7684c4969c4467ea581c78de9b331096ac27a4a5a98532e
```

Fetch the model first as above, with the `-cuda` image and this volume (that needs no `--gpus`). Give this image a volume of its own: it holds a different model than the CPU one.

**With the Compose example,** the `meetings-gpu` profile is the same bot on a GPU. It is a second service, `joinly-gpu`, so its address is different, and you turn on one profile, not both. It reserves one NVIDIA GPU (`deploy.resources.reservations.devices`) and publishes no port, like the CPU bot. In the `.env` file:

```bash
COMPOSE_PROFILES=meetings-gpu
SWITCHBOARD_JOINLY_URL=http://joinly-gpu:8000/mcp/
SWITCHBOARD_MEETING_STT_MODEL=large-v3-turbo
```

To keep the model between containers, or for the Zoom fix, uncomment the `volumes` lines under `joinly-gpu`. [Kubernetes](#meetings-on-kubernetes) and [Helm](#meetings-with-helm) have their own way.

### Meetings on Kubernetes

[deploy/kubernetes/meetings/joinly.yaml](https://github.com/amahpour/switchboard/blob/main/deploy/kubernetes/meetings/joinly.yaml) adds joinly as a second container in the broker's pod. It is a patch of the StatefulSet, in a folder of its own, so that [the manifest above](#kubernetes-eks-gke-aks) stays as it is for anyone who doesn't follow calls, and so that `kubectl apply -f deploy/kubernetes/` never takes it for a second StatefulSet. Apply it once, after the manifest (with [the Helm chart](#meetings-with-helm), `meetings.enabled=true` does all of this, and sizes joinly by the number of calls):

```bash
kubectl -n switchboard patch statefulset switchboard --patch-file deploy/kubernetes/meetings/joinly.yaml
kubectl -n switchboard get pod switchboard-0    # 2/2 when both containers are up
```

The patch replaces the pod, so the broker restarts with it: do it when no call is on. The first start pulls a 3.7 GB image, so the pod waits in `ContainerCreating` for a few minutes.

A patch made by hand is reverted at the next sync of Argo CD or Flux, because the repository's manifest has no sidecar. Under either, use kustomize: a `kustomization.yaml` beside copies of the manifest and the patch.

```yaml
resources: [switchboard.yaml]
patches: [{path: joinly.yaml}]
```

What the patch does, and why:

- **joinly listens on the pod's localhost, and nothing else can reach it.** It has no sign-in and takes its settings from whoever connects. The containers of a pod share one network, so joinly's `127.0.0.1:8000` is the broker's too. There is no Service for it and no port is declared. The image listens on `0.0.0.0` by default, which would give every pod in the cluster its port at the pod's IP, with or without a Service, so the patch sets `JOINLY_SERVER_HOST=127.0.0.1`. No NetworkPolicy is needed (EKS enforces one only when its network policy agent is on). The broker is told where joinly is by `SWITCHBOARD_JOINLY_URL=http://127.0.0.1:8000/mcp/`.
- **Resources: requests of 1 CPU and 2 GiB, and a 4 GiB memory limit, which fits one call at a time.** That is a starting point for the default Whisper model (`base`) and the broker's default, `[meeting] max_calls = 1`. Measured: about 350 MiB with no call, 1.4 GiB once a call's session is open with the browser on its page, and about 2.7 GiB and one CPU for each call that is live, more while people talk (two real calls at once took 5.5 GiB). Each call opens its own session with its own browser, so the limit has to be `memory ≈ 2.7Gi × max_calls + 0.5Gi`: `4Gi` for one call, about `6Gi` for two, `9Gi` for three. The patch leaves `max_calls` at its default of 1, so a second room's call is refused and the limit is never reached. To follow more calls, raise both together in a second patch, which `kubectl patch` or your kustomization applies after `joinly.yaml`: the variable on the broker's container, and the memory and CPU on joinly's. Raising the variable alone would let a second call run joinly out of memory, and every call on it would end. For three calls:

  ```yaml
  apiVersion: apps/v1
  kind: StatefulSet
  metadata:
    name: switchboard
  spec:
    template:
      spec:
        containers:
          - name: switchboard
            env:
              - name: SWITCHBOARD_MEETING_MAX_CALLS
                value: "3"            # a string, as every environment value is
          - name: joinly
            resources:
              requests: {cpu: "3", memory: 8Gi}
              limits: {memory: 9Gi}   # 2.7Gi × 3 + 0.5Gi, rounded up
  ```

  The node has to have the room: the pod's requests are what the scheduler reserves. There is no CPU limit, because throttling an audio pipeline makes the transcript fall behind.
- **`/dev/shm` is a 1 GiB memory-backed volume.** Chromium is known to crash in a container's default 64 MB. joinly starts it with `--disable-dev-shm-usage`, which keeps that memory in `/tmp` instead, and a join attempt ran with a 1 MB `/dev/shm`, so joinly 0.5.4 may not need the volume. It is cheap insurance for a long call with video tiles, and counts against the memory limit only when used. `/tmp` (2 GiB) is a volume too, since the root filesystem is read-only.
- **joinly runs as the image's own user, uid 1001, not the broker's 10001.** Its home is mode 0700 and holds the speech models (Whisper, Silero, Kokoro). As 10001, joinly starts, and every call then fails with `Silero VAD cache directory /.cache/silero does not exist`. Everything else is as tight as the broker's: a read-only root, no capabilities, no privilege escalation, the runtime's default seccomp profile. Chromium runs with `--no-sandbox`, which joinly passes itself, so it needs no capabilities. `/tmp` is the one place it writes, found by taking each candidate away in turn. PulseAudio would also write in the user's home, so the patch sets `XDG_CONFIG_HOME=/tmp`; without a writable place for it, joinly logs `PulseAudio server did not start in time` and no session opens.
- **The probes run inside the container.** joinly answers `GET /health` with `{"status":"healthy"}`, but it listens on localhost, and a kubelet's HTTP and TCP probes go to the pod's IP. So each probe is one line of the image's own Python. A startup probe keeps the pod from being Ready until joinly has answered once. There is no readiness probe, on purpose: a pod is Ready only when every container is, so a joinly that failed one would take the broker out of its Service, and every browser and agent would get a 503. The liveness probe restarts only joinly, after three minutes of silence. A restart ends the call in progress.
- **The node needs a way out to the internet.** joinly's browser reaches Zoom, Meet and Teams, and nothing needs to reach it. On EKS in private subnets that is a NAT gateway. Check the vendors' network requirements if your cluster restricts outbound traffic: a policy that lets the page load can still block the call's audio.

**Speaker names on Zoom.** The Zoom fix described above (joinly-ai/joinly#187) is a second patch, [joinly-zoom-fix.yaml](https://github.com/amahpour/switchboard/blob/main/deploy/kubernetes/meetings/joinly-zoom-fix.yaml), because the pod can't start without its ConfigMap. (`optional: true` doesn't help: the file is mounted with `subPath`, and when the ConfigMap is missing the kubelet makes that path a directory, so the container fails with `not a directory`.) Make the ConfigMap from the same pinned file, then patch:

```bash
curl -fsSLo zoom.py https://raw.githubusercontent.com/amahpour/joinly/9fbd07ceccbbfc358d697378c3cb1d7b911b17a5/joinly/providers/browser/platforms/zoom.py
kubectl -n switchboard create configmap joinly-zoom-fix --from-file=zoom.py=./zoom.py
kubectl -n switchboard patch statefulset switchboard --patch-file deploy/kubernetes/meetings/joinly-zoom-fix.yaml
```

With kustomize, add the patch and a generator for the file; kustomize adds a hash to the ConfigMap's name and rewrites the volume to match:

```yaml
patches: [{path: joinly.yaml}, {path: joinly-zoom-fix.yaml}]
configMapGenerator:
  - name: joinly-zoom-fix
    files: [zoom.py]
```

Drop the patch once a joinly release has the fix. To take the whole sidecar out, apply the manifest again without the patch (`kubectl patch` can't remove a container it added).

**First words of a call line.** The fix for the clipped first word ([joinly-ai/joinly#188](https://github.com/joinly-ai/joinly/pull/188); [#620](https://github.com/amahpour/switchboard/issues/620), described [above](#a-meeting-bot)) is a third patch, [joinly-preroll-fix.yaml](https://github.com/amahpour/switchboard/blob/main/deploy/kubernetes/meetings/joinly-preroll-fix.yaml), for the same reason as the Zoom one: the pod can't start without its ConfigMap. It is independent of the Zoom fix, and needs no setting. Make the ConfigMap from the pinned file, then patch:

```bash
curl -fsSLo transcription.py https://raw.githubusercontent.com/amahpour/joinly/1fe384d9deba32f38fabd5bfc77fc5975adef6a7/joinly/controllers/transcription/default.py
kubectl -n switchboard create configmap joinly-preroll-fix --from-file=transcription.py=./transcription.py
kubectl -n switchboard patch statefulset switchboard --patch-file deploy/kubernetes/meetings/joinly-preroll-fix.yaml
```

With kustomize, add its patch and its generator beside the Zoom ones:

```yaml
patches: [{path: joinly.yaml}, {path: joinly-zoom-fix.yaml}, {path: joinly-preroll-fix.yaml}]
configMapGenerator:
  - name: joinly-zoom-fix
    files: [zoom.py]
  - name: joinly-preroll-fix
    files: [transcription.py]
```

Drop this patch once a joinly release has the fix too, and move the image to that release.

**A GPU and a better model.** The patch is the CPU bot with `base`, and asks for no GPU, so that it applies on a cluster that has none. For [a GPU and `large-v3-turbo`](#the-speech-model-and-a-gpu), apply a second patch after it. The same lines are in a comment at the top of `joinly.yaml`:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: switchboard
spec:
  template:
    spec:
      nodeSelector: {nvidia.com/gpu.present: "true"}   # your GPU nodes' label
      tolerations:                                     # and their taint, if they have one
        - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
      containers:
        - name: switchboard
          env:
            - name: SWITCHBOARD_MEETING_STT_MODEL
              value: large-v3-turbo
        - name: joinly
          image: ghcr.io/joinly-ai/joinly:v0.5.4-cuda@sha256:cc79e10311f95252d7684c4969c4467ea581c78de9b331096ac27a4a5a98532e   # the same release, built for CUDA
          resources:
            limits:
              nvidia.com/gpu: 1
          volumeMounts:
            - {name: joinly-models, mountPath: /home/app/.cache/huggingface}
      volumes:
        - name: joinly-models
          emptyDir: {sizeLimit: 4Gi}
```

- **The cluster needs the NVIDIA device plugin** (or the GPU Operator), so that a node offers `nvidia.com/gpu`. Without one the pod stays `Pending`. An extended resource like this is asked for in the limits only, and the scheduler takes the request from it. The broker shares the pod, so the pod goes where the GPU is: the label and the taint are your nodes', and the patch writes them as you give them. The image is about 6 GB, so the first pull on a GPU node takes minutes.
- **The model gets a folder to be downloaded into.** The root is read-only, and joinly's cache is in the image user's home, so a model you name has nowhere to go without it. The folder is an `emptyDir`: it is gone when the pod is replaced, and the next pod downloads the model again, with the first call waiting for it. Leave the volume out, and the variable too, for the model inside the image, which needs no download. A cache that outlives the pod is not in the patch: a StatefulSet's claims are fixed once it exists ([Helm's upgrade notes](#helm) say how that bites), and the broker's volume is the broker's.
- **The memory and CPU are the ones above.** The sizes were measured with `base`. A bigger model adds to each call ([Memory](#the-speech-model-and-a-gpu)), so raise the limit.
- **Checked:** the YAML above was applied after `joinly.yaml` with `kubectl patch --local` and gave the CUDA image, the GPU limit, the unchanged 4 GiB memory limit, the broker's variable and the cache folder. It has not run on a GPU node.

**Check that it works.**

```bash
kubectl -n switchboard logs switchboard-0 -c joinly    # "Uvicorn running on http://127.0.0.1:8000"
kubectl -n switchboard exec switchboard-0 -c switchboard -- \
  python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health").read())'
kubectl -n switchboard exec switchboard-0 -c joinly -- \
  grep -c single-main-container__video-frame /app/.venv/lib/python3.12/site-packages/joinly/providers/browser/platforms/zoom.py    # 1 with the Zoom fix
kubectl -n switchboard exec switchboard-0 -c joinly -- \
  grep -c utterance_preroll_seconds /app/.venv/lib/python3.12/site-packages/joinly/controllers/transcription/default.py    # more than 0 with the first-word fix
```

The second command runs in the broker's container and reaches joinly on the pod's localhost. Then, in a room, `/meeting <a test meeting's link>`: the room says it is joining. `/meeting` with no link only says whether a call is live, so it doesn't tell you the broker found joinly. To see that nothing else can reach joinly, run a throwaway pod and fetch the broker pod's IP on port 8000, which must be refused:

```bash
kubectl -n switchboard run probe --image=busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec --restart=Never --rm -it -- \
  wget -T3 -qO- http://<pod-ip>:8000/health    # "Connection refused"; the same on :7419/healthz answers
```

### Meetings on your own machine

`/meeting <link> mine` in a room uses the person's own agent and host script. The link waits in broker memory for at most ten minutes and is handed once to that person's authenticated script through registration; no model sees it. It needs no server-side joinly. See [the private room handoff](guide/meetings-reference.md#ask-from-the-room-without-showing-the-link-to-a-model).

A person can run the bot on their own machine instead of beside the broker, for one call in one room ([DESIGN.md §54](DESIGN.md#54-hosted-calls-a-persons-machine-pushes-the-call-in-623), [the guide](guide/meetings-reference.md#host-the-bot-on-your-own-machine)). The broker needs nothing for it: no joinly, no `joinly_url`, and none of the bot's memory. The plugin's script starts two containers from [`plugins/claude/meeting/compose.yaml`](https://github.com/amahpour/switchboard/blob/main/plugins/claude/meeting/compose.yaml), one Compose project for each call, and you don't run that file by hand. Codex ships the same file at `plugins/codex/meeting/compose.yaml` and the same host scripts; its shell uses an agent token or its own session-bound key after [browser setup](guide/meetings-reference.md#codex-with-browser-sign-in). Both bots use the broker's `[meeting] tts_voice`:

```bash
sh plugins/claude/bin/meeting-host start --server https://sb.example.com '#uploads' < link.txt
```

What the file starts, and why it is built this way:

- **The relay** is `switchboard meeting relay`, in [switchboard's own image](#the-image) (about 330 MB, the release the plugin is: `.github/scripts/release.py` moves the tag). It reads the call's key and the meeting link from files in the call's folder, mounted at `/call`, never from its arguments or environment. It runs as the person who started it, with a read-only root, no capability, and `no-new-privileges`. It reaches the server over HTTPS (a plain `http` address is refused unless `SWITCHBOARD_RELAY_ALLOW_HTTP=1`), trusting what the container's trust store trusts.
- **joinly** is the same bot as [above](#a-meeting-bot), with the same two fixes mounted over its copies (below). It has no sign-in, so it has **no address of its own**: it shares the relay's network (`network_mode: service:relay`) and listens on that network's `127.0.0.1` (`JOINLY_SERVER_HOST`), as the broker's pod does for its own ([Meetings on Kubernetes](#meetings-on-kubernetes)). No port is published, and nothing else on the machine or the network can talk to it. It runs with the same restrictions as the Kubernetes patch gives it: a read-only root with `/tmp` writable, no capability, and `no-new-privileges`.
- **joinly ends with the relay.** It also shares the relay's process namespace (`pid: service:relay`), so when the relay exits (End call, a revoked sign-in, `meeting-host stop`) the kernel ends every process in that namespace and joinly's container stops with it. Nothing is left running and nothing has to watch for it. The exited containers stay until the script removes them, at the next `start` or at `stop`.
- **GPU or CPU is a Compose profile.** `gpu` runs `ghcr.io/joinly-ai/joinly:v0.5.4-cuda@sha256:cc79e10311f95252d7684c4969c4467ea581c78de9b331096ac27a4a5a98532e` (about 6 GB to pull) with one NVIDIA device reserved (`deploy.resources.reservations.devices`) and a named volume, `switchboard-joinly-models-cuda`, that keeps the speech model (about 1.6 GB for `large-v3-turbo`) between calls, so only the first GPU call waits for the download. `cpu` runs `ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0` with joinly's own `base`, which is in the image, so nothing is downloaded and no volume is mounted (a volume at that path would hide the model the image holds). The script picks the profile: `nvidia-smi` sees a GPU, Docker has an `nvidia` runtime (the NVIDIA Container Toolkit), and the machine is not a Mac. It pulls nothing to find out, says which it chose and why, and `--gpu` or `--cpu` forces either. The GPU image needs the driver for CUDA 12.6 or newer.
- **The two fixes are pinned by sha256.** `plugins/claude/meeting/fixes.pins` names each fix, its address (a commit, so it doesn't move) and its sha256, the same files as [above](#a-meeting-bot). The script keeps them in the plugin's data folder, checks both against the pins every time, fetches a missing one from its address, and refuses (and deletes) one whose hash differs, so a changed file never reaches joinly. The addresses are `raw.githubusercontent.com`, which the machine must reach the first time. Drop a line, and its mount, once a joinly release has the fix.
- **Images.** A missing image is pulled before the script tells the server anything, so a call registered in the room is ready to start. To try a build of your own, `SWITCHBOARD_RELAY_IMAGE=my-switchboard:dev` (`docker build -f deploy/image/Dockerfile .`) names the relay's image, and nothing else changes.
- **A server the containers can't resolve.** `--add-host NAME:IP` (or `host-gateway`) writes a second Compose file with `extra_hosts` for the relay, for a server whose name only this machine knows.

The key and the link are files in `calls/<fingerprint>/` of the plugin's data folder, mode 0600 in a folder only the person can enter. The relay deletes the link once it has joined, and the key when it exits. On Docker Desktop (a Mac, or Windows) a bind-mounted file shows the host's mode differently, which is why the relay is told to trust the files the script made (`--no-mode-check`); the folder is the person's, and the relay runs as them.

## Health checks and logs

- **`GET /healthz`** answers `ok` with status 200. It needs no sign-in, returns no data, and accepts any Host, so platforms can probe the container's own address. The image's Docker `HEALTHCHECK` calls it, and so do the Kubernetes probes and Render's `healthCheckPath`.
- **Logs** are on stdout: `docker logs switchboard`, `kubectl logs switchboard-0`, or Render's Logs tab. They hold ids, never message text. The admin's one-time password is the one secret ever printed there, and it works once and expires. The people's one-time passwords never appear there. Agent tokens never appear there.

## The image

- **Where:** `ghcr.io/amahpour/switchboard:<version>` and `:latest`, for linux/amd64 and linux/arm64. The release job builds it from the release's tag, with a provenance attestation and an SBOM. Inspect them with `docker buildx imagetools inspect ghcr.io/amahpour/switchboard:0.56.1 --format '{{json .Provenance}}'` (or `.SBOM`).
- **What's in it:** Python 3.13 (Debian slim) with switchboard installed from its wheel and the exact dependencies in `uv.lock`, plus tini. No uv, compiler, git, ssh or shell tools beyond the base image's.
- **Who it runs as:** the `switchboard` user (uid and gid 10001), always.
  - Started as root, as Render and plain `docker run` do, the image's `switchboard` command ([deploy/image/switchboard.sh](https://github.com/amahpour/switchboard/blob/main/deploy/image/switchboard.sh)) first makes `$SWITCHBOARD_HOME` that user's private directory. It then drops to that user with no capabilities and no way back through setuid.
  - `docker exec … switchboard …` drops to the same user.
  - Started as uid 10001, as Kubernetes does with the manifest above, it runs as that user from the start.
- **Build it yourself:** `docker build -t switchboard -f deploy/image/Dockerfile .` from the repository. CI builds it on every pull request and runs [tests/image](https://github.com/amahpour/switchboard/blob/main/tests/image/test_image.py) against it, behind a proxy that ends TLS (CONTRIBUTING.md, "The container image").
