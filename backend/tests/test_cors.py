"""gateway CORS 预检测试：本地/局域网来源 + Private Network Access。"""

import httpx
import pytest

from app.gateway.app import app


@pytest.mark.asyncio
async def test_cors_preflight_allows_local_and_lan_origins():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for origin in (
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://192.168.1.6:3000",
        ):
            resp = await client.options(
                "/chat/stream",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                    "Access-Control-Request-Private-Network": "true",
                },
            )
            assert resp.status_code == 200, origin
            assert resp.headers["access-control-allow-origin"] == "*"
            assert resp.headers["access-control-allow-private-network"] == "true"
