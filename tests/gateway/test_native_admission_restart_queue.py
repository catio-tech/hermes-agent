"""A platform message sent while a gateway restart is pending is saved for the next owner, not lost.

During a graceful restart's after-turn wait (and ``stop()``'s gap until inbound fetching stops) the
draining owner still receives messages (Slack Bolt acks them before the listener runs). They used
to be refused with a raw ``RuntimeStoreError: runtime_draining``, and the platform never redelivers.
Now ``_admit_native`` commits them queued under ``native_queue_open``: the draining owner never
claims them, the user is told at once that the message is saved, and the next owner's boot recovery
runs each exactly once, in order. Real GatewayRunner, SessionAuthority, FIFO and SQLite; the turn
itself is a stand-in (no model).
"""
import asyncio
from datetime import UTC, datetime

import pytest

from agent.i18n import t
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_shared_busy_receive_loop import Adapter
from tests.waiting import async_wait_until


async def _owner(monkeypatch, instance_id, ran):
    """One gateway owner on the test's HERMES_HOME; its agent turn is a stand-in that records the text."""
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    runner = GatewayRunner(GatewayConfig())
    runner._running = True
    adapter = Adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, '_read_user_config', lambda: {'approvals': {'destructive_slash_confirm': False}})

    async def agent_turn(event, source, session_entry, session_key, *args):
        ran.append((instance_id, event.text))
        reply = f'answer to {event.text}'
        execution_result.get()['result'] = {'final_response': reply, 'messages': []}
        return reply, None
    monkeypatch.setattr(runner, '_hmwa_prepare_turn', agent_turn)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id=instance_id)
    runner._wire_adapter_handlers(adapter)
    return runner, adapter, authority


def _begin_restart(runner):
    """What ``request_restart`` publishes before its after-turn wait (run_shutdown.py)."""
    from gateway.drain_control import open_restart_wait
    runner._restart_requested = True
    runner._draining = True
    if getattr(runner, 'session_runtime_descriptor', None) is None:
        runner.session_runtime_descriptor = {}
    runner.session_runtime_descriptor['state'] = 'draining'
    open_restart_wait(runner)


def _rows(authority):
    rows = (row for sid in authority.sessions
            for row in list_session_admissions(authority.db, session_id=sid, pending_only=False))
    return [(row['request_id'], row['status']) for row in sorted(rows, key=lambda row: row['seq'])]


_SENT_AT = datetime(2026, 11, 12, 21, 26, tzinfo=UTC)


def _event(source, mid, text=None):
    # A platform redelivery carries the original send time, so a resend has the same payload.
    return MessageEvent(text=text or mid, source=source, message_id=mid, timestamp=_SENT_AT)


async def _send(adapter, source, mid, text=None):
    await adapter.handle_message(_event(source, mid, text))


async def _send_settled(adapter, authority, source, mid, text=None):
    """Send once the chat is idle and wait for the commit: one message after another, as a person
    types them (an input landing on a busy session takes the adapter's debounced burst path)."""
    await async_wait_until(lambda: not adapter._active_sessions, what='the chat idle')
    await _send(adapter, source, mid, text)
    await async_wait_until(lambda: mid in [m for m, _status in _rows(authority)], what=f'{mid} committed')


@pytest.mark.asyncio
async def test_round_trip_queued_during_restart_runs_once_after_boot(monkeypatch):
    """AC1: two messages in one chat during the wait are saved queued, never run by the draining
    owner, then run by the next owner exactly once, in commit order; a redelivery is reconciled."""
    from gateway.run_runtime import drain_gateway_runtime, recover_profile_native_sessions
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    source = adapter.build_source(chat_id='chat', chat_type='dm', user_id='fixture-user')
    await _send(adapter, source, 'm0')
    await async_wait_until(lambda: adapter.sent == ['answer to m0'], what='the pre-restart reply')
    _begin_restart(runner)
    await _send_settled(adapter, authority, source, 'm1', 'first saved message')
    await _send_settled(adapter, authority, source, 'm2', 'second saved message')
    await asyncio.sleep(0.2)  # the draining owner's FIFO had its chance to (wrongly) claim them
    assert _rows(authority)[1:] == [('m1', 'queued'), ('m2', 'queued')]
    assert ran == [('a', 'm0')]
    # Told at once (not after an in-flight turn), once per pause episode, and never a raw error.
    assert adapter.sent == ['answer to m0', t('gateway.busy.drain_saved')]
    await drain_gateway_runtime(runner)  # the real stop: inbound stops, handlers withdrawn

    ran_b = []
    runner_b, adapter_b, authority_b = await _owner(monkeypatch, 'b', ran_b)
    outcome = await recover_profile_native_sessions(runner_b, authority_b)
    assert set(outcome.values()) == {'ready'}
    await async_wait_until(lambda: len(adapter_b.sent) == 2, what='both recovered replies')
    assert [text for _owner_id, text in ran_b] == ['first saved message', 'second saved message']
    assert adapter_b.sent == ['answer to first saved message', 'answer to second saved message']
    # The platform redelivers m1 to the new owner: same message id, same request id, no third run.
    redelivered = adapter_b.build_source(chat_id='chat', chat_type='dm', user_id='fixture-user')
    await _send(adapter_b, redelivered, 'm1', 'first saved message')
    await asyncio.sleep(0.3)
    assert len(ran_b) == 2 and len(adapter_b.sent) == 2  # no third run, and no error notice either
    assert [mid for mid, _status in _rows(authority_b)].count('m1') == 1


@pytest.mark.asyncio
async def test_first_message_of_new_thread_during_restart_is_recovered(monkeypatch):
    """AC1b: a chat this gateway never saw reserves its route during the window and recovers at boot."""
    from gateway.run_runtime import recover_profile_native_sessions
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    _begin_restart(runner)
    source = adapter.build_source(chat_id='brand-new', chat_type='dm', user_id='fixture-user')
    await _send(adapter, source, 'n1', 'hello from a new thread')
    await async_wait_until(lambda: _rows(authority) == [('n1', 'queued')], what='the first message saved')
    await async_wait_until(lambda: adapter.sent == [t('gateway.busy.drain_saved')], what='the saved notice')
    assert ran == []

    ran_b = []
    runner_b, adapter_b, authority_b = await _owner(monkeypatch, 'b', ran_b)
    assert set((await recover_profile_native_sessions(runner_b, authority_b)).values()) == {'ready'}
    await async_wait_until(lambda: adapter_b.sent == ['answer to hello from a new thread'], what='the reply')
    assert ran_b == [('b', 'hello from a new thread')]


@pytest.mark.asyncio
async def test_queue_window_closes_after_inbound_stop(monkeypatch):
    """AC1c: stop() drops the interactive ``drain_reason`` first, but a message the fetcher still
    delivers before inbound stops is saved; after the close a native admission is refused."""
    from gateway.drain_control import restart_wait_open
    from gateway.run_runtime import drain_gateway_runtime
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    _begin_restart(runner)
    source = adapter.build_source(chat_id='gap', chat_type='dm', user_id='fixture-user')
    seen = []

    async def stop_inbound():
        seen.append(restart_wait_open(runner))  # False: only the native window is still open
        await adapter.handle_message(_event(source, 'g1', 'sent in the gap'))
        # The fetcher delivered it well before its stop returned: the commit lands inside the window.
        await async_wait_until(lambda: _rows(authority) == [('g1', 'queued')], what='the gap message saved')
    adapter.stop_inbound = stop_inbound
    await drain_gateway_runtime(runner)
    assert seen == [False]
    await async_wait_until(lambda: adapter.sent == [t('gateway.busy.drain_saved')], what='the saved notice')
    assert ran == [] and runner._native_queue_closed
    with pytest.raises(RuntimeStoreError, match='runtime_draining'):
        await authority.admit_native(_event(source, 'g2', 'after the close'))
    assert _rows(authority) == [('g1', 'queued')]


@pytest.mark.asyncio
async def test_close_racing_commit_is_refused_inside_the_write(monkeypatch):
    """§5: the window closing while the write waits on the writer refuses it in the transaction."""
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    _begin_restart(runner)
    del runner.session_runtime_descriptor['drain_reason']  # stop() began: only the native window is open
    real_gate = authority._admission_gate

    def gate(*args, **kwargs):
        guard = real_gate(*args, **kwargs)

        def closing(conn):
            runner._native_queue_closed = True  # inbound stopped while this write queued
            return guard(conn)
        return closing
    monkeypatch.setattr(authority, '_admission_gate', gate)
    source = adapter.build_source(chat_id='race', chat_type='dm', user_id='fixture-user')
    with pytest.raises(RuntimeStoreError, match='runtime_draining'):
        await authority.admit_native(_event(source, 'r1'))
    assert _rows(authority) == []


@pytest.mark.asyncio
async def test_not_draining_path_identical(monkeypatch):
    """AC3: with no drain the predicate is False and the caller still awaits the turn's reply."""
    from gateway import drain_control
    verdicts = []
    real = drain_control.native_queue_open

    def spy(runner):
        verdicts.append(real(runner))
        return verdicts[-1]
    monkeypatch.setattr(drain_control, 'native_queue_open', spy)
    ran = []
    _runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    source = adapter.build_source(chat_id='hot', chat_type='dm', user_id='fixture-user')
    await _send(adapter, source, 'h1', 'hello')
    await async_wait_until(lambda: adapter.sent == ['answer to hello'], what='the awaited reply')
    assert verdicts and not any(verdicts)
    assert ran == [('a', 'hello')] and _rows(authority) == [('h1', 'terminal')]


@pytest.mark.asyncio
@pytest.mark.parametrize('planned_stop', [False, True], ids=['plain-stop', 'planned-stop-overrides-restart'])
async def test_plain_stop_does_not_queue(monkeypatch, planned_stop):
    """AC3: a stop with no successor (a plain stop, or a planned stop that cancelled a pending
    restart) keeps the plain refusal; the user is asked to resend, never shown the raw error."""
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    _begin_restart(runner)
    if planned_stop:
        from gateway.run_shutdown_kill import cancel_pending_restart
        cancel_pending_restart(runner)
    else:
        runner._restart_requested = False
    source = adapter.build_source(chat_id='stop', chat_type='dm', user_id='fixture-user')
    with pytest.raises(RuntimeStoreError, match='runtime_draining'):
        await authority.admit_native(_event(source, 's1'))
    await _send(adapter, source, 's2')
    await async_wait_until(lambda: adapter.sent, what='the refusal notice')
    notice = t('gateway.busy.drain_not_saved', action=t('gateway.busy.action_shutting_down'))
    assert adapter.sent == [notice]
    assert all(bad not in notice for bad in ('RuntimeStoreError', 'runtime_draining', '/reset'))
    assert _rows(authority) == [] and ran == []


@pytest.mark.asyncio
async def test_crash_after_commit_recovers(monkeypatch):
    """AC4: the draining owner dies after the commit (no stop, no settle); the next boot runs the row."""
    from gateway.run_runtime import recover_profile_native_sessions
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    _begin_restart(runner)
    source = adapter.build_source(chat_id='crash', chat_type='dm', user_id='fixture-user')
    await _send(adapter, source, 'c1', 'saved before the crash')
    await async_wait_until(lambda: _rows(authority) == [('c1', 'queued')], what='the commit')
    ran_b = []
    runner_b, adapter_b, authority_b = await _owner(monkeypatch, 'b', ran_b)
    await recover_profile_native_sessions(runner_b, authority_b)
    await async_wait_until(lambda: adapter_b.sent == ['answer to saved before the crash'], what='the reply')
    assert ran == [] and ran_b == [('b', 'saved before the crash')]


@pytest.mark.asyncio
async def test_boot_replay_races_live_message_preserves_order(monkeypatch):
    """§5: a live message the next owner admits before its boot recovery runs after the saved ones."""
    from gateway.run_runtime import recover_profile_native_sessions
    ran = []
    runner, adapter, authority = await _owner(monkeypatch, 'a', ran)
    source = adapter.build_source(chat_id='order', chat_type='dm', user_id='fixture-user')
    _begin_restart(runner)
    await _send_settled(adapter, authority, source, 'o1', 'saved one')
    await _send_settled(adapter, authority, source, 'o2', 'saved two')
    ran_b = []
    runner_b, adapter_b, authority_b = await _owner(monkeypatch, 'b', ran_b)
    live_source = adapter_b.build_source(chat_id='order', chat_type='dm', user_id='fixture-user')
    live = asyncio.create_task(_send(adapter_b, live_source, 'o3', 'live three'))
    await recover_profile_native_sessions(runner_b, authority_b)
    await live
    await async_wait_until(lambda: len(ran_b) == 3, what='all three run')
    assert [text for _owner_id, text in ran_b] == ['saved one', 'saved two', 'live three']
    await async_wait_until(lambda: len(adapter_b.sent) == 3, what='all three replies')
    assert sorted(adapter_b.sent) == ['answer to live three', 'answer to saved one', 'answer to saved two']


@pytest.mark.asyncio
async def test_notify_turn_error_maps_runtime_draining():
    """AC2 (base adapter): a residual ``runtime_draining`` gets the localized resend notice; any other
    error keeps the detailed turn-error notice."""
    adapter = Adapter()
    source = adapter.build_source(chat_id='err', chat_type='dm', user_id='fixture-user')
    await adapter._notify_turn_error(_event(source, 'e1'), RuntimeStoreError('runtime_draining'))
    await adapter._notify_turn_error(_event(source, 'e2'), RuntimeError('boom'))
    drained, other = adapter.sent
    assert drained == t('gateway.busy.drain_not_saved', action=t('gateway.busy.action_shutting_down'))
    assert all(bad not in drained for bad in ('RuntimeStoreError', 'runtime_draining', '/reset'))
    assert 'boom' in other or other == t('gateway.notify.turn_error')
