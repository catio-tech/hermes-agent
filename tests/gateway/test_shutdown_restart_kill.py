"""The gateway's shutdown sweep tells a graceful restart from a final stop.

``_stop_kill_tool_subprocesses`` swept with ``source="gateway_shutdown"`` on every stop, so a
SIGUSR1 relaunch killed the background jobs a successor could have re-adopted. A restart now
sweeps with ``gateway_restart`` (sparing restart-safe jobs) and arms the handoff before agents
are finalized; a final stop still sweeps with ``gateway_shutdown``.
"""
import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def sweep_sources(monkeypatch):
    import tools.process_registry as pr
    sources: list = []
    monkeypatch.setattr(pr.process_registry, "kill_all", lambda task_id=None, **kw: sources.append(kw["source"]) or 0)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *a, **k: 0)
    monkeypatch.setattr("tools.terminal_tool_lifecycle.cleanup_all_environments", lambda: None)
    monkeypatch.setattr("tools.browser_session_lifecycle.cleanup_all_browsers", lambda: None)
    return sources


@pytest.mark.parametrize(("restart", "source"), [(True, "gateway_restart"), (False, "gateway_shutdown")])
def test_sweep_source_follows_restart(sweep_sources, restart, source):
    GatewayShutdownMixin._stop_kill_tool_subprocesses("final-cleanup", restart)
    assert sweep_sources == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [True, False])
async def test_stop_arms_the_restart_handoff_only_on_restart(monkeypatch, sweep_sources, restart):
    import tools.process_registry as pr
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(registry, "kill_all", lambda task_id=None, **kw: sweep_sources.append(kw["source"]) or 0)
    runner, _adapter = make_restart_runner()
    await runner.stop(restart=restart)
    assert registry._restart_handoff is restart
    assert sweep_sources and set(sweep_sources) == {"gateway_restart" if restart else "gateway_shutdown"}


@pytest.mark.asyncio
async def test_planned_stop_during_a_pending_restart_is_a_final_stop(monkeypatch, sweep_sources):
    """``hermes gateway stop`` lands in the restart's after-turn wait: no successor will start, so the
    stop kills everything, and the restart task's later stop(restart=True) cannot re-arm the handoff."""
    import signal
    import tools.process_registry as pr
    from gateway.run_bootstrap import _start_gateway_make_shutdown_signal_handler
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(registry, "kill_all", lambda task_id=None, **kw: sweep_sources.append(kw["source"]) or 0)
    monkeypatch.setattr("gateway.status.consume_takeover_marker_for_self", lambda: False)
    monkeypatch.setattr("gateway.status.consume_planned_stop_marker_for_self", lambda: True)
    monkeypatch.setattr("gateway.shutdown_forensics.snapshot_shutdown_context", lambda *_a: 1 / 0)
    runner, _adapter = make_restart_runner()
    runner._restart_requested = runner._restart_via_service = True  # SIGUSR1 accepted, after-turn wait running
    registry.begin_restart_handoff()

    real_stop, stops = runner.stop, []
    runner.stop = lambda **kw: stops.append(kw) or real_stop(**kw)
    _start_gateway_make_shutdown_signal_handler(runner, [False])(signal.SIGTERM)
    assert not runner._restart_requested and not registry._restart_handoff
    import asyncio
    while runner._stop_task is None:  # the handler scheduled stop()
        await asyncio.sleep(0)
    await runner._stop_task
    assert stops == [{}]
    await real_stop(restart=True, service_restart=True)  # the pending restart task arrives late
    assert not registry._restart_handoff and set(sweep_sources) == {"gateway_shutdown"}
