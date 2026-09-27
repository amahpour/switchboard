# syntax=docker/dockerfile:1
# The switchboard sandbox box (docs/SANDBOX.md §3). Build context: sandbox/, so only
# the files COPYed below reach the image. switchboard itself is built from the named
# context switchboard-src (the repo; compose.yaml passes it) during the build, so the
# running box needs no host mount and an upgrade is a rebuild. Build with
# `docker compose build`, or: docker build --build-context switchboard-src=. sandbox
FROM debian:bookworm-slim

ARG TARGETARCH
ARG UV_VERSION=0.7.13
# sha256 of uv-<arch>-unknown-linux-gnu.tar.gz from the uv GitHub release
ARG UV_SHA256_ARM64=0b2ad9fe4295881615295add8cc5daa02549d29cc9a61f0578e397efcf12f08f
ARG UV_SHA256_AMD64=909278eb197c5ed0e9b5f16317d1255270d1f9ea4196e7179ce934d48c4c2545

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git tmux ripgrep jq sqlite3 procps lsof less bubblewrap socat \
      iptables ipset dnsmasq-base \
 && rm -rf /var/lib/apt/lists/*

# uv from its release tarball, checksum-pinned (switchboard's pyproject pins uv ==0.7.13).
RUN set -eu; \
    case "$TARGETARCH" in \
      arm64) a=aarch64; s="$UV_SHA256_ARM64" ;; \
      amd64) a=x86_64;  s="$UV_SHA256_AMD64" ;; \
      *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/uv.tgz "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$a-unknown-linux-gnu.tar.gz"; \
    echo "$s  /tmp/uv.tgz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tgz -C /usr/local/bin --strip-components=1 "uv-$a-unknown-linux-gnu/uv" "uv-$a-unknown-linux-gnu/uvx"; \
    rm /tmp/uv.tgz; uv --version

# uv, Python and switchboard live in root-owned /opt and /usr/local/bin, so agents
# (user dev) can't edit the broker or the UI your browser loads.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_TOOL_DIR=/opt/uv/tools UV_PYTHON_PREFERENCE=only-managed \
    UV_TOOL_BIN_DIR=/usr/local/bin UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
# The stdlib is byte-compiled here: it is root-owned, so dev can't write its .pyc
# files, and every hook run would recompile what it imports (about 40 ms).
RUN uv python install 3.13 \
 && P="$(uv python find 3.13)" \
 && "$P" -m compileall -q -j0 -x '/(test|tests|idlelib|lib2to3)/' "$("$P" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
 && useradd -m -s /bin/bash -u 1000 dev \
 && mkdir -p /var/lib/switchboard /work && chown dev:dev /var/lib/switchboard /work && chmod 0700 /var/lib/switchboard
# switchboard, with its dependencies pinned to uv.lock; the source is mounted for
# this step only and never lands in the image.
RUN --mount=type=bind,from=switchboard-src,target=/src/switchboard,rw \
    uv export --project /src/switchboard --frozen --no-dev --no-emit-project --no-hashes -o /tmp/switchboard-pins.txt \
 && uv tool install --constraints /tmp/switchboard-pins.txt /src/switchboard \
 && rm /tmp/switchboard-pins.txt && switchboard --version
# PATH stays root-owned directories only: the image-wide PATH is root's too
# (the entrypoint, every `docker compose exec`, which defaults to root), and
# /home/dev is the dev-writable switchboard-home volume, so a dev-writable directory
# on it would let any agent plant `iptables` or `sleep` for root to run. dev's
# login shells (`bash -l`) add ~/.local/bin, where the CLIs install, through
# /etc/profile.d below (and Debian's ~/.profile).
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 SWITCHBOARD_FIREWALL=1 \
    DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 ENABLE_CLAUDEAI_MCP_SERVERS=false \
    CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1 \
    SWITCHBOARD_HOME=/var/lib/switchboard SWITCHBOARD_PORT=8765
RUN printf '%s\n' '# switchboard box: the agent CLIs live in ~/.local/bin (never on root'"'"'s PATH)' \
      'if [ "$(id -u)" != 0 ]; then case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac; fi' \
      > /etc/profile.d/switchboard-dev-path.sh
COPY --chmod=0755 firewall.sh install-agents.sh forward.sh /opt/sandbox/
COPY --chmod=0644 allowlist.txt /opt/sandbox/
# Marks the box: install-agents.sh refuses to run anywhere else (e.g. on the Mac).
RUN touch /opt/sandbox/.switchboard-box
# Root entrypoint: applies the firewall (fails closed), then idles. Agents never
# run as root. Absolute paths and a pinned PATH: nothing dev can write is run.
ENTRYPOINT ["/bin/bash", "-c", "export PATH=/usr/sbin:/usr/bin:/sbin:/bin; [ \"$SWITCHBOARD_FIREWALL\" = 0 ] || /opt/sandbox/firewall.sh && exec /bin/sleep infinity"]
