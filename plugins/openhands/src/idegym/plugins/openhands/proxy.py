"""Loopback HTTP proxy from the IdeGYM server plugin to the OpenHands Tools Service.

One reusable async client with connection pooling and explicit timeouts. Maps an unreachable
service to 503 and other transport failures to 502; never forwards external auth headers to the
loopback service.
"""

import json
from typing import Any, Optional

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse
from idegym.plugins.openhands.api.names import INTERNAL_PREFIX
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from starlette.background import BackgroundTask


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

    async def stream(self, method: str, subpath: str, *, params: Optional[dict[str, Any]] = None) -> Response:
        """Forward a (potentially large) download without buffering the whole body in memory.

        The upstream response is streamed straight through (httpx stream -> StreamingResponse), so an
        artifact download does not load the entire file into either process. Errors are still read
        eagerly (they are small) and mapped like ``forward``.
        """
        try:
            request = self._client.build_request(method, subpath, params=params)
            resp = await self._client.send(request, stream=True)
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
        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            if content_type.startswith("application/json"):
                return JSONResponse(status_code=resp.status_code, content=json.loads(body or b"{}"))
            return Response(content=body, status_code=resp.status_code, media_type=content_type or None)
        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            media_type=content_type or None,
            background=BackgroundTask(resp.aclose),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
