"""A notify-armed background job's arm state reaches the checkpoint before its spawn call returns.

Live gap: the spawn wrote processes.json before ``notify_on_complete``, the chat routing and
``watcher_interval`` were stamped on the session, and nothing rewrote it afterwards. A gateway
restart before any other checkpoint write re-adopted the job as a silent one (no watcher), so its
completion was recorded with ``notify_on_complete: false`` and dropped from the checkpoint without
a wake. Real processes; the systemd scope is faked only where ``systemd-run --user`` would wrap it.
"""
import json

import pytest

_ROUTE = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_CHAT_ID": "C0TEST",
          "HERMES_SESSION_THREAD_ID": "1794333905.707479", "HERMES_SESSION_ID": "20261110_180505_69982de0"}


@pytest.fixture
def gateway_registry(tmp_path, monkeypatch):
    from gateway import session_context
    from tools import process_registry as pr
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(pr, "_build_systemd_scope_argv", lambda argv, unit_suffix: argv)
    monkeypatch.setattr(pr, "_stop_systemd_unit", lambda unit: True)
    monkeypatch.setattr(session_context, "get_session_env", lambda name, default="": _ROUTE.get(name, default))
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    yield registry, pr
    for session in list(registry._running.values()):
        registry.kill_process(session.id)


def _entry(tmp_path, session_id):
    entries = json.loads((tmp_path / "processes.json").read_text())
    return next(e for e in entries if e["session_id"] == session_id)


def _assert_armed_and_recovered(pr, tmp_path, session_id):
    entry = _entry(tmp_path, session_id)
    assert entry["notify_on_complete"] is True and entry["watcher_interval"] == 5, entry
    assert entry["watcher_platform"] == "slack" and entry["watcher_thread_id"] == _ROUTE["HERMES_SESSION_THREAD_ID"]
    successor = pr.ProcessRegistry()  # the relaunched gateway, before any other checkpoint write
    assert successor.recover_from_checkpoint() == 1
    assert [w["session_id"] for w in successor.pending_watchers] == [session_id]
    assert successor.pending_watchers[0]["notify_on_complete"] is True
    successor.kill_process(session_id)


@pytest.mark.platforms("linux")
def test_notify_spawn_checkpoints_its_arm_state(gateway_registry, tmp_path):
    from tools.terminal_tool_background import spawn_background_process
    registry, pr = gateway_registry
    result = json.loads(spawn_background_process(
        command="sleep 60", env=object(), env_type="local", effective_task_id="t1", task_id="t1",
        session_key="agent:dev:slack:group:T:C0TEST:1794333905.707479", workdir=None, cwd=str(tmp_path),
        effective_pty=False, notify_on_complete=True, watch_patterns=None, approval_note=None,
        pty_disabled_reason=None))
    assert result["notify_on_complete"] is True and registry._running[result["session_id"]].output_log
    _assert_armed_and_recovered(pr, tmp_path, result["session_id"])


@pytest.mark.platforms("linux")
def test_yielded_foreground_command_checkpoints_its_arm_state(gateway_registry, tmp_path):
    from tools.terminal_tool_background import _stamp_routing_if_gateway
    registry, pr = gateway_registry
    session = registry.spawn_local("sleep 60", cwd=str(tmp_path))
    session.notify_on_complete = True  # adopt_local arms the yielded command
    _stamp_routing_if_gateway(registry, session, "agent:dev:slack:group:T:C0TEST:1794333905.707479")
    _assert_armed_and_recovered(pr, tmp_path, session.id)
