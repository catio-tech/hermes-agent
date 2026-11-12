"""The process watcher's ack of a checkpointed completion wake (``mark_completion_delivered``).

A watcher that returns has routed its completion for good, so the wake leaves processes.json. One
that is cancelled (a restart stops it mid-retry) or raises leaves the wake there for the next
gateway to replay. A refusal because this gateway is draining or stopping is not an outcome at all:
the watcher keeps waiting, without spending its retries or falling back to the plain notice, until
the stop cancels it.
"""
import logging

logger = logging.getLogger("gateway.run")


def refused_by_shutdown(runner) -> bool:
    """The refusal came from this gateway going down (restart drain or stop), not from the route."""
    return bool(getattr(runner, "_draining", False)
                or getattr(runner, "_completion_notification_batches_stopping", False))


async def run_watcher_with_ack(runner, watcher: dict) -> None:
    from tools.process_registry import process_registry
    session_id = watcher["session_id"]
    async with runner._completion_event_scope(watcher):
        try:
            await runner._watch_process(watcher)
        except Exception:
            logger.warning("Process watcher for %s failed; its completion stays undelivered until the "
                           "next gateway boot replays it", session_id, exc_info=True)
            raise
        process_registry.mark_completion_delivered(session_id)
