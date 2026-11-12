"""What a Slack user sees when they message Hermes while a gateway restart is pending.

Driven through the real ``SlackAdapter._handle_slack_message`` into a real GatewayRunner and
SessionAuthority over SQLite: during the restart wait the reply is the localized "saved" notice
(the message is committed for the next owner); a ``runtime_draining`` refusal outside that window
asks for a resend. Neither shows ``RuntimeStoreError``, ``runtime_draining`` or ``/reset`` (the
2026-11-12 incident text).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.i18n import t
from tests.gateway.test_slack_ignore_other_user_mentions import BOT_USER_ID  # installs the bolt mock
from tests.waiting import async_wait_until

_RAW = ('RuntimeStoreError', 'runtime_draining', '/reset')


async def _slack_owner(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from plugins.platforms.slack.adapter import SlackAdapter

    monkeypatch.setenv('SLACK_ALLOWED_USERS', 'U_HUMAN')
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    adapter = SlackAdapter(PlatformConfig(enabled=True, token='xoxb-fixture'))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = BOT_USER_ID
    adapter._running = True
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f'sent-{len(sent)}')
    monkeypatch.setattr(adapter, 'send', send)
    runner.adapters = {Platform.SLACK: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='slack-owner')
    runner._wire_adapter_handlers(adapter)
    return runner, adapter, authority, sent


def _restart_pending(runner, pending=True):
    from gateway.drain_control import open_restart_wait
    runner._restart_requested = pending
    runner._draining = True
    runner.session_runtime_descriptor = getattr(runner, 'session_runtime_descriptor', None) or {}
    runner.session_runtime_descriptor['state'] = 'draining'
    if pending:
        open_restart_wait(runner)


async def _dm(adapter, text, ts):
    event = {'channel': 'D0001', 'channel_type': 'im', 'user': 'U_HUMAN', 'text': text, 'ts': ts}
    with patch.object(adapter, '_resolve_user_name', new=AsyncMock(return_value='human')), \
            patch.object(adapter, '_fetch_thread_context', new=AsyncMock(return_value=None)), \
            patch.object(adapter, '_fetch_thread_parent_text', new=AsyncMock(return_value='')), \
            patch.object(adapter, '_has_active_session_for_thread', return_value=False):
        await adapter._handle_slack_message(event)


@pytest.mark.asyncio
async def test_slack_message_during_restart_wait_is_saved_and_says_so(monkeypatch):
    from hermes_state_runtime import list_session_admissions
    runner, adapter, authority, sent = await _slack_owner(monkeypatch)
    _restart_pending(runner)
    await _dm(adapter, 'are you there?', '1700000001.000100')
    await async_wait_until(lambda: sent, what='the Slack reply')
    assert sent == [t('gateway.busy.drain_saved')]
    assert all(bad not in sent[0] for bad in _RAW)
    rows = [row for sid in authority.sessions for row in list_session_admissions(authority.db, session_id=sid)]
    assert [row['status'] for row in rows] == ['queued']


@pytest.mark.asyncio
async def test_slack_runtime_draining_outside_the_window_asks_for_a_resend(monkeypatch):
    runner, adapter, _authority, sent = await _slack_owner(monkeypatch)
    _restart_pending(runner, pending=False)  # a plain stop: no successor, nothing is saved
    await _dm(adapter, 'are you there?', '1700000002.000100')
    await async_wait_until(lambda: sent, what='the Slack reply')
    assert sent == [t('gateway.busy.drain_not_saved', action=t('gateway.busy.action_shutting_down'))]
    assert all(bad not in sent[0] for bad in _RAW)
