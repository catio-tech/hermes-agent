"""The shared "model went silent" fallback reply, split out of ``gateway/run_turn.py`` for the
code-health file-line cap (moved verbatim; behavior unchanged). Imported by the turn runner,
startup and persistence surfaces."""

from agent.i18n import t


def _unexpected_silence_reply() -> str:
    """Reply when the model returned only a silence marker for a message that needed an answer."""
    return t("gateway.errors.unexpected_silence")
