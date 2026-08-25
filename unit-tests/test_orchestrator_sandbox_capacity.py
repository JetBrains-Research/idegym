from asyncio import sleep
from types import SimpleNamespace

from idegym.api.config import Config, NodePoolConfig
from idegym.orchestrator import main


async def test_lifespan_prepares_capacity_before_serving(mocker):
    config = Config()
    config.orchestrator.node_pool = NodePoolConfig(
        max_sandboxes_per_node=20,
        sandbox_capacity_owner="grazie/idegym",
    )
    app = SimpleNamespace(state=SimpleNamespace(config=config))
    mocker.patch.object(main, "load_kubernetes_config", mocker.AsyncMock())
    mocker.patch.object(main, "init_db", mocker.AsyncMock())
    prepare = mocker.patch.object(main, "prepare_sandbox_node_capacity", mocker.AsyncMock())
    reconcile = mocker.patch.object(
        main,
        "reconcile_sandbox_node_capacity_periodically",
        mocker.AsyncMock(),
    )

    async with main.lifespan(app):
        prepare.assert_awaited_once_with(20, "grazie/idegym")
        await sleep(0)

    reconcile.assert_awaited_once_with(20, "grazie/idegym")


async def test_lifespan_runs_capacity_cleanup_before_serving(mocker):
    config = Config()
    config.orchestrator.node_pool = NodePoolConfig(
        sandbox_capacity_cleanup=True,
        sandbox_capacity_owner="grazie/idegym",
    )
    app = SimpleNamespace(state=SimpleNamespace(config=config))
    mocker.patch.object(main, "load_kubernetes_config", mocker.AsyncMock())
    mocker.patch.object(main, "init_db", mocker.AsyncMock())
    cleanup = mocker.patch.object(main, "cleanup_sandbox_node_capacity", mocker.AsyncMock())

    async with main.lifespan(app):
        cleanup.assert_awaited_once_with("grazie/idegym")
