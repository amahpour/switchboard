"""config.toml loading (DESIGN.md §2). Every key is optional."""

from __future__ import annotations

import dataclasses
import getpass
import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from switchboard.models import RESERVED_NAMES
from switchboard.paths import Paths

SCREEN_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
# a Kokoro voice name, as joinly takes it (``bm_george``): a short word, nothing else reaches joinly
VOICE_RE = re.compile(r"[a-z0-9_]{1,32}")
# Agents may not take a screen name that starts with the human's name, so a login
# name that prefixes a harness's usual names (claude-1, dev -> devin-1) is not used.
_AGENT_PREFIXES = ("claude", "codex", "cursor", "devin")
FALLBACK_HUMAN_NAME = "me"


def default_human_name() -> str:
    """The login name, lowercased, if it is a valid human screen name; else ``me``."""
    try:
        name = getpass.getuser().lower()
    except Exception:  # no login name for this uid (OSError; KeyError before 3.13)
        return FALLBACK_HUMAN_NAME
    if (
        not SCREEN_NAME_RE.match(name)
        or name in RESERVED_NAMES
        or name.startswith("switchboard")
        or any(p.startswith(name) for p in _AGENT_PREFIXES)
    ):
        return FALLBACK_HUMAN_NAME
    return name


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryCfg:
    quiet_s: float = 3.0
    max_hold_s: float = 60.0
    # chatter that doesn't address an idle agent waits this long before it wakes it (#137,
    # DESIGN.md §8.2): just inside a 5-minute prompt cache. 0: the quiet period, as before 0.29
    unaddressed_hold_s: float = 270.0
    batch_max_msgs: int = 20
    batch_max_chars: int = 6000
    pull_max_chars: int = 24000  # read()/say() unread answers (tool results), rendered
    rate_limit_s: float = 10.0
    budget_per_hour: int = 60
    # the loop guard: pause a room after this many agent messages in a row with none from a
    # person. 0 = off, the default (#406): the wake budget still bounds runaway chatter, and a
    # person's own default in Settings, /hops <n> or a room's wake dialog turns the guard on.
    # Rooms that exist keep the limit they have: a room's limit is that room's setting (§8.5, §42)
    hop_limit: int = 0
    watchdog_s: float = 120.0
    watchdog_max: int = 2
    catchup_n: int = 8
    max_msg_chars: int = 4000
    offer_backstop_s: float = 1800.0
    hook_ack_s: float = 5.0
    pull_ack_s: float = 10.0


# wait() caps, measured with each harness's CLI in November 2026 (#547, DESIGN.md §6.1): Claude Code
# uses the plugin timeout and background-result confirmation for a 600 s wait (#558); Codex
# ends a call at its default tool_timeout_sec of 300 s; Cursor at 60 s; Devin at 120 s (§49.7).
@dataclass(frozen=True)
class ClaudeCfg:
    wait_cap_s: int = 600


@dataclass(frozen=True)
class CodexCfg:
    wait_cap_s: int = 280
    ctx_max_chars: int = 5000


@dataclass(frozen=True)
class CursorCfg:
    wait_cap_s: int = 55
    ctx_max_chars: int = 8000


@dataclass(frozen=True)
class OpenCodeCfg:
    wait_cap_s: int = 50


@dataclass(frozen=True)
class DevinCfg:
    wait_cap_s: int = 600
    rearm: bool = True
    rearm_max_per_prompt: int = 2
    rearm_max_per_hour: int = 12  # per session: re-arms spend the room's shared wake budget
    ctx_max_chars: int = 6000


@dataclass(frozen=True)
class ReviewCfg:
    # Ignored since /catchup (DESIGN.md §26), which never looks for agentsview: still accepted
    # so a 0.2.0 config.toml loads; the broker logs a warning once at start when it is set.
    agentsview: str = ""


# Keys that only the local agent path read, retired in 0.38.0 (#385): still accepted, so a config.toml of
# an earlier release loads, and ignored. The broker logs one warning at start naming those it found, as
# it does for ``[review] agentsview``.
RETIRED_KEYS: dict[str, frozenset[str]] = {
    "claude": frozenset({"sessions_dir", "inbox_hold_s", "inbox_idle_expire_s"}),
    "codex": frozenset(
        {"control_socket", "home", "bin", "queue_fallback", "require_thread_proof", "restart_grace_s"}
    ),
    "cursor": frozenset({"stop_park_s", "max_unconfirmed_followups"}),
}


@dataclass(frozen=True)
class FilesCfg:
    """How large a file may be, and how much one room may hold (DESIGN.md §53).

    Retention is not here: an admin sets it, and the proxy's body limit has to match ``max_mb``,
    which a page setting cannot change.
    """

    max_mb: int = 25
    room_mb: int = 500

    def __post_init__(self) -> None:
        if isinstance(self.max_mb, bool) or not isinstance(self.max_mb, int) or not 1 <= self.max_mb <= 1024:
            raise ConfigError("[files] max_mb must be an integer from 1 to 1024")
        if (
            isinstance(self.room_mb, bool)
            or not isinstance(self.room_mb, int)
            or not 1 <= self.room_mb <= 1_048_576
        ):
            raise ConfigError("[files] room_mb must be an integer from 1 to 1048576")


def _joinly_url_ok(url: str) -> bool:
    """An http(s) URL of at most 300 characters with a host and no user name or password in it.
    The key in config.toml and ``SWITCHBOARD_JOINLY_URL`` both go through this."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:  # an unbalanced "[" in the host, say
        return False
    return (
        len(url) <= 300
        and parts.scheme in ("http", "https")
        and bool(parts.netloc)
        and not parts.username
        and not parts.password
    )


@dataclass(frozen=True)
class MeetingCfg:
    """Where joinly listens, and the voice it speaks with (DESIGN.md §52). Empty ``joinly_url``:
    ``/meeting`` says how to set it. Both are the operator's, from this file (the URL also from
    ``SWITCHBOARD_JOINLY_URL``), never from a room.

    ``tts_voice`` is a Kokoro voice name (``bm_george``, ``af_bella``): a short word of
    lowercase letters, digits and underscores, passed to joinly as it connects."""

    joinly_url: str = ""
    tts_voice: str = "bm_george"

    def __post_init__(self) -> None:
        if not VOICE_RE.fullmatch(self.tts_voice):
            raise ConfigError("[meeting] tts_voice must be a short word of a-z, 0-9 and _, like bm_george")
        url = self.joinly_url.strip()
        if url != self.joinly_url:
            object.__setattr__(self, "joinly_url", url)
        if url and not _joinly_url_ok(url):
            raise ConfigError("[meeting] joinly_url must be an http(s) URL")


@dataclass(frozen=True)
class SecurityCfg:
    # DESIGN.md §27.5.7: human commands (switchboard say/cmd/login ...) from a process under a
    # remote login on this machine (sshd, dropbear, mosh-server, ...). Off: refused.
    allow_ssh_cli: bool = False


@dataclass(frozen=True)
class Config:
    human_name: str = field(default_factory=default_human_name)
    # SWITCHBOARD_HUMAN_NAME set it: then it wins over a rename in Settings too (#114, §41)
    human_name_from_env: bool = False
    port: int = 7419
    # the web UI's listen address and public URL (DESIGN.md §30; `start --listen/--public-url`):
    # loopback and http://switchboard.localhost:<port> unless both are set
    listen: str = "127.0.0.1"
    public_url: str = ""
    delivery: DeliveryCfg = field(default_factory=DeliveryCfg)
    claude: ClaudeCfg = field(default_factory=ClaudeCfg)
    codex: CodexCfg = field(default_factory=CodexCfg)
    cursor: CursorCfg = field(default_factory=CursorCfg)
    devin: DevinCfg = field(default_factory=DevinCfg)
    opencode: OpenCodeCfg = field(default_factory=OpenCodeCfg)
    review: ReviewCfg = field(default_factory=ReviewCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)
    meeting: MeetingCfg = field(default_factory=MeetingCfg)
    files: FilesCfg = field(default_factory=FilesCfg)
    # the retired keys the file had (``section.key``), ignored: reported once when the broker starts
    retired: tuple[str, ...] = ()

    def replace(self, **changes: Any) -> "Config":
        return dataclasses.replace(self, **changes)

    def with_delivery(self, **changes: Any) -> "Config":
        return dataclasses.replace(self, delivery=dataclasses.replace(self.delivery, **changes))


_SECTIONS = {
    "delivery": DeliveryCfg,
    "claude": ClaudeCfg,
    "codex": CodexCfg,
    "cursor": CursorCfg,
    "devin": DevinCfg,
    "opencode": OpenCodeCfg,
    "review": ReviewCfg,
    "security": SecurityCfg,
    "meeting": MeetingCfg,
    "files": FilesCfg,
}


def _coerce(cls: type, name: str, value: Any, where: str) -> Any:
    """Check ``value`` against the field's declared type (not its default's)."""
    fields = {f.name: f for f in dataclasses.fields(cls)}
    f = fields[name]
    kind = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.{name} must be true or false")
        return value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise ConfigError(f"{where}.{name} must be a number")
        if value < 0:
            raise ConfigError(f"{where}.{name} must be >= 0")
        return float(value)
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}.{name} must be an integer")
        if value < 0:
            raise ConfigError(f"{where}.{name} must be >= 0")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{name} must be a string")
        return value
    return value


def _section(cls: type, data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{where}] must be a table")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(unknown)}")
    return cls(**{k: _coerce(cls, k, v, where) for k, v in data.items()})


def from_dict(data: dict[str, Any]) -> Config:
    top = {k: v for k, v in data.items() if k not in _SECTIONS}
    unknown = sorted(set(top) - {"human_name", "port", "listen", "public_url"})
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    if "human_name" in top:
        name = top["human_name"]
        if not isinstance(name, str) or not SCREEN_NAME_RE.match(name):
            raise ConfigError("human_name must match ^[a-z][a-z0-9_-]{0,23}$")
        kwargs["human_name"] = name
    if "port" in top:
        port = top["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
            raise ConfigError("port must be an integer in 0..65535")
        kwargs["port"] = port
    for key in ("listen", "public_url"):  # checked when the broker starts (daemon.web_settings)
        if key in top:
            if not isinstance(top[key], str):
                raise ConfigError(f"{key} must be a string")
            kwargs[key] = top[key]
    retired: list[str] = []
    for key, cls in _SECTIONS.items():
        if key in data:
            table = data[key]
            gone = RETIRED_KEYS.get(key, frozenset())
            if isinstance(table, dict):
                retired += [f"{key}.{k}" for k in sorted(table) if k in gone]
                table = {k: v for k, v in table.items() if k not in gone}
            kwargs[key] = _section(cls, table, key)
    return Config(**kwargs, retired=tuple(retired))


# the human's name from the environment: a deployment's manifest names its admin (§32.4),
# where the container's own user (``switchboard``) would make it ``me``
HUMAN_NAME_ENV = "SWITCHBOARD_HUMAN_NAME"
# the meeting bot's address from the environment: a Kubernetes manifest (and Argo with it) points the
# broker at joinly beside it in the pod, where config.toml sits on the volume and a manifest can't
# write it (§52, docs/DEPLOY.md). As for the human's name, a non-empty value wins over the file.
JOINLY_URL_ENV = "SWITCHBOARD_JOINLY_URL"


def load(paths: Paths) -> Config:
    """Read ``$SWITCHBOARD_HOME/config.toml`` if it exists; defaults otherwise. A non-empty
    ``SWITCHBOARD_HUMAN_NAME`` sets ``human_name`` over both, and a non-empty
    ``SWITCHBOARD_JOINLY_URL`` sets ``[meeting] joinly_url`` over the file."""
    try:
        raw = paths.config.read_bytes()
    except FileNotFoundError:
        cfg = Config()
    else:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ConfigError(f"{paths.config}: {e}") from None
        cfg = from_dict(data)
    env = os.environ.get(HUMAN_NAME_ENV, "").strip()
    if env:
        if not SCREEN_NAME_RE.match(env):
            raise ConfigError(f"{HUMAN_NAME_ENV} must match ^[a-z][a-z0-9_-]{{0,23}}$")
        cfg = cfg.replace(human_name=env, human_name_from_env=True)
    env = os.environ.get(JOINLY_URL_ENV, "").strip()
    if env:
        if not _joinly_url_ok(env):  # never ignored: a typo here would turn /meeting off without a word
            raise ConfigError(
                f"{JOINLY_URL_ENV} must be an http(s) URL (300 characters at most, no user name or password)"
            )
        cfg = cfg.replace(meeting=dataclasses.replace(cfg.meeting, joinly_url=env))
    return cfg
