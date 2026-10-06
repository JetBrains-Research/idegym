"""Check forwarding payload omission against PostgreSQL and an HTTP transport."""

import asyncio
import json

import pytest
from httpx import AsyncClient, MockTransport, Response
from idegym.api.orchestrator.operations import AsyncOperationType
from idegym.orchestrator.database import database
from idegym.orchestrator.database.models import AsyncOperation, Client, IdeGYMServer
from idegym.orchestrator.router.async_operation import get_operation_status
from idegym.orchestrator.router.forwarding import forward_request_to_server
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers


@pytest.mark.parametrize("persist_body", [None, False])
@pytest.mark.parametrize("inline", [True, False])
@pytest.mark.parametrize("status_code", [200, 422, 500])
async def test_forwarding_preserves_execution_and_results(db, db_url, monkeypatch, persist_body, inline, status_code):
    engine = create_async_engine(db_url, pool_size=3, max_overflow=0)
    monkeypatch.setattr(database, "SessionFactory", async_sessionmaker(engine, expire_on_commit=False))
    client = Client(name="forwarding-test")
    db.add(client)
    await db.flush()
    server = IdeGYMServer(client_id=client.id, generated_name="forwarding-test", pod_ip="127.0.0.1")
    db.add(server)
    await db.commit()
    server_id = server.id
    start_request = {"image_tag": "test-image", "command": "start input"}
    start = await database.save_async_operation(
        db, AsyncOperationType.START_SERVER, client_id=client.id, server_id=server.id, request=start_request
    )
    body = json.dumps({"command": "echo 'héllo'\n" + "runner input\n" * 3000}, ensure_ascii=False)
    seen = []

    def sandbox(request):
        seen.append(request.content)
        return Response(status_code, content='{"output":"complete result"}')

    kwargs = {} if persist_body is None else {"persist_forward_request_body": persist_body}
    try:
        async with AsyncClient(transport=MockTransport(sandbox)) as http:
            result = await forward_request_to_server(
                client_id=client.id,
                server_id=server.id,
                path="api/tools/bash",
                method="POST",
                headers=Headers({"content-type": "application/json"}),
                body=body,
                http_client=http,
                wait_seconds=5 if inline else 0,
                **kwargs,
            )
            async with asyncio.timeout(5):
                while True:
                    db.expire_all()
                    operation = (
                        await db.execute(select(AsyncOperation).where(AsyncOperation.request_type == "FORWARD_REQUEST"))
                    ).scalar_one()
                    if operation.finished_at is not None:
                        break
                    await db.commit()
                    await asyncio.sleep(0.01)
            assert seen == [body.encode("utf-8")]
            stored = json.loads(operation.request)
            assert stored["body"] == (None if persist_body is False else body)
            assert stored["path"] == "api/tools/bash"
            assert stored["method"] == "POST" and stored["server_id"] == server_id
            assert stored["headers"] == {"content-type": "application/json"}
            assert operation.payloads_expired_at is None
            assert operation.status == ("SUCCEEDED" if status_code == 200 else "FAILED")
            persisted_result = json.loads(operation.result)
            assert persisted_result["body"] == '{"output":"complete result"}'
            assert persisted_result["status_code"] == status_code
            if inline:
                assert result.body == persisted_result["body"] and result.status_code == status_code
            else:
                assert result.async_operation_id == operation.id
            polled = await get_operation_status(operation.id)
            assert polled.payloads_expired_at is None
            assert json.loads(polled.result) == persisted_result
            await db.refresh(start)
            assert json.loads(start.request) == start_request
    finally:
        await engine.dispose()
