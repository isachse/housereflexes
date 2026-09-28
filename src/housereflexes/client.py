"""Client for the housevitals REST API: live values and the control API (overrides).

housereflexes never talks to a device itself; housevitals is the only Modbus client.
"""

from __future__ import annotations

from typing import Any

import httpx


class HousevitalsError(Exception):
    """housevitals is unreachable or refused a request."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HousevitalsClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=url, headers=headers, timeout=timeout,
                                       transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise HousevitalsError(f"housevitals not reachable: {err}") from err
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise HousevitalsError(f"{method} {path}: {response.status_code} {detail}",
                                   response.status_code)
        return response.json()

    async def values(self, appliance: str, keys: list[str]) -> dict[str, float | int | str | None]:
        """Current values by key. A value that is stale, failed or missing is None, so
        a reflex never acts on outdated readings."""
        data = await self._request("GET", f"/api/v1/appliances/{appliance}/values",
                                   params={"keys": keys, "lang": "en"})
        out: dict[str, Any] = {}
        for key in keys:
            item = (data.get("values") or {}).get(key) or {}
            usable = not item.get("stale") and not item.get("error") and data.get("available") is not False
            out[key] = item.get("value") if usable else None
        return out

    async def overrides(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/overrides"))["overrides"]

    async def put_override(self, appliance: str, key: str, value: Any, owner: str, until: str,
                           reason: str) -> dict[str, Any]:
        return await self._request("PUT", f"/api/v1/appliances/{appliance}/overrides/{key}",
                                   json={"value": value, "owner": owner, "until": until,
                                         "reason": reason})

    async def delete_override(self, appliance: str, key: str, owner: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/appliances/{appliance}/overrides/{key}",
                                   params={"owner": owner})
