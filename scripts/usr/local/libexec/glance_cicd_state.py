#!/usr/bin/env python3
"""
Purpose:
    Provide durable Glance blue-green deployment checkpoint operations.

Input:
    Deployment identifiers, deadlines and a caller-supplied verification
    callback, with state stored under /var/lib/glance-cicd.

Output:
    Atomic pending.json creation, confirmed history and safe finalization.

Safety:
    All mutations require the shared deployment.lock.
    This module never contacts Docker, NPM or external services.
    The caller must verify candidate health and routing before confirmation.
"""

import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_STATE = Path("/var/lib/glance-cicd")

HOST_ID = 198
DOMAIN = "glance.blinkwired.com"
BLUE = "glance"
GREEN = "glance-green-cicd"

IDENTITY = (
    "deployment_id",
    "host_id",
    "domain",
    "previous_upstream",
    "candidate_upstream",
    "deadline_epoch",
)


def sync_directory(path):
    fd = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY |
        os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def present(path):
    return os.path.lexists(path)


def load_json(path):
    fd = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        data = json.load(source)

    if not isinstance(data, dict):
        raise RuntimeError("Invalid checkpoint document")

    return data


def same_identity(left, right):
    return all(left.get(key) == right.get(key) for key in IDENTITY)


class StateStore:
    def __init__(self, root=DEFAULT_STATE):
        self.root = Path(root)
        self.pending = self.root / "pending.json"
        self.history = self.root / "history"
        self.lock_path = self.root / "deployment.lock"
        self._held = False

    def _require_lock(self):
        if not self._held:
            raise RuntimeError("Deployment lock required")

    @contextmanager
    def locked(self, blocking=True):
        if self._held:
            raise RuntimeError("Nested deployment lock")

        if self.root.is_symlink():
            raise RuntimeError("Unsafe state directory")

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

        info = os.stat(self.root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RuntimeError("Unsafe state directory permissions")

        fd = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT |
            os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )

        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
            ):
                raise RuntimeError("Unsafe lock file")

            os.fchmod(fd, 0o600)

            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB

            fcntl.flock(fd, flags)
            self._held = True

            try:
                yield self
            finally:
                self._held = False
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _atomic_json(self, target, data):
        self._require_lock()

        if present(target):
            raise RuntimeError(f"Checkpoint already exists: {target.name}")

        temporary = target.parent / (
            f".{target.name}.{uuid.uuid4().hex}.tmp"
        )

        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())

            os.replace(temporary, target)
            sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _ensure_history(self):
        self._require_lock()

        if self.history.is_symlink():
            raise RuntimeError("Unsafe history directory")

        existed = self.history.exists()
        self.history.mkdir(mode=0o700, exist_ok=True)

        info = os.stat(self.history, follow_symlinks=False)

        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RuntimeError("Unsafe history directory permissions")

        if not existed:
            sync_directory(self.root)

    def read_pending(self):
        self._require_lock()

        if not present(self.pending):
            return None

        data = load_json(self.pending)

        if (
            data.get("status") != "pending"
            or data.get("host_id") != HOST_ID
            or data.get("domain") != DOMAIN
            or data.get("previous_upstream") != BLUE
            or data.get("candidate_upstream") != GREEN
            or not isinstance(data.get("deployment_id"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}",
                data["deployment_id"],
            )
            or type(data.get("deadline_epoch")) not in (int, float)
            or not math.isfinite(data["deadline_epoch"])
        ):
            raise RuntimeError("Unexpected pending checkpoint")

        return data

    def begin(self, deployment_id, ttl_seconds=600):
        self._require_lock()

        if not isinstance(deployment_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,80}", deployment_id
        ):
            raise ValueError("Invalid deployment identifier")

        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 3600:
            raise ValueError("TTL must be 60–3600 seconds")

        if present(self.pending):
            raise RuntimeError("Another deployment is pending")

        history_path = self.history / f"{deployment_id}.json"

        if present(history_path):
            raise RuntimeError("Deployment identifier already used")

        record = {
            "deployment_id": deployment_id,
            "host_id": HOST_ID,
            "domain": DOMAIN,
            "previous_upstream": BLUE,
            "candidate_upstream": GREEN,
            "status": "pending",
            "deadline_epoch": time.time() + ttl_seconds,
        }

        self._atomic_json(self.pending, record)

        return record

    def _finish_pending(self):
        self._require_lock()
        self.pending.unlink()
        sync_directory(self.root)

    def confirm(self, deployment_id, verifier):
        self._require_lock()

        if not callable(verifier):
            raise ValueError("Verification callback required")

        pending = self.read_pending()

        if pending is None:
            raise RuntimeError("No pending deployment")

        if pending["deployment_id"] != deployment_id:
            raise RuntimeError("Deployment identifier mismatch")

        target = self.history / f"{deployment_id}.json"

        # Resume a confirmation that was durably recorded but
        # interrupted before pending.json was removed.
        if present(target):
            previous = load_json(target)

            if (
                previous.get("status") != "confirmed"
                or not same_identity(previous, pending)
            ):
                raise RuntimeError("Conflicting deployment history")

            self._finish_pending()
            return previous

        if time.time() >= pending["deadline_epoch"]:
            raise RuntimeError("Confirmation deadline expired")

        # The future controller supplies verified routing and health
        # evidence while holding this same deployment lock.
        if verifier(dict(pending)) is not True:
            raise RuntimeError("Deployment verification failed")

        # Verification may take time. Never confirm after expiry.
        if time.time() >= pending["deadline_epoch"]:
            raise RuntimeError("Confirmation deadline expired")

        self._ensure_history()

        record = dict(pending)
        record["status"] = "confirmed"
        record["completed_epoch"] = time.time()

        self._atomic_json(target, record)

        # History is durable before pending state is removed.
        self._finish_pending()

        return record


# V2 PREPARATION — NOT YET ENABLED IN PRODUCTION
#
# Keep the v1 StateStore API unchanged until the controller,
# verification layer and rollback guard migrate together.
import glance_cicd_slot_protocol as slot_protocol


class V2StateStore(StateStore):
    """Crash-recoverable two-slot state using the existing lock.

    This class does not communicate with Docker or NPM.
    Production callers must supply independently verified evidence.
    """

    def __init__(self, root=DEFAULT_STATE):
        super().__init__(root)
        self.active = self.root / "active.json"

    def read_active_v2(self):
        self._require_lock()

        if not present(self.active):
            return None

        info = os.stat(self.active, follow_symlinks=False)

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("Unsafe active-slot state")

        data = load_json(self.active)
        slot_protocol.validate_active(data)
        return data

    def _replace_active_v2(self, expected, updated):
        self._require_lock()

        current = self.read_active_v2()

        if current != expected:
            raise RuntimeError("Active-slot state changed")

        slot_protocol.validate_active(updated)

        temporary = self.root / (
            f".active.json.{uuid.uuid4().hex}.tmp"
        )

        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(updated, out, indent=2, allow_nan=False)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())

            # The shared lock serializes cooperating writers.
            if self.read_active_v2() != expected:
                raise RuntimeError("Active-slot state drift")

            os.replace(temporary, self.active)
            sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def bootstrap_v2(self, routes):
        """Explicit Blue-only genesis; never implicit during deploy."""
        self._require_lock()

        if present(self.pending):
            raise RuntimeError("Cannot bootstrap with pending state")

        if present(self.active):
            raise RuntimeError("Active state already initialized")

        active = slot_protocol.genesis_blue(
            routes,
            no_pending=True,
        )

        self._atomic_json(self.active, active)
        return active

    def begin_v2(self, deployment_id, *, now, ttl_seconds=600):
        """Durably publish pending before any external NPM write."""
        self._require_lock()

        active = self.read_active_v2()

        if active is None:
            raise RuntimeError("V2 active state not initialized")

        if present(self.pending):
            raise RuntimeError("Another deployment is pending")

        pending = slot_protocol.begin_transition(
            active,
            deployment_id,
            now=now,
            ttl_seconds=ttl_seconds,
        )

        self._ensure_history()

        if present(self.history / f"{deployment_id}.json"):
            raise RuntimeError("Deployment identifier already used")

        self._atomic_json(self.pending, pending)
        return pending

    def _pending_v2(self):
        self._require_lock()

        if not present(self.pending):
            raise RuntimeError("No pending deployment")

        pending = load_json(self.pending)

        if pending.get("schema") != slot_protocol.SCHEMA:
            raise RuntimeError("Unsupported pending state version")

        return pending

    def _confirmed_record_v2(self, pending):
        self._require_lock()

        path = self.history / f"{pending['deployment_id']}.json"
        record = load_json(path)

        if record.get("status") != "confirmed":
            raise RuntimeError("Conflicting deployment history")

        for key, value in pending.items():
            if key == "status":
                continue
            if record.get(key) != value:
                raise RuntimeError("Conflicting confirmed identity")

        completed = record.get("completed_epoch")

        if (
            type(completed) not in (int, float)
            or not math.isfinite(completed)
        ):
            raise RuntimeError("Invalid confirmed timestamp")

        return record

    def finalize_confirmed_v2(self):
        """Replay a durable confirmation without redoing NPM writes.

        A future v2 guard must call this before deadline rollback.
        """
        self._require_lock()

        pending = self._pending_v2()
        record = self._confirmed_record_v2(pending)
        active = self.read_active_v2()

        if active is None:
            raise RuntimeError("Missing active-slot state")

        predecessor = {
            "schema": slot_protocol.SCHEMA,
            "active_slot": pending["previous_slot"],
            "generation": pending["generation"] - 1,
            "deployment_id": None,
        }

        slot_protocol.validate_pending(predecessor, pending)

        updated = {
            "schema": slot_protocol.SCHEMA,
            "active_slot": pending["candidate_slot"],
            "generation": pending["generation"],
            "deployment_id": pending["deployment_id"],
        }

        if active == updated:
            pass
        elif (
            active["active_slot"] == predecessor["active_slot"]
            and active["generation"] == predecessor["generation"]
        ):
            self._replace_active_v2(active, updated)
        else:
            raise RuntimeError(
                "Conflicting active-slot confirmation state"
            )

        self._finish_pending()
        return record

    def confirm_v2(
        self,
        deployment_id,
        *,
        routes,
        healthy_slots,
        verifier,
        clock=time.time,
    ):
        """Confirm under lock, with durable history as commit point.

        The callback must independently verify live NPM and health.
        """
        self._require_lock()

        if not callable(verifier) or not callable(clock):
            raise ValueError("Verifier and clock required")

        pending = self._pending_v2()

        if pending.get("deployment_id") != deployment_id:
            raise RuntimeError("Deployment identifier mismatch")

        self._ensure_history()

        history_path = self.history / f"{deployment_id}.json"

        # Once history commits confirmation, never reinterpret
        # expiry as a reason to undo the committed decision.
        if present(history_path):
            return self.finalize_confirmed_v2()

        active = self.read_active_v2()

        if active is None:
            raise RuntimeError("Missing active-slot state")

        slot_protocol.validate_pending(active, pending)

        if verifier(dict(pending)) is not True:
            raise RuntimeError("Live deployment verification failed")

        # Recheck deadline after potentially lengthy verification.
        updated, record = slot_protocol.confirm_transition(
            active,
            pending,
            routes=routes,
            healthy_slots=healthy_slots,
            now=clock(),
        )

        self._atomic_json(history_path, record)

        # Crash recovery replays these two remaining steps.
        self._replace_active_v2(active, updated)
        self._finish_pending()

        return record

    def record_rollback_v2(
        self,
        *,
        routes,
        healthy_slots,
        verifier,
        clock=time.time,
    ):
        """Finalize only AFTER a future guard restores and verifies NPM."""
        self._require_lock()

        if not callable(verifier) or not callable(clock):
            raise ValueError("Verifier and clock required")

        active = self.read_active_v2()

        if active is None:
            raise RuntimeError("Missing active-slot state")

        pending = self._pending_v2()
        slot_protocol.validate_pending(active, pending)

        if routes.get(slot_protocol.HOST_ID) != pending["previous_upstream"]:
            raise RuntimeError("Previous upstream not restored")

        if verifier(dict(pending)) is not True:
            raise RuntimeError("Live rollback verification failed")

        _, _, record = slot_protocol.rollback_transition(
            active,
            pending,
            routes=routes,
            healthy_slots=healthy_slots,
            now=clock(),
        )

        self._ensure_history()
        path = self.history / f"{pending['deployment_id']}.json"

        if present(path):
            previous = load_json(path)

            if (
                previous.get("status") != "rolled_back"
                or any(
                    previous.get(k) != v
                    for k, v in pending.items()
                    if k != "status"
                )
            ):
                raise RuntimeError("Conflicting rollback history")
        else:
            self._atomic_json(path, record)

        self._finish_pending()
        return record
