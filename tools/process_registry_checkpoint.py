"""Running-process checkpoint persistence and PID-safe recovery.

The checkpoint also keeps each finished, notify-armed completion its gateway watcher has not acked yet
(``awaits_wake``, stamped ``"exited": true``), so a restart between exit and delivery replays the wake
instead of losing it: boot restores it as finished and re-arms its watcher, which acks once routed.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")


class ProcessCheckpointMixin:
    def _detached_host_fate(self, pid: Optional[int], expected_start: Optional[int]) -> str:
        """Subclass supplies the live PID decision. See ProcessRegistry."""
        raise NotImplementedError

    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self, extra_entries: Optional[list[dict[str, Any]]] = None):
        """Write running process metadata to the checkpoint file atomically."""
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS
        from tools.environments.sandbox_scope import hosted_scope

        # A hosted caller only changes its own agent's records, which are never persisted here, so
        # it has no host change to write: it neither takes the lock nor rewrites this host's file.
        if hosted_scope() is not None:
            return
        try:
            path = _checkpoint_path()
            with self._lock:
                entries = []
                for s in self._running.values():
                    # A hosted agent's process is not this host's to persist: its record (command,
                    # cwd, owner) stays in memory, whoever triggers the write (scoped or not).
                    if s.exited or s.sandbox_key:
                        continue
                    # Backfill the start time so recovery can detect PID recycling
                    # even for sessions spawned before this field existed.
                    if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                        s.host_start_time = self._safe_host_start_time(s.pid)
                    entries.append(self._checkpoint_entry(s, _CHECKPOINT_FIELDS))
                entries.extend(
                    {**self._checkpoint_entry(s, _CHECKPOINT_FIELDS), "exited": True, "exited_at": s.exited_at,
                     **self._exit_fields(s)} for s in self._undelivered_wakes(str(path)))
                if extra_entries:
                    tracked_ids = {item.get("session_id") for item in entries}
                    entries.extend(item for item in extra_entries if item.get("session_id") not in tracked_ids)
                known = {*self._running, *self._finished}
            from hermes_cli.active_sessions import _FileLock
            from utils import atomic_json_write
            # Every interpreter of the profile (owner, each managed worker) rewrites this file from
            # its own table; a worker's output-log handoff is its only path to the next one, so a
            # writer keeps the handoffs it does not track (read-merge-write under one file lock).
            with _FileLock(path.with_name(path.name + ".lock")):
                entries.extend(self._foreign_handoffs(path, known))
                atomic_json_write(path, entries)
        except Exception as e:
            logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)

    @staticmethod
    def _checkpoint_entry(s, fields) -> dict[str, Any]:
        entry = {"session_id": s.id, **{f: getattr(s, f) for f in fields}}
        # Redact inline credentials before persisting (~/.hermes/processes.json).
        # Recovery uses command only for display (adoption re-validates the
        # PID, never re-runs it), so masking is lossless.
        # See #77484.
        entry["command"] = redact_sensitive_text(s.command, code_file=True)
        entry["owner_task_id"] = s.owner_task_id or s.task_id
        return entry

    def _undelivered_wakes(self, path: str) -> list:
        """This profile's unacked completions (hold ``_lock``). Only the owner's watchers ack, so a
        managed worker never persists them; a wake lives in the checkpoint it was first written to."""
        from agent.runtime_session_store import is_worker_process
        from tools.process_registry_awaiting import awaits_wake
        if is_worker_process():
            return []
        now = time.time()
        wakes = [s for s in self._finished.values() if awaits_wake(s, now) and s.id not in self._completion_consumed]
        for s in wakes:
            s._wake_checkpoint = s._wake_checkpoint or path
        return [s for s in wakes if s._wake_checkpoint == path]

    def _foreign_handoffs(self, path, known) -> list[dict[str, Any]]:
        """On-disk entries this registry never tracked that still decide an outcome: a worker handoff
        whose process is alive (same start time) or whose recorded exit is waiting to be read, and an
        unacked completion (another interpreter's write must not drop it before the owner replays it)."""
        from pathlib import Path
        from tools.async_delegation import _MAX_COMPLETION_REPLAY_AGE_S
        from tools.process_registry_output_log import _exit_path, adoptable_log
        try:
            on_disk = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        cutoff = time.time() - _MAX_COMPLETION_REPLAY_AGE_S
        return [entry for entry in on_disk if isinstance(entry, dict) and entry.get("session_id") not in known
                and ((entry.get("exited") and entry.get("exited_at", 0) > cutoff)
                     or ((log := adoptable_log(entry.get("output_log"))) and Path(log).exists()
                         and (_exit_path(log).exists() or self._detached_host_fate(
                             entry.get("pid"), entry.get("host_start_time")) == "running")))]

    def _consume_completion(self, session_id: str) -> None:
        """The agent read the result itself (wait/log): no wake is due any more, so the first read also
        drops a persisted one from the checkpoint (a restart must not replay a consumed completion)."""
        with self._lock:
            first = session_id not in self._completion_consumed
            self._completion_consumed.add(session_id)
            session = self._finished.get(session_id)
            persisted = first and session is not None and bool(session._wake_checkpoint)
        if persisted:
            self._write_checkpoint()

    def mark_completion_delivered(self, session_id: str) -> None:
        """The gateway watcher finished routing this completion (delivered, deduplicated, dropped as
        terminal or sent as the plain notice): drop its wake from the checkpoint. Idempotent."""
        with self._lock:
            session = self._finished.get(session_id)
            if session is None or session.completion_delivered:
                return
            session.completion_delivered = True
            persisted = bool(session._wake_checkpoint)
        if persisted:
            self._write_checkpoint()

    def _restore_wake(self, entry: dict[str, Any]) -> bool:
        """Rebuild an unacked completion as a finished session and re-arm its watcher. The producer
        identity (session_id, started_at) is the checkpointed one, so a wake the previous gateway
        admitted but never acked is recognised as delivered (session admission lost-ACK)."""
        from agent.runtime_session_store import is_worker_process
        from tools.process_registry import ProcessSession, _CHECKPOINT_FIELDS, _CHECKPOINT_DEFAULTS, _checkpoint_path
        from tools.process_registry_awaiting import awaits_wake
        from tools.process_registry_results import receipt_output
        with self._lock:
            known = entry.get("session_id") in {*self._running, *self._finished}
        if known or is_worker_process() or not entry.get("session_id"):
            return False
        fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
        fields.update(owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
                      started_at=entry.get("started_at", 0.0), exited=True, exited_at=entry.get("exited_at", 0.0),
                      **{f: entry.get(f, ProcessSession.__dataclass_fields__[f].default)
                         for f in ("exit_code", "completion_reason", "termination_source")})
        session = ProcessSession(id=entry["session_id"], detached=True, **fields)
        if not awaits_wake(session):
            return False  # aged out (or written by a build that did not stamp the arm state)
        session.output_buffer = receipt_output(session.id)
        session.total_output_chars = len(session.output_buffer)
        session._finish_claimed, session._wake_checkpoint = True, str(_checkpoint_path())
        session._completion_event.set()
        with self._lock:
            self._finished[session.id] = session
        self.pending_watchers.append(self._watcher_entry(session))
        return True

    @staticmethod
    def _watcher_entry(session) -> dict[str, Any]:
        from tools.process_registry import _WATCHER_ROUTE_KEYS
        return {
            "session_id": session.id,
            "check_interval": session.watcher_interval,
            "session_key": session.session_key,
            **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
            "notify_on_complete": session.notify_on_complete,
            "parent_session_id": session.parent_session_id,
        }

    def recover_from_checkpoint(self) -> int:
        """On gateway startup, probe PIDs from the checkpoint file; returns how many
        were recovered as detached sessions."""
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _checkpoint_path, _CHECKPOINT_DEFAULTS, _stop_systemd_unit,
        )
        from tools.process_registry_output_log import adoptable_log

        # This host adopts what a crashed predecessor left running, so the jobs it starts must
        # survive its own crash for the next one: they stay out of the owner-death supervisor.
        self._jobs_outlive_host = True
        checkpoint_path = _checkpoint_path()
        if not checkpoint_path.exists():
            return 0
        try:
            entries = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return 0
        recovered = replayed = 0
        unresolved_scope_entries: list[dict[str, Any]] = []
        for entry in entries:
            if entry.get("exited"):
                replayed += self._restore_wake(entry)
                continue
            pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
            if not pid:
                continue
            # The registry is process-global, so every profile's checkpoint carries every live
            # process; a multiplexer recovering several homes must adopt each session once.
            with self._lock:
                already_tracked = entry.get("session_id") in self._running
            if already_tracked:
                continue
            if pid_scope != "host":  # in-sandbox PIDs mean nothing once the env handle is gone
                logger.info(
                    "Skipping recovery for non-host process: %s (pid=%s, scope=%s)",
                    entry.get("command", "unknown")[:60], pid, pid_scope)
                continue
            # Alive and still ours: re-attach. A start-time probe that cannot
            # be read is not proof the PID was reused — dropping it would leave
            # a live child unsupervised, and marking it exited would invent a
            # completion. A positive mismatch means the number was recycled:
            # do not adopt it and do not signal it.
            fate = self._detached_host_fate(pid, entry.get("host_start_time"))
            # A worker spawn's output log and recorded exit outlive both the spawner and the PID,
            # so its reader decides the outcome (a recycled PID is never signalled: fate != running).
            log = adoptable_log(entry.get("output_log"))
            if log:
                fate = "running"
            if fate == "reused":
                logger.info(
                    "Not recovering session %s: pid %d is alive but its "
                    "start time no longer matches — PID was recycled onto "
                    "an unrelated process; refusing to adopt or signal it.",
                    entry.get("session_id", "?"), pid)
                continue
            if fate != "running":
                systemd_unit = entry.get("systemd_unit", "")
                if systemd_unit and not _stop_systemd_unit(systemd_unit):
                    logger.warning(
                        "Could not reap persisted scope %s for dead wrapper pid %s; "
                        "retaining checkpoint entry for the next startup",
                        systemd_unit, pid)
                    unresolved_scope_entries.append(entry)
                continue
            fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
            fields.update(
                command=entry.get("command", "unknown"),
                owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
                started_at=entry.get("started_at", time.time()), sandbox_key=entry.get("sandbox_key", ""))
            # detached: no Popen handle, but can report status + kill; a worker spawn's output log
            # (and recorded exit) outlives its spawner, so tail it from the start.
            fields["output_log"] = log
            session = ProcessSession(id=entry["session_id"], detached=True, **fields)
            if session.output_log:
                self._track_started(session, self._log_reader_loop, f"proc-log-reader-{session.id}")
            else:
                with self._lock:
                    self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
            # Re-enqueue watcher so gateway can resume notifications
            if session.watcher_interval > 0:
                self.pending_watchers.append(self._watcher_entry(session))
        if replayed:
            logger.info("Replaying %d undelivered background completion(s) from the checkpoint", replayed)
        self._write_checkpoint(extra_entries=unresolved_scope_entries)
        return recovered
