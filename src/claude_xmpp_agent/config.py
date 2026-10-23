"""Load and validate the TOML configuration."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slixmpp import JID
from slixmpp.jid import InvalidJID

PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
SETTING_SOURCES = ("user", "project", "local")
VERBOSITIES = ("quiet", "normal", "verbose")


class ConfigError(ValueError):
    """The configuration file is missing, malformed or invalid."""


@dataclass(frozen=True)
class XmppConfig:
    jid: str
    password: str
    room: str
    nick: str = "claude"
    room_password: str | None = None


@dataclass(frozen=True)
class AccessConfig:
    allowed_jids: frozenset[str]


@dataclass(frozen=True)
class ClaudeConfig:
    cwd: Path
    model: str | None = None
    permission_mode: str = "default"
    allowed_tools: tuple[str, ...] = ()
    setting_sources: tuple[str, ...] = SETTING_SOURCES
    allow_bypass: bool = False


@dataclass(frozen=True)
class UiConfig:
    verbosity: str = "normal"
    max_chunk: int = 3000
    approval_timeout: float | None = None


@dataclass(frozen=True)
class Config:
    xmpp: XmppConfig
    access: AccessConfig
    claude: ClaudeConfig
    ui: UiConfig = field(default_factory=UiConfig)


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    """Read the TOML file at `path` and return a validated Config.

    `env` defaults to os.environ; it supplies the `XMPP_PASSWORD` fallback.
    A relative `claude.cwd` is resolved against the config file's directory.
    """
    path = Path(path)
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    return parse_config(raw, base_dir=path.parent, env=os.environ if env is None else env)


def parse_config(
    raw: Mapping[str, Any], base_dir: Path, env: Mapping[str, str]
) -> Config:
    _check_keys(raw, {"xmpp", "access", "claude", "ui"}, "top level")
    return Config(
        xmpp=_parse_xmpp(_section(raw, "xmpp"), env),
        access=_parse_access(_section(raw, "access")),
        claude=_parse_claude(_section(raw, "claude"), base_dir),
        ui=_parse_ui(_section(raw, "ui")),
    )


def normalize_jid(value: Any, what: str) -> str:
    """Return the bare, lower-cased form of a JID, or raise ConfigError."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{what}: expected a JID, got {value!r}")
    try:
        bare = JID(value.strip()).bare
    except InvalidJID as e:
        raise ConfigError(f"{what}: invalid JID {value!r}: {e}") from None
    if not bare:
        raise ConfigError(f"{what}: invalid JID {value!r}")
    return bare


# -- sections ---------------------------------------------------------------


def _parse_xmpp(sec: Mapping[str, Any], env: Mapping[str, str]) -> XmppConfig:
    _check_keys(sec, {"jid", "password", "room", "nick", "room_password"}, "[xmpp]")
    jid = _require_str(sec, "jid", "[xmpp]")
    try:
        JID(jid)
    except InvalidJID as e:
        raise ConfigError(f"[xmpp] jid: invalid JID {jid!r}: {e}") from None

    password = _opt_str(sec, "password", "[xmpp]") or env.get("XMPP_PASSWORD")
    if not password:
        raise ConfigError("[xmpp] password is not set and $XMPP_PASSWORD is empty")

    room = sec.get("room")
    if room is None or room == "":
        raise ConfigError("[xmpp] room must not be empty")
    room = normalize_jid(room, "[xmpp] room")
    if "@" not in room:
        raise ConfigError(f"[xmpp] room must be a room JID like room@conference.example.org, got {room!r}")

    nick = _opt_str(sec, "nick", "[xmpp]") or "claude"
    return XmppConfig(
        jid=jid,
        password=password,
        room=room,
        nick=nick,
        room_password=_opt_str(sec, "room_password", "[xmpp]") or None,
    )


def _parse_access(sec: Mapping[str, Any]) -> AccessConfig:
    _check_keys(sec, {"allowed_jids"}, "[access]")
    jids = sec.get("allowed_jids", [])
    if not isinstance(jids, list):
        raise ConfigError("[access] allowed_jids must be a list of JIDs")
    allowed = frozenset(normalize_jid(j, "[access] allowed_jids") for j in jids)
    if not allowed:
        raise ConfigError("[access] allowed_jids must not be empty")
    return AccessConfig(allowed_jids=allowed)


def _parse_claude(sec: Mapping[str, Any], base_dir: Path) -> ClaudeConfig:
    where = "[claude]"
    _check_keys(
        sec,
        {"cwd", "model", "permission_mode", "allowed_tools", "setting_sources", "allow_bypass"},
        where,
    )
    cwd = Path(_opt_str(sec, "cwd", where) or ".").expanduser()
    if not cwd.is_absolute():
        cwd = base_dir / cwd
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ConfigError(f"{where} cwd is not a directory: {cwd}")

    mode = _opt_str(sec, "permission_mode", where) or "default"
    if mode not in PERMISSION_MODES:
        raise ConfigError(f"{where} permission_mode must be one of {', '.join(PERMISSION_MODES)}; got {mode!r}")

    allow_bypass = sec.get("allow_bypass", False)
    if not isinstance(allow_bypass, bool):
        raise ConfigError(f"{where} allow_bypass must be true or false")
    if mode == "bypassPermissions" and not allow_bypass:
        raise ConfigError(f"{where} permission_mode = 'bypassPermissions' requires allow_bypass = true")

    sources = _str_list(sec, "setting_sources", where, default=list(SETTING_SOURCES))
    bad = [s for s in sources if s not in SETTING_SOURCES]
    if bad:
        raise ConfigError(f"{where} setting_sources: unknown value(s) {bad}; allowed: {', '.join(SETTING_SOURCES)}")

    return ClaudeConfig(
        cwd=cwd,
        model=_opt_str(sec, "model", where) or None,
        permission_mode=mode,
        allowed_tools=tuple(_str_list(sec, "allowed_tools", where, default=[])),
        setting_sources=tuple(sources),
        allow_bypass=allow_bypass,
    )


def _parse_ui(sec: Mapping[str, Any]) -> UiConfig:
    where = "[ui]"
    _check_keys(sec, {"verbosity", "max_chunk", "approval_timeout"}, where)
    verbosity = _opt_str(sec, "verbosity", where) or "normal"
    if verbosity not in VERBOSITIES:
        raise ConfigError(f"{where} verbosity must be one of {', '.join(VERBOSITIES)}; got {verbosity!r}")

    max_chunk = sec.get("max_chunk", 3000)
    if isinstance(max_chunk, bool) or not isinstance(max_chunk, int) or max_chunk < 200:
        raise ConfigError(f"{where} max_chunk must be an integer >= 200; got {max_chunk!r}")

    timeout = sec.get("approval_timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"{where} approval_timeout must be a positive number of seconds; got {timeout!r}")
        timeout = float(timeout)

    return UiConfig(verbosity=verbosity, max_chunk=max_chunk, approval_timeout=timeout)


# -- helpers ----------------------------------------------------------------


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    sec = raw.get(name, {})
    if not isinstance(sec, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    return sec


def _check_keys(sec: Mapping[str, Any], known: set[str], where: str) -> None:
    unknown = sorted(set(sec) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _require_str(sec: Mapping[str, Any], key: str, where: str) -> str:
    value = _opt_str(sec, key, where)
    if not value:
        raise ConfigError(f"{where} {key} is required")
    return value


def _opt_str(sec: Mapping[str, Any], key: str, where: str) -> str | None:
    value = sec.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where} {key} must be a string; got {value!r}")
    return value.strip()


def _str_list(sec: Mapping[str, Any], key: str, where: str, default: list[str]) -> list[str]:
    value = sec.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} {key} must be a list of strings")
    return value
