#!/usr/bin/env python3
"""V2 Glance transition orchestration.

No production entrypoint is enabled in this source-only stage.
Dependencies are injected for isolated controller/guard tests.
"""

import math
import os
import time

import glance_cicd_slot_protocol as protocol
from glance_cicd_state import load_json


class Engine:
    def __init__(
        self, store, verification, recovery, *,
        guard_ready, clock=time.time,
    ):
        self.store = store
        self.verification = verification
        self.recovery = recovery
        self.guard_ready = guard_ready
        self.clock = clock

    def _guard(self):
        if self.guard_ready() is not True:
            raise RuntimeError(
                "Rollback timer must be enabled and active"
            )

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise RuntimeError("Invalid deployment clock")
        return value

    def _pending_exists(self):
        return os.path.lexists(self.store.pending)

    def _check_host(self, host, expected):
        if not isinstance(host, dict):
            raise RuntimeError("Invalid NPM response")
        if host.get("domain_names") != [protocol.DOMAIN]:
            raise RuntimeError("Unexpected NPM domain")
        if host.get("forward_host") != expected:
            raise RuntimeError("Unexpected NPM upstream")
        port = host.get("forward_port")
        if type(port) not in (str, int) or str(port) != "8080":
            raise RuntimeError("Unexpected NPM port")
        if host.get("forward_scheme") != "http":
            raise RuntimeError("Unexpected NPM scheme")

    def _health(self, slot):
        names = (
            self.verification.BLUE_CONTAINERS
            if slot == "blue"
            else self.verification.GREEN_CONTAINERS
        )
        for name in names:
            self.verification.check_health(name)
        self.verification.check_http(protocol.UPSTREAMS[slot])

    def _both_healthy(self):
        for slot in protocol.UPSTREAMS:
            self._health(slot)
        return {"blue": True, "green": True}

    def _verify_routes(self, slot):
        result = self.verification.verify(slot)
        expected = {
            host: protocol.UPSTREAMS["blue"]
            for host in protocol.OTHER_HOSTS
        }
        expected[protocol.HOST_ID] = protocol.UPSTREAMS[slot]
        routes = {
            entry["host_id"]: entry["upstream"]
            for entry in result["routes"]
        }
        if routes != expected or result.get("status") != "verified":
            raise RuntimeError("NPM route verification mismatch")
        return routes

    def _api_host(self, token, expected):
        host = self.recovery.get_host(token)
        self._check_host(host, expected)
        return host

    def _switch(self, token, host, candidate):
        payload = {
            field: host[field]
            for field in self.recovery.FIELDS
            if field in host and host[field] is not None
        }
        payload["forward_host"] = candidate
        self.recovery.api(
            "PUT",
            f"/nginx/proxy-hosts/{protocol.HOST_ID}",
            token,
            payload,
        )

    def preflight(self):
        with self.store.locked():
            if self._pending_exists():
                raise RuntimeError("Deployment already pending")
            active = self.store.read_active_v2()
            protocol.validate_active(active)
            slot = active["active_slot"]
            self._verify_routes(slot)
            self._health(protocol.opposite(slot))
            token = self.recovery.authenticate()
            self._api_host(token, protocol.UPSTREAMS[slot])
            return {
                "active_slot": slot,
                "candidate_slot": protocol.opposite(slot),
                "status": "ready",
            }

    def promote(self, deployment_id, ttl_seconds=600):
        self._guard()

        with self.store.locked():
            if self._pending_exists():
                raise RuntimeError("Deployment already pending")

            active = self.store.read_active_v2()
            protocol.validate_active(active)

            previous = active["active_slot"]
            candidate = protocol.opposite(previous)

            self._verify_routes(previous)
            self._health(candidate)

            token = self.recovery.authenticate()
            host = self._api_host(
                token,
                protocol.UPSTREAMS[previous],
            )

            self._guard()

            pending = self.store.begin_v2(
                deployment_id,
                now=self._now(),
                ttl_seconds=ttl_seconds,
            )

            # The pending checkpoint is durable BEFORE NPM PUT.
            self._switch(
                token, host, protocol.UPSTREAMS[candidate]
            )

            self._verify_routes(candidate)
            self._api_host(
                token,
                protocol.UPSTREAMS[candidate],
            )

            return pending

    def confirm(self, deployment_id):
        self._guard()

        with self.store.locked():
            pending = self.store._pending_v2()

            if pending.get("deployment_id") != deployment_id:
                raise RuntimeError("Deployment identifier mismatch")

            active = self.store.read_active_v2()
            protocol.validate_pending(active, pending)

            candidate = pending["candidate_slot"]
            routes = self._verify_routes(candidate)
            health = self._both_healthy()

            token = self.recovery.authenticate()
            self._api_host(
                token,
                pending["candidate_upstream"],
            )
            self._guard()

            def verify_again(checkpoint):
                if checkpoint != pending:
                    return False
                return (
                    self._verify_routes(candidate) == routes
                    and self._both_healthy() == health
                    and bool(self._api_host(
                        token, pending["candidate_upstream"]
                    ))
                )

            return self.store.confirm_v2(
                deployment_id,
                routes=routes,
                healthy_slots=health,
                verifier=verify_again,
                clock=self._now,
            )

    def recover(self):
        with self.store.locked(blocking=False):
            if not self._pending_exists():
                return {"status": "idle"}

            pending = self.store._pending_v2()
            deployment_id = pending.get("deployment_id")

            if (
                not isinstance(deployment_id, str)
                or not protocol.ID_PATTERN.fullmatch(deployment_id)
            ):
                raise RuntimeError(
                    "Invalid pending deployment identifier"
                )

            history = (
                self.store.history / f"{deployment_id}.json"
            )

            # Durable confirmation is authoritative even after TTL.
            if os.path.lexists(history):
                record = load_json(history)

                if record.get("status") == "confirmed":
                    # A durable confirmation must not be reversed.
                    # Verify that NPM still routes to the committed
                    # candidate before finishing the active-slot write.
                    for host_id in protocol.OTHER_HOSTS:
                        self.verification.check_route(
                            host_id, protocol.UPSTREAMS["blue"]
                        )
                    self.verification.check_route(
                        protocol.HOST_ID,
                        pending["candidate_upstream"],
                        required_domain=protocol.DOMAIN,
                    )
                    token = self.recovery.authenticate()
                    self._api_host(
                        token, pending["candidate_upstream"]
                    )
                    finalized = self.store.finalize_confirmed_v2()
                    return {
                        "status": "confirmed",
                        "deployment_id": finalized["deployment_id"],
                    }

                if record.get("status") != "rolled_back":
                    raise RuntimeError("Conflicting history status")

                # Never perform an NPM write against a checkpoint
                # whose existing rollback decision is incompatible.
                if any(
                    record.get(key) != value
                    for key, value in pending.items()
                    if key != "status"
                ):
                    raise RuntimeError(
                        "Conflicting rolled-back history identity"
                    )

                completed = record.get("completed_epoch")
                if (
                    type(completed) not in (int, float)
                    or not math.isfinite(completed)
                ):
                    raise RuntimeError(
                        "Invalid rollback completion timestamp"
                    )

            active = self.store.read_active_v2()
            protocol.validate_pending(active, pending)

            if self._now() < pending["deadline_epoch"]:
                return {"status": "pending"}

            previous = pending["previous_slot"]
            self._health(previous)

            # Only the canary host may change.
            for host_id in protocol.OTHER_HOSTS:
                self.verification.check_route(
                    host_id,
                    protocol.UPSTREAMS["blue"],
                )

            token = self.recovery.authenticate()
            host = self.recovery.get_host(token)

            allowed = (
                pending["candidate_upstream"],
                pending["previous_upstream"],
            )

            if host.get("forward_host") not in allowed:
                raise RuntimeError(
                    "Unknown canary upstream; refusing overwrite"
                )

            self._check_host(host, host["forward_host"])

            self.verification.check_route(
                protocol.HOST_ID,
                host["forward_host"],
                required_domain=protocol.DOMAIN,
            )

            if host["forward_host"] != pending["previous_upstream"]:
                self._switch(
                    token, host, pending["previous_upstream"]
                )

            routes = self._verify_routes(previous)
            self._api_host(
                token,
                pending["previous_upstream"],
            )

            health = {previous: True}

            def verify_again(checkpoint):
                return (
                    checkpoint == pending
                    and self._verify_routes(previous) == routes
                    and bool(self._api_host(
                        token, pending["previous_upstream"]
                    ))
                )

            record = self.store.record_rollback_v2(
                routes=routes,
                healthy_slots=health,
                verifier=verify_again,
                clock=self._now,
            )

            return {
                "status": "rolled_back",
                "deployment_id": record["deployment_id"],
            }
