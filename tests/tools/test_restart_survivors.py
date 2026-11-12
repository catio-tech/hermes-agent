"""A graceful gateway restart is not a final stop: restart-safe background jobs outlive it.

The gateway's shutdown sweep (``kill_all(source="gateway_shutdown")``) killed every job on each
SIGUSR1 drain-and-relaunch, so a long ``terminal(background=true)`` job and its armed completion
died with the old gateway although its systemd scope would have survived the unit's
``KillMode=mixed``. A restart now spares a job with its own scope and an output log, which the
successor re-adopts from the checkpoint; a final stop still kills everything (#41225), and a
scope-less job is still killed on restart. Real processes; the scope is faked only where
``systemd-run --user`` would wrap the command.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def gateway_registry(tmp_path, monkeypatch):
    """A registry inside a supervised gateway whose jobs get a (faked) systemd scope."""
    from tools import process_registry as pr
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(pr, "_build_systemd_scope_argv", lambda argv, unit_suffix: argv)
    monkeypatch.setattr(pr, "_stop_systemd_unit", lambda unit: True)
    registry = pr.ProcessRegistry()
    yield registry, monkeypatch, pr
    for session in list(registry._running.values()):
        registry.kill_process(session.id)


def _spawn_pair(registry, monkeypatch, pr, tmp_path):
    scoped = registry.spawn_local("sleep 60", cwd=str(tmp_path))
    with monkeypatch.context() as m:  # systemd-run --user unavailable: a degraded, scope-less job
        m.setattr(pr, "_systemd_run_user_scope_available", lambda: False)
        bare = registry.spawn_local("sleep 60", cwd=str(tmp_path))
    assert scoped.systemd_unit and scoped.output_log and not bare.systemd_unit and not bare.output_log
    return scoped, bare


@pytest.mark.platforms("linux")
def test_restart_sweep_spares_the_scoped_job_and_kills_the_bare_one(gateway_registry, tmp_path):
    registry, monkeypatch, pr = gateway_registry
    scoped, bare = _spawn_pair(registry, monkeypatch, pr, tmp_path)

    assert registry.kill_all(source="gateway_restart") == 1
    assert bare.exited and bare.termination_source == "gateway_restart"
    assert not scoped.exited and scoped.process.poll() is None
    # Agent close during the restart's finalization spares it too; an explicit kill still works.
    assert registry.kill_all(scoped.owner_task_id, source="agent_close") == 0
    assert registry.spared_by_restart(scoped.id)
    assert scoped.id in {e["session_id"] for e in json.loads((tmp_path / "processes.json").read_text())}
    assert registry.kill_process(scoped.id)["status"] == "killed"


@pytest.mark.platforms("linux")
def test_final_stop_kills_the_scoped_job_too(gateway_registry, tmp_path):
    registry, monkeypatch, pr = gateway_registry
    scoped, bare = _spawn_pair(registry, monkeypatch, pr, tmp_path)

    assert registry.kill_all(source="gateway_shutdown") == 2
    assert scoped.exited and bare.exited and scoped.termination_source == "gateway_shutdown"
    assert not registry.spared_by_restart(scoped.id)


@pytest.mark.platforms("linux")
def test_pty_job_keeps_its_pty_and_is_not_restart_safe(gateway_registry, tmp_path):
    pytest.importorskip("ptyprocess")
    registry, _monkeypatch, _pr = gateway_registry
    pty_job = registry.spawn_local("sleep 60", cwd=str(tmp_path), use_pty=True)
    assert pty_job._pty is not None and not pty_job.output_log
    assert registry.kill_all(source="gateway_restart") == 1 and pty_job.exited


@pytest.mark.platforms("linux")
def test_restart_safe_job_output_and_exit_outlive_the_gateway(tmp_path):
    """The spawning gateway dies at once (os._exit, no cleanup); the job keeps writing to its log
    and records its exit, and the successor's adoption collects both."""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "PYTHONPATH")}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(ROOT))
    command = "sleep 2; echo FIRST_$((40+2)); sleep 1; echo SECOND_$((6*7)); (exit 7)"
    spawner = textwrap.dedent(f"""
        import json, os
        import tools.process_registry as pr
        pr._is_supervised_gateway_process = lambda: True
        pr._systemd_run_user_scope_available = lambda: True
        pr._build_systemd_scope_argv = lambda argv, unit_suffix: argv
        session = pr.process_registry.spawn_local({command!r}, cwd={str(tmp_path)!r})
        assert session.output_log and session.systemd_unit, session
        print(json.dumps(session.id), flush=True)
        os._exit(0)
    """)
    out = subprocess.run([sys.executable, "-c", spawner], env=env, cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    session_id = json.loads(out.stdout.strip().splitlines()[-1])
    adopter = textwrap.dedent(f"""
        import json
        from tools.process_registry import ProcessRegistry
        registry = ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
        print(json.dumps(registry.wait({session_id!r}, timeout=30)))
    """)
    out = subprocess.run([sys.executable, "-c", adopter], env=env, cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=False)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["status"] == "exited", (result, out.stderr[-2000:])
    assert result["exit_code"] == 7 and result["output"] == "FIRST_42\nSECOND_42\n", result
    log = home / "logs" / "process-output" / f"{session_id}.log"
    assert log.read_text().endswith("SECOND_42\n") and log.with_suffix(".exit").read_text().strip() == "7"
