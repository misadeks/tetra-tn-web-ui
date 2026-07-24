"""Configuration loading: config.toml -> environment -> CLI overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChannelConfig:
    host: str = "127.0.0.1"
    port: int = 0
    use_tls: bool = False
    ca_cert: str | None = None
    username: str = ""
    password: str = ""

    @property
    def credentials(self) -> tuple[str, str] | None:
        """Return (user, pass) if auth is configured, else None (accept all)."""
        if self.username:
            return (self.username, self.password)
        return None


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    poll_interval: float = 2.0


@dataclass
class RegistrationConfig:
    """Operator preference for how to register — NOT identity.

    The radio's identity (ISSI, home MCC/MNC) is owned by the MS and read live
    from MsRuntimeState; it is never configured here.
    """
    registration_type: str = "RegistrationToIndicatedCell"


@dataclass
class Config:
    command: ChannelConfig = field(default_factory=lambda: ChannelConfig(port=9102))
    telemetry: ChannelConfig = field(default_factory=lambda: ChannelConfig(port=9101))
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    registration: RegistrationConfig = field(default_factory=RegistrationConfig)


def _channel_from(raw: dict, default_port: int) -> ChannelConfig:
    return ChannelConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", default_port)),
        use_tls=bool(raw.get("use_tls", False)),
        ca_cert=raw.get("ca_cert") or None,
        username=str(raw.get("username", "")),
        password=str(raw.get("password", "")),
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config from a TOML file (if present) and apply env overrides."""
    cfg = Config()

    file_path = Path(path) if path else Path("config.toml")
    if file_path.exists():
        raw = tomllib.loads(file_path.read_text(encoding="utf-8"))
        if "command" in raw:
            cfg.command = _channel_from(raw["command"], 9102)
        if "telemetry" in raw:
            cfg.telemetry = _channel_from(raw["telemetry"], 9101)
        if "dashboard" in raw:
            d = raw["dashboard"]
            cfg.dashboard = DashboardConfig(
                host=str(d.get("host", "127.0.0.1")),
                port=int(d.get("port", 8080)),
                poll_interval=float(d.get("poll_interval", 2.0)),
            )
        if "registration" in raw or "radio" in raw:
            r = raw.get("registration", raw.get("radio", {}))
            cfg.registration = RegistrationConfig(
                registration_type=str(r.get("registration_type", "RegistrationToIndicatedCell")),
            )

    # Environment overrides (handy for containers / CI).
    _env_override(cfg.command, "TNMM_CONTROL")
    _env_override(cfg.telemetry, "TNMM_TELEMETRY")
    if v := os.environ.get("TNMM_DASHBOARD_HOST"):
        cfg.dashboard.host = v
    if v := os.environ.get("TNMM_DASHBOARD_PORT"):
        cfg.dashboard.port = int(v)

    return cfg


def _env_override(chan: ChannelConfig, prefix: str) -> None:
    if v := os.environ.get(f"{prefix}_HOST"):
        chan.host = v
    if v := os.environ.get(f"{prefix}_PORT"):
        chan.port = int(v)
    if v := os.environ.get(f"{prefix}_USERNAME"):
        chan.username = v
    if v := os.environ.get(f"{prefix}_PASSWORD"):
        chan.password = v
