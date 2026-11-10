"""Catio: per-user / per-channel verbosity levels (``resolve_verbosity`` + ``!verbosity``).

Levels full > medium > low > minimal CAP the configured display knobs and never raise them.
``display.quiet_group_chats: true`` alone keeps today's behavior (alias for ``verbosity.group:
low``). In GROUP chats a user-originated layer never resolves above the non-user floor (channel →
chat-type → alias → full); DMs have no floor. Harness reused from ``test_quiet_group_chats.py``.
"""

import asyncio
import dataclasses
import importlib
import queue
import sys
import threading
import types
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml
from gateway.config import Platform
from gateway.display_config import LEVELS, resolve_verbosity
from gateway.platforms.event import MessageEvent
from gateway.turn_context import TurnContext
from tests.gateway.test_quiet_group_chats import (
    _QUIET_CONFIG, _VERBOSE_CONFIG, QuietModeCaptureAdapter, _display_settings, _make_runner, _source,
)


class _CardsAdapter(QuietModeCaptureAdapter):
    def native_task_cards_enabled(self):
        return True


def _settings(monkeypatch, tmp_path, config_yaml, source, adapter=None):
    _display_settings(monkeypatch, tmp_path, config_yaml)
    return _make_runner(adapter or _CardsAdapter(source.platform))._run_agent_display_settings(source)


def _resolve(config_yaml, source, platform_key="slack"):
    return resolve_verbosity(yaml.safe_load(config_yaml) or {}, platform_key, source)


def _fields(disp):
    """Every resolved knob (callables, queues and the raw config excluded)."""
    skip = {"user_config", "resolve_display_setting", "_display_surface_mode", "_generic_status_phrase",
            "log_queue", "_live_status_adapter"}
    return {f.name: getattr(disp, f.name) for f in dataclasses.fields(disp) if f.name not in skip}


# --- AC1 / AC2: zero-config backward compatibility ------------------------------------------------

_LEGACY_GROUP_FIELDS = {
    "progress_mode": "off", "tool_progress_enabled": False, "interim_assistant_messages_enabled": False,
    "_thinking_enabled": False, "_live_status_mode": "verb", "_progress_describe_enabled": False,
    "_native_slack_task_cards": False, "needs_progress_queue": False, "log_mode_enabled": False,
    "progress_names_only": False,
}


def test_quiet_alias_alone_keeps_todays_group_behavior_and_reports_its_layer(monkeypatch, tmp_path):
    src = _source("group", "C1", user_id="U1")
    disp = _settings(monkeypatch, tmp_path, _QUIET_CONFIG, src)
    assert {k: v for k, v in _fields(disp).items() if k in _LEGACY_GROUP_FIELDS} == _LEGACY_GROUP_FIELDS
    assert _resolve(_QUIET_CONFIG, src) == ("low", "quiet_group_chats")


def test_dm_under_quiet_alias_equals_a_world_without_the_flag(monkeypatch, tmp_path):
    src = _source("dm", "D1", user_id="U1")
    quiet = _fields(_settings(monkeypatch, tmp_path, _QUIET_CONFIG, src))
    plain = _fields(_settings(monkeypatch, tmp_path, _VERBOSE_CONFIG, src))
    assert quiet == plain
    assert _resolve(_QUIET_CONFIG, src) == ("full", "default")


# --- AC3: the ladder, one rung each ------------------------------------------------------------------

@pytest.mark.parametrize("config, source, expected", [
    pytest.param(  # pair beats channel (DM ladder)
        "display: {user_verbosity: {U1: {channels: {D1: minimal}}}, channel_verbosity: {D1: medium}}",
        _source("dm", "D1", user_id="U1"), ("minimal", "user+channel"), id="pair>channel"),
    pytest.param(  # channel beats user (DM ladder)
        "display: {user_verbosity: {U1: medium}, channel_verbosity: {D1: low}}",
        _source("dm", "D1", user_id="U1"), ("low", "channel"), id="channel>user"),
    pytest.param(  # user beats chat-type
        "display: {user_verbosity: {U1: medium}, verbosity: {dm: low}}",
        _source("dm", "D1", user_id="U1"), ("medium", "user"), id="user>chat-type"),
    pytest.param(  # chat-type beats the alias
        "display: {quiet_group_chats: true, verbosity: {group: medium}}",
        _source("group", "C1", user_id="U1"), ("medium", "chat-type"), id="chat-type>alias"),
    pytest.param(  # alias beats global
        "display: {quiet_group_chats: true}", _source("group", "C1"), ("low", "quiet_group_chats"),
        id="alias>global"),
    pytest.param("display: {}", _source("group", "C1", user_id="U1"), ("full", "default"), id="global"),
    pytest.param(  # a thread resolves through its parent channel
        "display: {channel_verbosity: {C1: medium}}",
        _source("group", "T1", user_id="U1", parent_chat_id="C1"), ("medium", "channel"), id="thread-parent"),
    pytest.param(  # a user's dict default applies where no pair matches
        "display: {user_verbosity: {U1: {default: medium, channels: {C9: low}}}}",
        _source("dm", "D1", user_id="U1"), ("medium", "user"), id="dict-default"),
    pytest.param(  # the user layer follows the turn sender only
        "display: {user_verbosity: {U2: minimal}}", _source("dm", "D1", user_id="U1"), ("full", "default"),
        id="other-user"),
])
def test_ladder(config, source, expected):
    assert _resolve(config, source) == expected


# --- AC11 (D1): group-chat floor ------------------------------------------------------------------

@pytest.mark.parametrize("display, chat_type, user_level, expected", [
    ("{quiet_group_chats: true}", "group", "full", "low"),
    ("{quiet_group_chats: true}", "group", "medium", "low"),
    ("{quiet_group_chats: true}", "group", "minimal", "minimal"),
    ("{quiet_group_chats: true, channel_verbosity: {C1: medium}}", "group", "full", "medium"),
    ("{quiet_group_chats: true, channel_verbosity: {C1: medium}}", "group", "low", "low"),
    ("{quiet_group_chats: true}", "dm", "medium", "medium"),
])
def test_group_floor(display, chat_type, user_level, expected):
    cfg = yaml.safe_load(f"display: {display}")
    cfg["display"]["user_verbosity"] = {"U1": user_level}
    assert resolve_verbosity(cfg, "slack", _source(chat_type, "C1", user_id="U1"))[0] == expected


def test_group_floor_also_caps_a_user_channel_pair():
    cfg = {"display": {"quiet_group_chats": True, "user_verbosity": {"U1": {"channels": {"C1": "full"}}}}}
    assert resolve_verbosity(cfg, "slack", _source("group", "C1", user_id="U1")) == ("low", "quiet_group_chats")


# --- AC4: per-platform scoping ---------------------------------------------------------------------

def test_channel_verbosity_scopes_per_platform():
    cfg = "display: {platforms: {slack: {channel_verbosity: {C1: minimal}}}}"
    assert _resolve(cfg, _source("group", "C1"), "slack") == ("minimal", "channel")
    assert _resolve(cfg, _source("group", "C1", Platform.TELEGRAM), "telegram") == ("full", "default")


# --- AC5 / AC12: level matrix ----------------------------------------------------------------------

_RICH_CONFIG = """
display:
  tool_progress: all
  interim_assistant_messages: true
  thinking_progress: true
  live_status: full
  progress_describe: true
  user_verbosity: {{U1: {level}}}
"""

_MATRIX = {  # mode, tool_progress, interim, thinking, live_status, describe, cards, names_only
    "full": ("all", True, True, True, "full", True, True, False),
    "medium": ("new", True, True, False, "verb", False, True, True),
    "low": ("off", False, False, False, "verb", False, False, False),
    "minimal": ("off", False, False, False, "off", False, False, False),
}


@pytest.mark.parametrize("level", LEVELS)
def test_level_matrix(monkeypatch, tmp_path, level):
    disp = _settings(monkeypatch, tmp_path, _RICH_CONFIG.format(level=level), _source("dm", "D1", user_id="U1"))
    assert (
        disp.progress_mode, disp.tool_progress_enabled, disp.interim_assistant_messages_enabled,
        disp._thinking_enabled, disp._live_status_mode, disp._progress_describe_enabled,
        disp._native_slack_task_cards, disp.progress_names_only,
    ) == _MATRIX[level]


def test_four_levels_are_first_class():
    assert LEVELS == ("full", "medium", "low", "minimal")
    cfg = {"display": {"user_verbosity": {"U1": "MINIMAL", "U2": "loud"}, "channel_verbosity": {"C1": "minimal"}}}
    assert resolve_verbosity(cfg, "slack", _source("dm", "D1", user_id="U1")) == ("minimal", "user")
    assert resolve_verbosity(cfg, "slack", _source("dm", "D1", user_id="U2")) == ("full", "default")  # invalid dropped
    assert resolve_verbosity(cfg, "slack", _source("group", "C1")) == ("minimal", "channel")


def test_medium_progress_names_the_tool_without_its_command():
    from gateway.run_turn_runner import TurnRunner
    ctx = TurnContext(source=_source("dm", "D1"), progress_names_only=True, progress_mode="new",
                      tool_progress_enabled=True, _run_still_current=lambda: True)
    runner = TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda s: None), ctx)
    line = runner._progress_build_message("terminal", "cat /etc/secret-path", {"command": "cat /etc/secret-path"})
    assert "secret-path" not in line and "```" not in line


# --- AC6: caps never raise -------------------------------------------------------------------------

def test_levels_never_raise_configured_values(monkeypatch, tmp_path):
    cfg = """
display:
  tool_progress: off
  interim_assistant_messages: false
  live_status: verb
  user_verbosity: {U1: full}
  channel_verbosity: {C1: full}
"""
    for src in (_source("dm", "D1", user_id="U1"), _source("group", "C1", user_id="U1")):
        disp = _settings(monkeypatch, tmp_path, cfg, src)
        assert (disp.progress_mode, disp.tool_progress_enabled) == ("off", False)
        assert disp._native_slack_task_cards is False  # written off is still explicit intent
        assert disp.interim_assistant_messages_enabled is False
        assert disp._live_status_mode == "verb"
    medium_new = "display: {tool_progress: new, user_verbosity: {U1: medium}}"
    assert _settings(monkeypatch, tmp_path, medium_new, _source("dm", "D1", user_id="U1")).progress_mode == "new"


# --- AC7: invariants at every level ----------------------------------------------------------------

def _turn_runner_at(monkeypatch, tmp_path, level, captured):
    from gateway.run_turn_runner import TurnRunner
    src = _source("group", "C1", user_id="U1")
    gw = _make_runner(_CardsAdapter())
    disp = _settings(monkeypatch, tmp_path, f"display: {{user_verbosity: {{U1: {level}}}, channel_verbosity: {{C1: {level}}}}}", src)

    async def _notice(source, content):
        captured.append(content)

    gw._deliver_platform_notice = _notice
    fields = {name: getattr(disp, name) for name in gw._DISPLAY_TO_TURN_CTX}
    fields["progress_queue"] = None if not disp.needs_progress_queue else queue.Queue()
    ctx = TurnContext(source=src, _run_still_current=lambda: True, _loop_for_step=None, **fields)
    return TurnRunner(gw, ctx)


@pytest.mark.parametrize("level", LEVELS)
def test_dead_delegation_notice_posts_at_every_level(monkeypatch, tmp_path, level):
    from gateway import run as run_mod
    captured: list[str] = []
    runner = _turn_runner_at(monkeypatch, tmp_path, level, captured)
    monkeypatch.setattr(run_mod, "safe_schedule_threadsafe", lambda coro, loop, **kw: asyncio.run(coro))
    runner.progress_callback("subagent.complete", preview="Error code: 404", status="failed", goal="scan")
    assert len(captured) == 1 and "404" in captured[0]


@pytest.mark.parametrize("level", LEVELS)
def test_clarify_reaches_send_clarify_at_every_level(monkeypatch, tmp_path, level):
    """Clarify never rides a display gate: no progress bubble for it, and the card goes to
    ``send_clarify`` whatever the level."""
    from gateway.platforms.base import SendResult
    from tools import clarify_gateway as cm
    captured: list[str] = []
    runner = _turn_runner_at(monkeypatch, tmp_path, level, captured)
    runner.progress_callback("tool.started", "clarify", "Pick?", {"question": "Pick?"})
    if runner._ctx.progress_queue is not None:
        assert runner._ctx.progress_queue.empty()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    cards = []

    class _Card(QuietModeCaptureAdapter):
        def pause_typing_for_chat(self, chat_id):
            return None

        def resume_typing_for_chat(self, chat_id):
            return None

        async def send_clarify(self, **kwargs):
            cards.append(kwargs)
            entry = cm.get_pending_for_session("sk-verbosity", include_choice_prompts=True)
            if entry is not None:
                cm.resolve_gateway_clarify(entry.clarify_id, "beta")
            return SendResult(success=True, message_id="card-1")

    try:
        runner._ctx._status_adapter, runner._ctx._status_chat_id = _Card(), "C1"
        runner._ctx._status_thread_metadata, runner._ctx.session_key = None, "sk-verbosity"
        runner._ctx._loop_for_step = loop
        runner._close_native_stream_boundary = lambda *a, **k: None
        monkeypatch.setattr(cm, "get_clarify_timeout", lambda: 5)
        response, answered = runner._ask_clarify_question("Pick?", ["alpha", "beta"], False)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
    assert len(cards) == 1
    assert (response, answered) == ("beta", True)


# --- AC9: heartbeat stays forced quiet -------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduled_heartbeat_stays_quiet_at_user_level_full(monkeypatch, tmp_path):
    from tests.gateway.test_run_progress_topics import (
        ProgressCaptureAdapter, SilentHeartbeatAgent, _make_runner as _progress_runner,
    )
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = SilentHeartbeatAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    adapter = ProgressCaptureAdapter()
    runner = _progress_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    (tmp_path / "config.yaml").write_text(
        "display: {tool_progress: all, user_verbosity: {U1: full}, channel_verbosity: {'123': full}}\n")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="U1",
                           thread_id="topic-7", message_id="m1")
    result = await runner._run_agent(
        message="scheduled heartbeat", context_prompt="", history=[], source=source,
        session_id="hb", session_key="agent:main:telegram:dm:123:topic-7", scheduled_heartbeat=True,
    )
    assert result["final_response"] == "[SILENT]"
    assert adapter.sent == []
    assert adapter.typing == []


# --- AC8: the !verbosity command -------------------------------------------------------------------

def _command(runner, text, source):
    return asyncio.run(runner._handle_verbosity_command(MessageEvent(text=text, source=source)))


def _raw(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}


def test_command_writes_user_level_and_next_turn_reads_it(monkeypatch, tmp_path):
    src = _source("group", "C1", user_id="U1")
    _display_settings(monkeypatch, tmp_path, "display: {tool_progress: all}\n")
    runner = _make_runner(_CardsAdapter())
    assert runner._run_agent_display_settings(src).progress_mode == "all"
    _command(runner, "/verbosity medium", src)
    assert _raw(tmp_path)["display"]["user_verbosity"] == {"U1": "medium"}
    disp = runner._run_agent_display_settings(src)  # no other mutation: the file is re-read per turn
    assert (disp.progress_mode, disp.progress_names_only) == ("new", True)


def test_command_here_writes_channel_level_for_the_parent_channel(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, "display: {}\n")
    runner = _make_runner(_CardsAdapter())
    _command(runner, "/verbosity minimal here", _source("group", "T1", user_id="U1", parent_chat_id="C1"))
    assert _raw(tmp_path)["display"]["channel_verbosity"] == {"C1": "minimal"}
    _command(runner, "/verbosity reset here", _source("group", "C1", user_id="U1"))
    assert _raw(tmp_path)["display"]["channel_verbosity"] == {}


def test_command_keeps_user_channel_pairs_and_resets(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, "display: {user_verbosity: {U1: {channels: {C1: low}}}}\n")
    runner = _make_runner(_CardsAdapter())
    src = _source("dm", "D1", user_id="U1")
    _command(runner, "/verbosity minimal", src)
    assert _raw(tmp_path)["display"]["user_verbosity"]["U1"] == {"channels": {"C1": "low"}, "default": "minimal"}
    _command(runner, "/verbosity reset", src)
    assert _raw(tmp_path)["display"]["user_verbosity"]["U1"] == {"channels": {"C1": "low"}}


def test_command_writes_into_an_existing_per_platform_map(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, "display: {platforms: {slack: {user_verbosity: {U9: low}}}}\n")
    runner = _make_runner(_CardsAdapter())
    _command(runner, "/verbosity medium", _source("dm", "D1", user_id="U1"))
    assert _raw(tmp_path)["display"]["platforms"]["slack"]["user_verbosity"] == {"U9": "low", "U1": "medium"}


def test_bare_command_reports_level_layer_and_four_levels(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, _QUIET_CONFIG)
    reply = _command(_make_runner(_CardsAdapter()), "/verbosity", _source("group", "C1", user_id="U1"))
    text = str(getattr(reply, "content", reply))
    assert "**low**" in text and "quiet_group_chats" in text and "not set" in text
    assert "full | medium | low | minimal" in text


def test_command_rejects_an_unknown_level(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, "display: {}\n")
    reply = _command(_make_runner(_CardsAdapter()), "/verbosity loud", _source("dm", "D1", user_id="U1"))
    assert "loud" in str(getattr(reply, "content", reply))
    assert "user_verbosity" not in (_raw(tmp_path).get("display") or {})


def test_here_follows_the_slash_acl(monkeypatch, tmp_path):
    _display_settings(monkeypatch, tmp_path, "display: {}\n")
    runner = _make_runner(_CardsAdapter())
    runner.config.platforms = {Platform.SLACK: SimpleNamespace(extra={"group_allow_admin_from": ["UADMIN"]})}
    _command(runner, "/verbosity low here", _source("group", "C1", user_id="U1"))
    assert "channel_verbosity" not in (_raw(tmp_path).get("display") or {})
    _command(runner, "/verbosity low here", _source("group", "C1", user_id="UADMIN"))
    assert _raw(tmp_path)["display"]["channel_verbosity"] == {"C1": "low"}


# --- AC10: Slack bang rewrite ----------------------------------------------------------------------

def test_slack_bang_rewrite_knows_verbosity():
    from plugins.platforms.slack.adapter import _rewrite_known_bang_command
    assert _rewrite_known_bang_command("!verbosity low here") == "/verbosity low here"


def test_verbosity_is_a_plain_gateway_command():
    from hermes_cli.commands import resolve_command
    gw = importlib.import_module("gateway.run").GatewayRunner
    assert "verbosity" in gw._PLAIN_COMMANDS
    assert resolve_command("verbosity").busy_policy == "dispatch"
