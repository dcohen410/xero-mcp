"""Xero sign-in with OAuth 2.0 + PKCE ("Mobile or desktop app"): free, no client secret.

Only the refresh token is stored, in the OS keychain (macOS Keychain, Windows
Credential Manager, or the Secret Service on Linux). Xero rotates the refresh
token on every use, so each new one is saved straight away. Access tokens live
in memory only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import http.server
import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring
from keyring.errors import KeyringError

from .api import XeroError
from .config import SCOPES, Config, config_dir

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
KEYRING_SERVICE = "xero-mcp"
REFRESH_EARLY_SECONDS = 120
log = logging.getLogger("xero_mcp")


class NotSignedIn(Exception):
    pass


# ---------------------------------------------------------------- token storage

def _store_key(cfg: Config) -> str:
    return cfg.client_id or "default"


def save_refresh_token(cfg: Config, refresh_token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, _store_key(cfg), json.dumps({"refresh_token": refresh_token}))


def load_refresh_token(cfg: Config) -> str | None:
    try:
        raw = keyring.get_password(KEYRING_SERVICE, _store_key(cfg))
    except KeyringError:
        return None
    return json.loads(raw).get("refresh_token") if raw else None


def clear_refresh_token(cfg: Config) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, _store_key(cfg))
    except KeyringError:
        pass


# ---------------------------------------------------------------- interactive login

def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _wait_for_callback(port: int, timeout: int = 300) -> dict:
    """Serve one request on localhost:<port>/callback and return its query parameters."""
    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in parse_qs(url.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in result
            msg = "Signed in to Xero. You can close this tab." if ok else "Xero sign-in failed. Check the terminal."
            self.wfile.write(f"<html><body style='font-family:sans-serif'><p>{msg}</p></body></html>".encode())
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise RuntimeError("Timed out waiting for the Xero sign-in to finish.")
    finally:
        server.shutdown()
    return result


def login(cfg: Config, organisation: str | None = None, open_browser: bool = True) -> dict:
    """Sign in in the browser, pick the organisation, and store the refresh token.

    Returns the chosen organisation {tenant_id, tenant_name}.
    """
    if not cfg.client_id:
        raise NotSignedIn("xero-mcp is not configured. Run `xero-mcp configure --client-id <id>` first.")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print(f"Opening your browser to sign in to Xero. If it doesn't open, visit:\n{url}\n", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    params = _wait_for_callback(cfg.port)
    if params.get("state") != state:
        raise RuntimeError("Sign-in response didn't match this request. Please try again.")
    if "code" not in params:
        raise RuntimeError(f"Xero sign-in failed: {params.get('error_description') or params.get('error') or params}")

    resp = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "code": params["code"],
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Xero sign-in failed: {resp.text[:300]}")
    token = resp.json()
    save_refresh_token(cfg, token["refresh_token"])

    conns = httpx.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {token['access_token']}"}, timeout=30).json()
    orgs = [c for c in conns if c.get("tenantType") == "ORGANISATION"]
    if not orgs:
        raise RuntimeError("Signed in, but no Xero organisation was connected. Try again and pick an organisation.")
    chosen = orgs[0]
    if organisation:
        matches = [o for o in orgs if organisation.lower() in o["tenantName"].lower()]
        if not matches:
            names = ", ".join(o["tenantName"] for o in orgs)
            raise RuntimeError(f"No connected organisation matches '{organisation}'. Connected: {names}")
        chosen = matches[0]
    elif len(orgs) > 1:
        names = ", ".join(o["tenantName"] for o in orgs)
        print(f"Several organisations are connected ({names}); using {chosen['tenantName']}. "
              "Use `xero-mcp login --organisation <name>` to pick another.", file=sys.stderr)
    return {"tenant_id": chosen["tenantId"], "tenant_name": chosen["tenantName"]}


# ---------------------------------------------------------------- tokens for API calls

class TokenProvider:
    """Hands out a valid access token, refreshing it (and saving the rotated refresh token) as needed."""

    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None):
        self._cfg = cfg
        self._client = client
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._unsaved_refresh: str | None = None

    async def __call__(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - REFRESH_EARLY_SECONDS:
                return self._access_token
            # Claude Desktop and Claude Code each run their own copy of this server, sharing one
            # saved sign-in. Xero refresh tokens are single-use, so only one copy may refresh at a
            # time, and each re-reads the saved token after getting the lock.
            async with _refresh_lock(config_dir() / "refresh.lock"):
                return await self._refresh()

    async def _refresh(self) -> str:
        refresh = self._unsaved_refresh or load_refresh_token(self._cfg)
        if not refresh or not self._cfg.client_id:
            raise NotSignedIn("Not signed in to Xero. Run `xero-mcp login` in a terminal, then try again.")
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "client_id": self._cfg.client_id, "refresh_token": refresh,
            })
        except httpx.HTTPError as exc:
            raise XeroError(503, f"Couldn't reach Xero's sign-in service ({type(exc).__name__}). "
                                 "Check the internet connection and try again.") from None
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code >= 400:
            if _token_error(resp) in ("invalid_grant", "unauthorized_client", "invalid_client"):
                self._unsaved_refresh = None
                raise NotSignedIn(
                    "Your Xero sign-in has expired (Xero signs you out after 60 days without use). "
                    "Run `xero-mcp login` in a terminal, then try again."
                )
            raise XeroError(resp.status_code, "Xero's sign-in service had a problem. Try again in a few minutes; "
                                              "you don't need to sign in again.")
        token = resp.json()
        try:
            save_refresh_token(self._cfg, token["refresh_token"])
            self._unsaved_refresh = None
        except KeyringError:
            # The old token is used up, so keep the new one in memory and try saving it next time.
            self._unsaved_refresh = token["refresh_token"]
            log.warning("Couldn't save the new Xero sign-in to the keychain (is it locked?); will retry.")
        self._access_token = token["access_token"]
        self._expires_at = time.time() + int(token.get("expires_in", 1800))
        return self._access_token


def _token_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", ""))
    except ValueError:
        return ""


@contextlib.asynccontextmanager
async def _refresh_lock(path: Path, timeout: float = 60.0):
    """An exclusive lock on `path` shared by every process on this computer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _lock_file(f)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise XeroError(503, "Another copy of xero-mcp is busy signing in to Xero. Try again shortly.") from None
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            _unlock_file(f)
    finally:
        f.close()


if os.name == "nt":
    import msvcrt

    def _lock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
