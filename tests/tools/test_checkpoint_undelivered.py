"""processes.json keeps a notify-armed completion until its gateway watcher acks it.

The checkpoint held only running processes: the exit rewrote it without the session, and the
completion lived only in the gateway's memory. A restart between a job's exit and the watcher's
injection (or a refusal while the gateway drained) lost the wake for good. Now a finished,
notify-armed, unacked session stays in the checkpoint with its exit, boot restores it as finished
and re-arms its watcher, and the ack drops it. Real processes, real checkpoint file.
"""
import json
import logging
import time

import pytest


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    from tools import process_registry as pr
    path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", path)
    return path


def _entries(path):
    return json.loads(path.read_text()) if path.exists() else []


def finish_armed(registry, tmp_path, command="sleep 1; echo out; exit 3", **arm):
    """A gateway-armed background job (routing stamped, watcher registered) that has exited."""
    session = registry.spawn_local(command, cwd=str(tmp_path), session_key="agent:main:telegram:dm:1")
    for key, value in {"notify_on_complete": True, "watcher_interval": 5, "watcher_platform": "telegram",
                       "watcher_chat_id": "1", "parent_session_id": "sess-1", **arm}.items():
        setattr(session, key, value)
    assert session._completion_event.wait(30)
    return session


@pytest.mark.platforms("linux", "macos")
def test_finished_wake_persists_restores_once_and_ack_drops_it(checkpoint, tmp_path):
    from tools.process_registry import ProcessRegistry
    producer = ProcessRegistry()
    done = finish_armed(producer, tmp_path)
    [entry] = _entries(checkpoint)
    assert entry["session_id"] == done.id and entry["exited"] is True and entry["exit_code"] == 3
    assert entry["started_at"] == done.started_at and entry["watcher_chat_id"] == "1"

    boot = ProcessRegistry()
    assert boot.recover_from_checkpoint() == 0  # no live process adopted
    [watcher] = boot.pending_watchers
    assert watcher["session_id"] == done.id and watcher["notify_on_complete"] and watcher["chat_id"] == "1"
    replayed = boot.get(done.id)
    assert replayed.exited and replayed.exit_code == 3 and replayed.output_buffer == done.output_buffer
    assert replayed.output_buffer.startswith("out\n")
    assert replayed.started_at == done.started_at and replayed._completion_event.is_set()
    assert [e["session_id"] for e in _entries(checkpoint)] == [done.id]  # still unacked

    boot.mark_completion_delivered(done.id)
    boot.mark_completion_delivered(done.id)
    assert _entries(checkpoint) == []
    second = ProcessRegistry()
    second.recover_from_checkpoint()
    assert second.pending_watchers == []


@pytest.mark.platforms("linux", "macos")
def test_another_writer_keeps_an_unacked_wake(checkpoint, tmp_path):
    """A worker or a second interpreter rewriting the file from its own table must not drop it."""
    from tools.process_registry import ProcessRegistry
    done = finish_armed(ProcessRegistry(), tmp_path)
    ProcessRegistry()._write_checkpoint()
    assert [e["session_id"] for e in _entries(checkpoint)] == [done.id]


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("case", ["final_stop_kill", "consumed", "no_watcher", "not_notify"])
def test_completions_without_a_pending_wake_are_not_persisted(checkpoint, tmp_path, case):
    from tools.process_registry import ProcessRegistry
    registry = ProcessRegistry()
    if case == "final_stop_kill":
        session = registry.spawn_local("sleep 60", cwd=str(tmp_path))
        session.notify_on_complete, session.watcher_interval = True, 5
        registry.kill_all(source="gateway_shutdown")
    else:
        arm = {"no_watcher": {"watcher_interval": 0}, "not_notify": {"notify_on_complete": False}}.get(case, {})
        session = finish_armed(registry, tmp_path, **arm)
        if case == "consumed":
            registry.wait(session.id, timeout=5)
            registry._write_checkpoint()
    assert session.exited and _entries(checkpoint) == []


def test_boot_with_no_pending_wake_is_silent(checkpoint, caplog):
    from tools.process_registry import ProcessRegistry
    checkpoint.write_text("[]")
    registry = ProcessRegistry()
    with caplog.at_level(logging.INFO, logger="tools.process_registry"):
        assert registry.recover_from_checkpoint() == 0
    assert registry.pending_watchers == [] and registry._finished == {}
    assert not [r for r in caplog.records if "Replaying" in r.getMessage()]


def test_stale_wake_is_not_replayed(checkpoint):
    from tools.process_registry import ProcessRegistry
    old = time.time() - 49 * 3600
    checkpoint.write_text(json.dumps([{
        "session_id": "proc_old", "command": "x", "exited": True, "exited_at": old, "started_at": old,
        "exit_code": 0, "notify_on_complete": True, "watcher_interval": 5, "watcher_platform": "telegram"}]))
    registry = ProcessRegistry()
    registry.recover_from_checkpoint()
    assert registry.pending_watchers == [] and _entries(checkpoint) == []


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("read", ["wait", "read_log"])
def test_reading_the_result_drops_the_persisted_wake(checkpoint, tmp_path, read):
    """The agent consumed the result itself: a restart before the watcher's next tick must not replay it."""
    from tools.process_registry import ProcessRegistry
    registry = ProcessRegistry()
    done = finish_armed(registry, tmp_path)
    assert [e["session_id"] for e in _entries(checkpoint)] == [done.id]
    getattr(registry, read)(done.id, **({"timeout": 5} if read == "wait" else {}))
    assert _entries(checkpoint) == []
    boot = ProcessRegistry()
    boot.recover_from_checkpoint()
    assert boot.pending_watchers == []
