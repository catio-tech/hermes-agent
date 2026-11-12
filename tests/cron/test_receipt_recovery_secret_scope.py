"""Cron receipt recovery delivers inside the profile's secret scope on a multiplexed gateway.

Live crash, every boot and every tick after it: ``reconcile_pending`` composed a recovered Slack
``deliver=origin`` result with no secret scope installed, and the stale-thread check read
``SLACK_HOME_CHANNEL`` through ``get_secret``, which fails closed under multiplexing
(``UnscopedSecretError``). The receipt was deferred forever and its run never booked.
"""
from contextlib import asynccontextmanager
import json

import pytest

from agent import secret_scope
from cron import executions, jobs, scheduler_authority, scheduler_delivery
from hermes_cli import gateway_client

_ORIGIN = {"platform": "slack", "chat_id": "C0HOME", "thread_id": "1794333905.707479"}


@pytest.fixture
def multiplexed():
    token = secret_scope.set_multiplex_context(True)
    yield
    secret_scope.reset_multiplex_context(token)


def _peer(monkeypatch, job):
    class Peer:
        async def rpc(self, method, **params):
            return {"status": "terminal", "job": job, "result": [True, "document", "answer", None]}

    @asynccontextmanager
    async def connected():
        yield Peer()
    monkeypatch.setattr(gateway_client, "connect_gateway", connected)


def test_recovered_origin_delivery_resolves_inside_the_profile_scope(tmp_path, monkeypatch, multiplexed):
    from hermes_constants import get_hermes_home
    (get_hermes_home() / ".env").write_text("SLACK_HOME_CHANNEL=C0HOME\n")
    seen = []
    resolve = scheduler_delivery._get_home_target_chat_id
    monkeypatch.setattr(scheduler_delivery, "_get_home_target_chat_id",
                        lambda platform: seen.append(resolve(platform)) or seen[-1])
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="report", schedule="every 1h", deliver="origin", origin=_ORIGIN)
        execution = executions.create_execution(job["id"], source="test")
        executions.mark_execution_running(execution["id"])
        with executions._transaction() as conn:
            conn.execute("UPDATE executions SET process_id=?,pid=? WHERE id=?", ("old-owner", 999999, execution["id"]))
        journal = scheduler_authority.journal_path(job["id"], execution["id"])
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({"params": {"job_id": job["id"], "request_id": execution["id"],
                                                  "extra_prompt": None}, "receipt": None}))
        _peer(monkeypatch, job)
        scheduler_authority.reconcile_pending()
        assert not journal.exists(), "receipt recovery was deferred"
        assert executions.get_execution(execution["id"])["status"] == "completed"
    assert seen and set(seen) == {"C0HOME"}, seen  # read from the profile's .env, not refused


def test_stale_thread_check_keeps_the_origin_thread_when_unscoped(multiplexed):
    """Defence in depth: an unscoped read keeps the recorded thread (same chat) instead of crashing."""
    with pytest.raises(secret_scope.UnscopedSecretError):
        secret_scope.get_secret("SLACK_HOME_CHANNEL")
    assert scheduler_delivery._origin_thread_is_stale(dict(_ORIGIN)) is False
    assert scheduler_delivery._origin_delivery_thread(dict(_ORIGIN)) == _ORIGIN["thread_id"]
