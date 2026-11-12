"""Background-job sweep of a gateway stop: a graceful restart is not a final stop.

A final stop (``hermes gateway stop``, SIGTERM) kills every job, even ``persist_on_release`` ones, or
they would become PPID=1 orphans (#41225/#46778). A restart hands the jobs that can outlive this
process (``survives_host_restart``: own systemd scope, output log, host PID) to the successor, which
re-adopts them from the checkpoint; every other job is killed as before.
"""
import logging

logger = logging.getLogger("gateway.run")


def begin_restart_handoff() -> None:
    """Spare restart-safe jobs from here on: agent close during finalization would kill them too."""
    from tools.process_registry import process_registry
    process_registry.begin_restart_handoff()


def kill_background_processes(phase: str, restart: bool) -> None:
    from tools.process_registry import process_registry
    from tools.process_registry_ownership import survives_host_restart

    killed = process_registry.kill_all(source="gateway_restart" if restart else "gateway_shutdown")
    if killed:
        logger.info("Shutdown (%s): killed %d tool subprocess(es)", phase, killed)
    if restart and (left := sum(map(survives_host_restart, list(process_registry._running.values())))):
        logger.info("Shutdown (%s): left %d restart-safe job(s) for the successor", phase, left)
