"""Settings for the server.

Settings come from environment variables first, then from a JSON file in the
user's config folder (written by `xero-mcp configure` and `xero-mcp login`).
The sign-in token itself is never stored here; it lives in the OS keychain.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "XERO_MCP_"

# Xero's granular scopes (required for apps created from March 2026). Each one
# must also be ticked on the app's Configuration page in the Xero developer portal.
SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "accounting.invoices",  # invoices and bills (bills are ACCPAY invoices)
    "accounting.contacts",
    "accounting.settings.read",  # organisation, accounts, tax rates, tracking
    "accounting.attachments",
    "accounting.payments.read",
    "accounting.banktransactions.read",
    "accounting.reports.profitandloss.read",
    "accounting.reports.balancesheet.read",
]

APPROVE_MODES = ("confirm", "disabled")
DEFAULT_PORT = 8765


def config_dir() -> Path:
    override = os.environ.get(ENV_PREFIX + "CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "xero-mcp"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "xero-mcp"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    client_id: str = ""
    # The Xero organisation chosen at login.
    tenant_id: str = ""
    tenant_name: str = ""
    # Port of the local sign-in callback. The redirect URI registered in Xero must be
    # http://localhost:<port>/callback
    port: int = DEFAULT_PORT
    # Local folders the server may read attachments from and save downloads to.
    local_dirs: list[str] = field(default_factory=lambda: [str(Path.home() / "Downloads")])
    # "confirm": approving bills is allowed, with the user's confirmation each time.
    # "disabled": the server can only create and edit drafts.
    approve_mode: str = "disabled"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/callback"

    def local_roots(self) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in self.local_dirs if p]


def load() -> Config:
    data: dict = {}
    if config_path().is_file():
        data = json.loads(config_path().read_text(encoding="utf-8"))
    cfg = Config(
        client_id=data.get("client_id", ""),
        tenant_id=data.get("tenant_id", ""),
        tenant_name=data.get("tenant_name", ""),
        port=int(data.get("port", DEFAULT_PORT)),
        local_dirs=data.get("local_dirs") or Config().local_dirs,
        approve_mode=data.get("approve_mode", "disabled"),
    )
    env = os.environ
    cfg.client_id = env.get(ENV_PREFIX + "CLIENT_ID", cfg.client_id)
    cfg.tenant_id = env.get(ENV_PREFIX + "TENANT_ID", cfg.tenant_id)
    if env.get(ENV_PREFIX + "LOCAL_DIRS"):
        cfg.local_dirs = [p for p in env[ENV_PREFIX + "LOCAL_DIRS"].split(os.pathsep) if p]
    cfg.approve_mode = env.get(ENV_PREFIX + "APPROVE_MODE", cfg.approve_mode)
    if cfg.approve_mode not in APPROVE_MODES:
        cfg.approve_mode = "disabled"
    return cfg


def save(cfg: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "client_id": cfg.client_id,
                "tenant_id": cfg.tenant_id,
                "tenant_name": cfg.tenant_name,
                "port": cfg.port,
                "local_dirs": cfg.local_dirs,
                "approve_mode": cfg.approve_mode,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
