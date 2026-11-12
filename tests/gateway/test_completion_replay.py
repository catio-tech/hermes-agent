"""A completion the previous gateway never delivered reaches its session once after a restart.

The wake lived only in the dying gateway's memory: a restart between the job's exit and the
watcher's injection, or a refusal while the gateway drained, lost it, and an agent waiting on the
job stalled. The checkpoint now keeps it until the watcher routes it; the next boot replays it
through the same watcher, and the boot after that delivers nothing.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from tests.tools.test_checkpoint_undelivered import _entries, finish_armed


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """Each call is a fresh gateway boot: a new registry recovers the checkpoint, a runner runs its watchers."""
    import gateway.run as gateway_run
    from tools import process_registry as pr
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    real_sleep = asyncio.sleep

    async def _instant(*_a, **_kw):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", _instant)

    async def _boot(delivered=True):
        registry = pr.ProcessRegistry()
        monkeypatch.setattr(pr, "process_registry", registry)
        registry.recover_from_checkpoint()
        runner = GatewayRunner(GatewayConfig())
        runner.adapters[Platform.TELEGRAM] = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
        runner._enqueue_process_completion_notification = AsyncMock(return_value=delivered)
        tasks = [asyncio.ensure_future(runner._run_process_watcher(w)) for w in registry.pending_watchers]
        return runner, tasks
    return _boot


@pytest.mark.asyncio
@pytest.mark.platforms("linux", "macos")
async def test_undelivered_completion_is_replayed_exactly_once(boot, tmp_path):
    from tools.process_registry import ProcessRegistry
    done = finish_armed(ProcessRegistry(), tmp_path)  # exited while the old gateway was going down

    runner, tasks = await boot()
    await asyncio.gather(*tasks)
    runner._enqueue_process_completion_notification.assert_awaited_once()
    _text, evt = runner._enqueue_process_completion_notification.await_args.args
    assert (evt["session_id"], evt["started_at"], evt["exit_code"]) == (done.id, done.started_at, 3)
    assert "out" in evt["output"]
    assert _entries(tmp_path / "processes.json") == []

    runner, tasks = await boot()
    assert tasks == []
    runner._enqueue_process_completion_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.platforms("linux", "macos")
async def test_refused_wake_cancelled_by_restart_is_kept_for_the_next_boot(boot, tmp_path):
    from tools.process_registry import ProcessRegistry
    done = finish_armed(ProcessRegistry(), tmp_path)

    runner, [task] = await boot(delivered=False)  # draining gateway refuses; its stop cancels the watcher
    while not runner._enqueue_process_completion_notification.await_count:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["session_id"] for e in _entries(tmp_path / "processes.json")] == [done.id]

    runner, tasks = await boot()
    await asyncio.gather(*tasks)
    runner._enqueue_process_completion_notification.assert_awaited_once()
