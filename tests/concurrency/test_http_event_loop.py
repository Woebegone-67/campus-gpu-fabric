"""A waiting writer must not stop unrelated ASGI requests from being served."""

import asyncio
from threading import Event, Timer

import httpx

from campus_fabric.app import create_app


def test_waiting_write_keeps_event_loop_available(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path)
    entered, release = Event(), Event()
    original = app.state.kernel.commit_with_payload

    def blocked_commit(command):
        entered.set()
        assert release.wait(5), "Test writer was not released"
        return original(command)

    monkeypatch.setattr(app.state.kernel, "commit_with_payload", blocked_commit)

    @app.get("/test-loop-probe")
    async def probe():
        return {"writer_still_waiting": not release.is_set()}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            writer = asyncio.create_task(client.post("/v1/allocations", headers={"X-Owner": "alice"},
                json={"owner": "alice", "request": {"cpu_millicores": 1000,
                      "memory_mib": 1024, "gpu": {"count": 0}}}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                response = await client.get("/test-loop-probe")
                assert response.status_code == 200
                assert response.json()["writer_still_waiting"], "Writer blocked the event loop"
            finally:
                release.set()
                result = await writer
            assert result.status_code == 202, result.text

    # Bound a regression in which the synchronous writer blocks the event loop.
    watchdog = Timer(3, release.set)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.cancel()
