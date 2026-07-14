"""Loopback HTTP proxy from the IdeGYM server plugin to the OpenHands Tools Service.

One reusable async client with connection pooling and explicit timeouts. Maps an unreachable
service to 503 and other transport failures to 502; never forwards external auth headers to the
loopback service.
"""

from typing import Any, Optional

import httpx
from fastapi.responses import JSONResponse, Response
from idegym.plugins.openhands.api.names import INTERNAL_PREFIX
from idegym.plugins.openhands.runtime.config import RuntimeConfig


class LoopbackProxy:
    def __init__(
        self, base_url: Optional[str] = None, *, connect_timeout: float = 2.0, read_timeout: float = 900.0
    ) -> None:
        if base_url is None:
            config = RuntimeConfig.from_env()
            base_url = f"http://{config.service_host}:{config.service_port}{INTERNAL_PREFIX}"
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    async def forward(
        self,
        method: str,
        subpath: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Response:
        try:
            resp = await self._client.request(method, subpath, json=json_body, params=params)
        except httpx.ConnectError:
            return JSONResponse(
                status_code=503,
                content={"error": "service_unavailable", "message": "OpenHands Tools Service is unreachable"},
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "internal_error", "message": f"OpenHands Tools Service proxy error: {exc}"},
            )
        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        return Response(content=resp.content, status_code=resp.status_code, media_type=content_type or None)

    async def aclose(self) -> None:
        await self._client.aclose()
