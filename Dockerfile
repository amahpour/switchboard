# syntax=docker/dockerfile:1
# switchboard's broker as a container image (issue #34; docs/DEPLOY.md). The release job
# publishes it as ghcr.io/amahpour/switchboard:<version> and :latest, for linux/amd64 and
# linux/arm64; the `image` CI job builds it on every PR and runs tests/image against it.
#
#   docker build -t switchboard .
#   docker run -d --name switchboard -p 127.0.0.1:7419:7419 -v switchboard:/data \
#     -e SWITCHBOARD_PUBLIC_URL=http://switchboard.localhost:7419 switchboard
#   docker exec -it switchboard switchboard login
#
# Two stages. The first has uv build switchboard's wheel from this tree and install it, with
# the dependencies exactly as uv.lock pins them, into a venv (/opt/switchboard). The runtime is
# the same slim Python with that venv and tini, and nothing else: no uv, no compiler, no git,
# no ssh (a remote link needs one: DESIGN.md §30).
#
# The broker listens on 0.0.0.0:7419 in the container and needs SWITCHBOARD_PUBLIC_URL, the
# address browsers use; its data lives in $SWITCHBOARD_HOME, /data/switchboard on the /data
# volume. It runs as the unprivileged `switchboard` user (uid 10001) whichever user the
# container starts as (deploy/image/switchboard.sh).

# Both pinned by digest; bump them together with the tag in a PR of their own.
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.13@sha256:6c1e19020ec221986a210027040044a5df8de762eb36d5240e382bc41d7a9043

FROM ${UV_IMAGE} AS uv

# ------------------------------------------------------------------ build
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
# the image's own Python (never a download); bytecode compiled now, not at every start; files
# copied rather than linked to uv's cache, which isn't in the runtime image
ENV UV_PYTHON=/usr/local/bin/python3.13 UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/switchboard
WORKDIR /src
# the locked dependencies first: this layer is reused until uv.lock changes
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
# then switchboard itself: a wheel, installed non-editable
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
 && /opt/switchboard/bin/switchboard --version

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="switchboard" \
      org.opencontainers.image.description="A local group chat where you and your coding agents talk and hand work to each other: the broker and its web UI" \
      org.opencontainers.image.source="https://github.com/amahpour/switchboard" \
      org.opencontainers.image.licenses="MIT"
# tini: PID 1. The `switchboard` user owns the data: /data, the volume, and the home in it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 switchboard \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/switchboard --create-home \
      --shell /usr/sbin/nologin switchboard \
 && install -d -o switchboard -g switchboard -m 0700 /data
COPY --from=build /opt/switchboard /opt/switchboard
COPY --chmod=0755 deploy/image/switchboard.sh /usr/local/bin/switchboard
ENV SWITCHBOARD_HOME=/data/switchboard \
    SWITCHBOARD_LISTEN=0.0.0.0 \
    SWITCHBOARD_PORT=7419 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
WORKDIR /data
EXPOSE 7419
# for Docker and Compose (Kubernetes and Render probe /healthz themselves): the standard
# library only, straight to the broker's own port, never through a proxy from the environment
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c 'import os, urllib.request as u; u.build_opener(u.ProxyHandler({})).open("http://127.0.0.1:%s/healthz" % (os.environ.get("SWITCHBOARD_PORT") or "7419"), timeout=4)'
ENTRYPOINT ["/usr/local/bin/switchboard"]
CMD ["start", "--foreground", "--log-stdout"]
