"""Slack adapter routing onto the slack-sdk Agent Sessions API (agents.sessions.*)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod


@pytest.fixture(autouse=True)
def _pin_legacy_assistant_threads_api():
    """Pin the SDK capability probe to the legacy assistant.threads API and
    restore it afterwards, so tests that flip the cached flag on don't leak."""
    prev = _slack_mod._AGENT_SESSIONS_SUPPORTED
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    yield
    _slack_mod._AGENT_SESSIONS_SUPPORTED = prev


class TestAgentSessionsApiRouting:
    """slack-sdk 3.44.0 Agent Sessions API (assistant_view deprecation Feb 2027).

    When the installed slack-sdk ships agents.sessions.* typed methods, status
    and title calls route through them; older SDKs keep using the legacy
    assistant.threads.* methods (compat bridge on Slack's side).
    """

    def _adapter(self):
        config = PlatformConfig(enabled=True, token="xoxb-fake-token")
        a = SlackAdapter(config)
        a._app = MagicMock()
        a._app.client = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_typing_uses_agent_sessions_when_supported(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        # Catio (#110374): agents.sessions.setStatus accepts only the enum
        # (active|processing|suspended|closed); free text is rejected with
        # invalid_arguments, so working text maps to "processing".
        a._app.client.agents_sessions_setStatus.assert_called_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="processing",
        )
        a._app.client.assistant_threads_setStatus.assert_not_called()


    @pytest.mark.asyncio
    async def test_stop_typing_clears_via_agent_sessions(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.agents_sessions_setStatus.reset_mock()
        await a.stop_typing("C123", metadata={"thread_id": "parent_ts"})
        # Catio (#110374): the legacy empty-string clear is also rejected by the enum; the
        # clear maps to "active" (and "processing" does not auto-clear on reply, so an
        # unmapped clear would strand the spinner).
        a._app.client.agents_sessions_setStatus.assert_called_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="active",
        )
        a._app.client.assistant_threads_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_free_text_status_maps_to_the_enum_per_call(self):
        """Per-tool live status / typing_status_text phrases all collapse onto ``processing``
        on the Agent Sessions API — Slack renders its own generic loading UX for it."""
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        await a._set_thread_status("C123", "", "parent_ts", "is running pytest --max-turns=40", "probe")
        a._app.client.agents_sessions_setStatus.assert_called_once_with(
            channel_id="C123", thread_ts="parent_ts", status="processing")

    @pytest.mark.asyncio
    async def test_legacy_sdk_keeps_free_text_status(self):
        """slack-sdk < 3.44 keeps the legacy assistant.threads.setStatus: free text renders
        as-is there, so the mapping must not touch that path."""
        a = self._adapter()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a._set_thread_status("C123", "", "parent_ts", "is thinking...", "probe")
        a._app.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="C123", thread_ts="parent_ts", status="is thinking...")

    @pytest.mark.asyncio
    async def test_thread_title_uses_agents_sessions_rename(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock()
        a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "Summarize the incident")
        a._app.client.agents_sessions_rename.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            title="Summarize the incident",
        )
        a._app.client.assistant_threads_setTitle.assert_not_called()
