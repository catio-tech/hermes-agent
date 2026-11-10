"""Catio: per-chat-type quiet mode — ``display.quiet_group_chats``.

A group chat (gateway ``chat_type`` ``"group"`` — Slack channels, Telegram groups;
Slack MPIMs are stamped ``"dm"`` and keep DM rendering) posts ONLY the final reply
when ``display.quiet_group_chats`` is on:
tool progress (bubbles, previews, native task cards), interim assistant
messages and thinking progress go quiet, and the live typing status drops its
argument preview. One-to-one DMs (``chat_type`` ``"dm"``) resolve identically to
a world without the flag. Harness mirrors ``tests/gateway/test_run_progress_topics.py``.
"""

import importlib
from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class QuietModeCaptureAdapter(BasePlatformAdapter):
    def __init__(self, platform=Platform.SLACK):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent: list[dict] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="quiet-1")

    async def edit_message(self, chat_id, message_id, content, *, finalize: bool = False) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id) -> dict:
        return {"name": chat_id, "type": "group"}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


def _display_settings(monkeypatch, tmp_path, config_yaml: str):
    """``_run_agent_display_settings`` against a ``config.yaml`` written into a tmp Hermes home."""
    gateway_run = importlib.import_module("gateway.run")
    (tmp_path / "config.yaml").write_text(config_yaml)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "get_hermes_home_override", lambda: None)
    monkeypatch.delenv("HERMES_TOOL_PROGRESS_MODE", raising=False)


def _source(
    chat_type: str, chat_id: str, platform: Platform = Platform.SLACK, user_id: str | None = None,
    thread_id: str | None = None, parent_chat_id: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
        user_id=user_id, parent_chat_id=parent_chat_id,
    )


_QUIET_CONFIG = """
display:
  quiet_group_chats: true
  tool_progress: all
  interim_assistant_messages: true
  live_status: full
"""

_VERBOSE_CONFIG = """
display:
  tool_progress: all
  interim_assistant_messages: true
  live_status: full
"""

_PER_PLATFORM_CONFIG = """
display:
  tool_progress: all
  live_status: full
  platforms:
    slack:
      quiet_group_chats: true
"""


def test_quiet_group_chat_posts_only_the_final_reply(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, _QUIET_CONFIG)
    runner = _make_runner(QuietModeCaptureAdapter())
    disp = runner._run_agent_display_settings(_source("group", "C1"))
    # every mid-turn surface is off; the typing status keeps its verb, drops the argument preview
    assert disp.progress_mode == "off"
    assert disp.tool_progress_enabled is False
    assert disp.interim_assistant_messages_enabled is False
    assert disp._thinking_enabled is False
    assert disp._live_status_mode == "verb"
    assert disp.needs_progress_queue is False


def test_dm_keeps_full_rendering_when_quiet_group_chats_on(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, _QUIET_CONFIG)
    runner = _make_runner(QuietModeCaptureAdapter())
    disp = runner._run_agent_display_settings(_source("dm", "D1"))
    assert disp.progress_mode == "all"
    assert disp.tool_progress_enabled is True
    assert disp.interim_assistant_messages_enabled is True
    assert disp._live_status_mode == "full"


def test_group_chat_unchanged_when_flag_absent(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, _VERBOSE_CONFIG)
    runner = _make_runner(QuietModeCaptureAdapter())
    disp = runner._run_agent_display_settings(_source("group", "C1"))
    assert disp.progress_mode == "all"
    assert disp.tool_progress_enabled is True
    assert disp.interim_assistant_messages_enabled is True
    assert disp._live_status_mode == "full"


def test_quiet_flag_scopes_per_platform(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, _PER_PLATFORM_CONFIG)
    slack_runner = _make_runner(QuietModeCaptureAdapter(Platform.SLACK))
    telegram_runner = _make_runner(QuietModeCaptureAdapter(Platform.TELEGRAM))
    # the per-platform override quiets Slack group chats only
    assert slack_runner._run_agent_display_settings(
        _source("group", "C1", Platform.SLACK)).progress_mode == "off"
    assert telegram_runner._run_agent_display_settings(
        _source("group", "G1", Platform.TELEGRAM)).progress_mode == "all"


def test_explicit_off_intent_survives_in_group_chats(monkeypatch, tmp_path):
    """The quiet gate must read as explicit operator intent so the Slack native task-card
    lane dies too (the ``_tool_progress_explicit`` gate in ``_run_agent_display_settings``),
    exactly as a written ``tool_progress: off`` would."""
    _display_settings(monkeypatch, tmp_path, _QUIET_CONFIG)
    runner = _make_runner(QuietModeCaptureAdapter())
    disp = runner._run_agent_display_settings(_source("group", "C1"))
    assert disp._native_slack_task_cards is False
