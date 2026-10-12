"""A small async Xero Accounting API client: tenant header, retries and error messages."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

BASE = "https://api.xero.com/api.xro/2.0"
MAX_ATTEMPTS = 3
_MS_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


class XeroError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Xero error {status}: {message}")
        self.status = status


def _error_message(resp: httpx.Response) -> str:
    if resp.status_code == 401 and "insufficient_scope" in resp.headers.get("WWW-Authenticate", ""):
        return ("This needs a Xero permission the sign-in doesn't have yet. Tick the missing scope on the app's "
                "Configuration page at developer.xero.com, then run `xero-mcp login` again.")
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    # Validation errors are nested inside Elements[].ValidationErrors[].
    msgs = [
        v.get("Message")
        for el in body.get("Elements", []) or []
        for v in el.get("ValidationErrors", []) or []
        if v.get("Message")
    ]
    return "; ".join(msgs) or body.get("Detail") or body.get("Message") or body.get("Title") or resp.text[:300]


def clean_dates(value: Any) -> Any:
    """Turn Xero's /Date(1700000000000+0000)/ strings into ISO dates, recursively."""
    if isinstance(value, dict):
        return {k: clean_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_dates(v) for v in value]
    if isinstance(value, str):
        m = _MS_DATE.fullmatch(value)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return value


class Xero:
    def __init__(self, token: Callable[[], Awaitable[str]], tenant_id: str, client: httpx.AsyncClient | None = None):
        self._token = token
        self._tenant = tenant_id
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None,
        content: bytes | None = None, headers: dict | None = None,
    ) -> httpx.Response:
        if not self._tenant:
            raise XeroError(401, "No Xero organisation selected yet. Run `xero-mcp login` in a terminal.")
        hdrs = {
            "Authorization": f"Bearer {await self._token()}",
            "xero-tenant-id": self._tenant,
            "Accept": "application/json",
            **(headers or {}),
        }
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = await self._client.request(method, BASE + path, params=params, json=json, content=content,
                                              headers=hdrs)
            if resp.status_code in (429, 503) and attempt < MAX_ATTEMPTS:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                if wait > 30:
                    break  # daily limit or long back-off: report it rather than hang
                await asyncio.sleep(wait)
                continue
            break
        if resp.status_code == 429:
            raise XeroError(429, f"Xero is rate limiting requests. Try again in {resp.headers.get('Retry-After', 'a few')} seconds.")
        if resp.status_code >= 400:
            raise XeroError(resp.status_code, _error_message(resp))
        return resp

    async def get(self, path: str, params: dict | None = None) -> dict:
        return clean_dates((await self.request("GET", path, params=params)).json())

    async def post(self, path: str, json: Any, params: dict | None = None) -> dict:
        return clean_dates((await self.request("POST", path, json=json, params=params)).json())

    async def get_bytes(self, path: str) -> bytes:
        return (await self.request("GET", path, headers={"Accept": "application/octet-stream"})).content
