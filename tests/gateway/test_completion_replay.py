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


@pytest.mark.asyncio
@pytest.mark.platforms("linux", "macos")
async def test_drain_refusals_spend_no_retries_and_never_ack(boot, tmp_path):
    """A restart's after-turn wait refuses every injection (runtime_draining) for up to 30 min: the
    watcher must not use up its retries, fall back to the plain notice and ack the wake away."""
    from tools.process_registry import ProcessRegistry
    done = finish_armed(ProcessRegistry(), tmp_path)

    runner, [task] = await boot(delivered=False)
    runner._draining = True
    while runner._enqueue_process_completion_notification.await_count < 12 and not task.done():  # > 8 attempts
        await asyncio.sleep(0)
    assert not task.done() and not done.completion_delivered
    runner.adapters[Platform.TELEGRAM].send.assert_not_awaited()  # no plain-notice fallback
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["session_id"] for e in _entries(tmp_path / "processes.json")] == [done.id]


@pytest.mark.asyncio
@pytest.mark.platforms("linux", "macos")
async def test_a_watcher_that_raises_keeps_the_wake_and_says_so(boot, tmp_path, caplog):
    from tools.process_registry import ProcessRegistry
    done = finish_armed(ProcessRegistry(), tmp_path)

    runner, [task] = await boot()
    runner._watch_process = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level("WARNING", logger="gateway.run"), pytest.raises(RuntimeError):
        await task
    assert any(done.id in r.getMessage() and "next gateway boot" in r.getMessage() for r in caplog.records)
    assert [e["session_id"] for e in _entries(tmp_path / "processes.json")] == [done.id]


@pytest.mark.asyncio
@pytest.mark.platforms("linux", "macos")
async def test_replay_of_a_wake_admitted_before_the_restart_is_deduplicated(boot, tmp_path, monkeypatch):
    """The old gateway's injection was admitted, then the restart cancelled the watcher before its ack.
    The replay carries the same producer identity, so session-admission lost-ACK takes it as
    delivered: no second injection, and the wake is acked."""
    import gateway.session_automation as automation
    from tools.process_registry import ProcessRegistry
    done = finish_armed(ProcessRegistry(), tmp_path)
    admitted: set = set()
    monkeypatch.setattr(automation, "completion_admission", lambda runner, evt: (
        {"admission": 1} if automation.producer_identity(runner, evt) in admitted else None))

    def through_real_delivery(runner, inject):
        runner.session_authority = object()
        runner._preflight_completion_delivery = AsyncMock(return_value=runner._CompletionClaim())
        runner._enqueue_process_completion_notification = runner._deliver_completion_notification
        runner._inject_watch_notification = inject

    async def admit_then_hang(_text, evt, **_kw):
        admitted.add(automation.producer_identity(runner, evt))
        await asyncio.Event().wait()  # the restart cancels the watcher before it acks

    runner, [task] = await boot()
    through_real_delivery(runner, admit_then_hang)
    while not admitted:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["session_id"] for e in _entries(tmp_path / "processes.json")] == [done.id]

    runner, tasks = await boot()
    reinject = AsyncMock(return_value=True)
    through_real_delivery(runner, reinject)
    await asyncio.gather(*tasks)
    reinject.assert_not_awaited()
    assert _entries(tmp_path / "processes.json") == []


def _job_outlives_its_gateway_and_exits(tmp_path, home):
    """A supervised gateway spawns a restart-safe, notify-armed job, then dies at once (os._exit, no
    cleanup); returns (session_id, started_at) once the job has exited with no gateway running."""
    import json
    import os
    import subprocess
    import sys
    import textwrap
    import time
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "PYTHONPATH")}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(root))
    spawner = textwrap.dedent(f"""
        import json, os, pathlib
        import tools.process_registry as pr
        pr.CHECKPOINT_PATH = pathlib.Path({str(tmp_path / "processes.json")!r})
        pr._is_supervised_gateway_process = lambda: True
        pr._systemd_run_user_scope_available = lambda: True
        pr._build_systemd_scope_argv = lambda argv, unit_suffix: argv
        s = pr.process_registry.spawn_local("sleep 1; echo DONE_$((6*7)); (exit 7)", cwd={str(tmp_path)!r},
                                            session_key="agent:main:telegram:dm:1")
        s.notify_on_complete, s.watcher_interval, s.watcher_platform = True, 5, "telegram"
        s.watcher_chat_id, s.parent_session_id = "1", "sess-1"
        pr.process_registry._write_checkpoint()
        print(json.dumps([s.id, s.started_at, s.output_log]), flush=True)
        os._exit(0)
    """)
    out = subprocess.run([sys.executable, "-c", spawner], env=env, cwd=root, capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    session_id, started_at, log = json.loads(out.stdout.strip().splitlines()[-1])
    exit_record = Path(log).with_suffix(".exit")
    deadline = time.monotonic() + 30
    while not exit_record.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert exit_record.exists()  # the job finished while no gateway ran
    return session_id, started_at


@pytest.mark.asyncio
@pytest.mark.platforms("linux")
async def test_job_that_exits_while_the_gateway_is_down_wakes_once(boot, tmp_path, monkeypatch):
    """A restart-safe job outlives its gateway and exits before the next boot: the successor adopts it
    from the log, collects the recorded exit, and its watcher delivers the completion once; the boot
    after that delivers nothing."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))  # the adopter reads this profile's output log
    session_id, started_at = _job_outlives_its_gateway_and_exits(tmp_path, home)

    runner, tasks = await boot()
    await asyncio.gather(*tasks)
    runner._enqueue_process_completion_notification.assert_awaited_once()
    _text, evt = runner._enqueue_process_completion_notification.await_args.args
    assert (evt["session_id"], evt["started_at"], evt["exit_code"]) == (session_id, started_at, 7)
    assert "DONE_42" in evt["output"]
    assert _entries(tmp_path / "processes.json") == []

    runner, tasks = await boot()
    assert tasks == []
