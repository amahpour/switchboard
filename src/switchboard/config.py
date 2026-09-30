"""config.toml loading (DESIGN.md §2). Every key is optional."""

from __future__ import annotations

import dataclasses
import getpass
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from switchboard.models import RESERVED_NAMES
from switchboard.paths import Paths

SCREEN_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
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
    if (not SCREEN_NAME_RE.match(name) or name in RESERVED_NAMES or name.startswith("switchboard")
            or any(p.startswith(name) for p in _AGENT_PREFIXES)):
        return FALLBACK_HUMAN_NAME
    return name


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryCfg:
    quiet_s: float = 3.0
    max_hold_s: float = 60.0
    batch_max_msgs: int = 20
    batch_max_chars: int = 6000
    pull_max_chars: int = 24000  # read()/say() unread answers (tool results), rendered
    rate_limit_s: float = 10.0
    budget_per_hour: int = 60
    hop_limit: int = 6
    watchdog_s: float = 120.0
    watchdog_max: int = 2
    catchup_n: int = 30
    max_msg_chars: int = 4000
    offer_backstop_s: float = 1800.0
    hook_ack_s: float = 5.0
    pull_ack_s: float = 10.0


@dataclass(frozen=True)
class ClaudeCfg:
    sessions_dir: str = "~/.claude/sessions"
    inbox_hold_s: float = 0.3
    inbox_idle_expire_s: float = 5.0
    wait_cap_s: int = 110


@dataclass(frozen=True)
class CodexCfg:
    control_socket: str = "~/.codex/app-server-control/app-server-control.sock"
    home: str = ""
    bin: str = "codex"
    queue_fallback: bool = True
    require_thread_proof: bool = True
    wait_cap_s: int = 240
    ctx_max_chars: int = 5000
    # a joined thread whose app-server died (e.g. the daemon auto-updated and restarted)
    # is re-bound if it shows up on the new app-server within this window; 0 = end at once
    restart_grace_s: float = 30.0


@dataclass(frozen=True)
class CursorCfg:
    stop_park_s: int = 600
    wait_cap_s: int = 50
    ctx_max_chars: int = 8000
    max_unconfirmed_followups: int = 2


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


@dataclass(frozen=True)
class SecurityCfg:
    # DESIGN.md §27.5.7: human commands (switchboard say/cmd/login ...) from a process under a
    # remote login on this machine (sshd, dropbear, mosh-server, ...). Off: refused. A relay peer
    # is refused either way.
    allow_ssh_cli: bool = False


@dataclass(frozen=True)
class Config:
    human_name: str = field(default_factory=default_human_name)
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
    review: ReviewCfg = field(default_factory=ReviewCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)

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
    "review": ReviewCfg,
    "security": SecurityCfg,
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
    for key, cls in _SECTIONS.items():
        if key in data:
            kwargs[key] = _section(cls, data[key], key)
    return Config(**kwargs)


# the human's name from the environment: a deployment's manifest names its admin (§32.4),
# where the container's own user (``switchboard``) would make it ``me``
HUMAN_NAME_ENV = "SWITCHBOARD_HUMAN_NAME"


def load(paths: Paths) -> Config:
    """Read ``$SWITCHBOARD_HOME/config.toml`` if it exists; defaults otherwise. A non-empty
    ``SWITCHBOARD_HUMAN_NAME`` sets ``human_name`` over both."""
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
        cfg = cfg.replace(human_name=env)
    return cfg
