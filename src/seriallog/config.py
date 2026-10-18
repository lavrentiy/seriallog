"""Configuration, read from a TOML file.

    log_dir = "/var/lib/seriallog"
    retention_days = 90          # delete day files older than this
    flush_after = 1.0            # seconds of silence before a partial line is written

    [[port]]
    name = "usb0"                # used in file names and on the command line
    host = "127.0.0.1"           # ser2net
    port = 4001

    [[alert]]
    name = "crash"
    pattern = "PANIC|HardFault"  # Python regex, searched in each line
    ignore_case = true
    ports = ["usb0"]             # optional; default: all ports
    cooldown = 60                # seconds before the same alert fires again on a port
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = "/etc/seriallog.toml"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Port:
    name: str
    host: str
    port: int


@dataclass(frozen=True)
class Alert:
    name: str
    regex: re.Pattern
    ports: frozenset | None = None   # None: every port
    cooldown: float = 60.0

    def applies_to(self, port_name):
        return self.ports is None or port_name in self.ports


@dataclass(frozen=True)
class Config:
    log_dir: Path
    ports: tuple = ()
    alerts: tuple = ()
    retention_days: int = 90
    flush_after: float = 1.0
    path: Path | None = field(default=None, compare=False)

    def port(self, name):
        for p in self.ports:
            if p.name == name:
                return p
        known = ", ".join(p.name for p in self.ports) or "none configured"
        raise ConfigError(f"unknown port {name!r} (known: {known})")


def config_path(arg=None):
    return Path(arg or os.environ.get("SERIALLOG_CONFIG") or DEFAULT_CONFIG)


def load(path=None):
    path = config_path(path)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(raw, path)


def parse(raw, path=None):
    where = f"{path}: " if path else ""
    try:
        log_dir = Path(raw.get("log_dir", "/var/lib/seriallog"))
        retention = int(raw.get("retention_days", 90))
        flush_after = float(raw.get("flush_after", 1.0))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}{e}") from None
    if retention < 1:
        raise ConfigError(f"{where}retention_days must be at least 1")
    if flush_after <= 0:
        raise ConfigError(f"{where}flush_after must be positive")

    ports = []
    for i, p in enumerate(raw.get("port", [])):
        name = p.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError(f"{where}port #{i + 1}: name must be letters, digits, - or _")
        if any(q.name == name for q in ports):
            raise ConfigError(f"{where}port {name!r} is defined twice")
        try:
            ports.append(Port(name, str(p.get("host", "127.0.0.1")), int(p["port"])))
        except (KeyError, TypeError, ValueError):
            raise ConfigError(f"{where}port {name!r}: needs a TCP port number") from None

    alerts = []
    names = {p.name for p in ports}
    for i, a in enumerate(raw.get("alert", [])):
        name = a.get("name") or f"alert{i + 1}"
        pattern = a.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(f"{where}alert {name!r}: needs a pattern")
        flags = re.IGNORECASE if a.get("ignore_case", False) else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ConfigError(f"{where}alert {name!r}: bad pattern: {e}") from None
        only = a.get("ports")
        if only is not None:
            unknown = set(only) - names
            if unknown:
                raise ConfigError(f"{where}alert {name!r}: unknown ports {sorted(unknown)}")
            only = frozenset(only)
        alerts.append(Alert(name, regex, only, float(a.get("cooldown", 60))))

    return Config(log_dir, tuple(ports), tuple(alerts), retention, flush_after, path)
