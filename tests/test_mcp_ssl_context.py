import asyncio
import threading

from src import mcp_manager


def test_ssl_context_is_built_once_and_off_the_loop(monkeypatch):
    calls = []
    main = threading.get_ident()

    def fake_create():
        calls.append(threading.get_ident())
        return object()

    import httpx
    monkeypatch.setattr(httpx, "create_ssl_context", fake_create)
    monkeypatch.setattr(mcp_manager, "_ssl_context", None)
    monkeypatch.setattr(mcp_manager, "_ssl_context_lock", None)

    async def run():
        return await asyncio.gather(
            mcp_manager._shared_ssl_context(), mcp_manager._shared_ssl_context()
        )

    a, b = asyncio.run(run())
    assert a is b
    assert len(calls) == 1 and calls[0] != main


def test_factory_clients_share_the_context(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(mcp_manager, "_ssl_context", sentinel)

    async def run():
        f = await mcp_manager._mcp_httpx_factory()
        import httpx
        seen = []
        orig = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: seen.append(kw["verify"]))
        f(); f()
        return seen

    assert asyncio.run(run()) == [sentinel, sentinel]
