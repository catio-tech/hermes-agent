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
