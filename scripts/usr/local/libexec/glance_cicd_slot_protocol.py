#!/usr/bin/env python3
"""Pure, fail-closed two-slot Glance CI/CD transition contract.

This module performs no filesystem, Docker, NPM or systemd I/O.
Production controller and recovery integration is deliberately separate.
"""

import math
import re
from copy import deepcopy

SCHEMA = 2
HOST_ID = 198
DOMAIN = "glance.blinkwired.com"
OTHER_HOSTS = (174, 191, 195)

UPSTREAMS = {
    "blue": "glance",
    "green": "glance-green-cicd",
}

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class ProtocolError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ProtocolError(message)


def slot_for(upstream):
    for slot, value in UPSTREAMS.items():
        if value == upstream:
            return slot
    raise ProtocolError("Unknown Glance upstream")


def opposite(slot):
    _require(slot in UPSTREAMS, "Unknown slot")
    return "green" if slot == "blue" else "blue"


def validate_active(active):
    _require(
        isinstance(active, dict),
        "Missing active-slot state",
    )
    _require(
        active.get("schema") == SCHEMA,
        "Unsupported state schema",
    )
    _require(
        active.get("active_slot") in UPSTREAMS,
        "Unknown active slot",
    )

    generation = active.get("generation")
    _require(
        type(generation) is int and generation >= 0,
        "Invalid state generation",
    )

    deployment = active.get("deployment_id")

    _require(
        deployment is None or (
            isinstance(deployment, str)
            and ID_PATTERN.fullmatch(deployment)
        ),
        "Invalid confirmed deployment identifier",
    )

    return active


def genesis_blue(npm_routes, *, no_pending):
    """Explicit migration only when all four routes are Blue."""

    _require(
        no_pending is True,
        "Cannot migrate with pending deployment",
    )
    _require(
        isinstance(npm_routes, dict),
        "Missing NPM route snapshot",
    )
    _require(
        set(npm_routes) == set(OTHER_HOSTS) | {HOST_ID},
        "Unexpected NPM hosts",
    )
    _require(
        all(
            npm_routes[host] == UPSTREAMS["blue"]
            for host in (*OTHER_HOSTS, HOST_ID)
        ),
        "Bootstrap requires all routes on Blue",
    )

    return {
        "schema": SCHEMA,
        "active_slot": "blue",
        "generation": 0,
        "deployment_id": None,
    }


def begin_transition(
    active,
    deployment_id,
    *,
    now,
    ttl_seconds=600,
):
    validate_active(active)

    _require(
        isinstance(deployment_id, str)
        and ID_PATTERN.fullmatch(deployment_id),
        "Invalid deployment identifier",
    )
    _require(
        type(ttl_seconds) is int
        and 60 <= ttl_seconds <= 3600,
        "Invalid deployment TTL",
    )
    _require(
        type(now) in (int, float) and math.isfinite(now),
        "Invalid clock",
    )

    previous = active["active_slot"]
    candidate = opposite(previous)

    return {
        "schema": SCHEMA,
        "deployment_id": deployment_id,
        "host_id": HOST_ID,
        "domain": DOMAIN,
        "previous_slot": previous,
        "candidate_slot": candidate,
        "previous_upstream": UPSTREAMS[previous],
        "candidate_upstream": UPSTREAMS[candidate],
        "generation": active["generation"] + 1,
        "status": "pending",
        "deadline_epoch": now + ttl_seconds,
    }


def validate_pending(active, pending):
    validate_active(active)

    _require(
        isinstance(pending, dict),
        "Missing pending state",
    )
    _require(
        pending.get("schema") == SCHEMA,
        "Unsupported pending schema",
    )
    _require(
        pending.get("status") == "pending",
        "Not pending",
    )
    _require(
        isinstance(pending.get("deployment_id"), str)
        and ID_PATTERN.fullmatch(pending["deployment_id"]),
        "Invalid pending identifier",
    )
    _require(
        pending.get("host_id") == HOST_ID
        and pending.get("domain") == DOMAIN,
        "Wrong NPM target",
    )

    previous = active["active_slot"]
    candidate = opposite(previous)

    _require(
        pending.get("previous_slot") == previous
        and pending.get("candidate_slot") == candidate,
        "Stale or inverted transition",
    )
    _require(
        pending.get("previous_upstream") == UPSTREAMS[previous]
        and pending.get("candidate_upstream") == UPSTREAMS[candidate],
        "Unexpected transition upstream",
    )
    _require(
        type(pending.get("generation")) is int
        and pending["generation"] == active["generation"] + 1,
        "Stale transition generation",
    )

    deadline = pending.get("deadline_epoch")

    _require(
        type(deadline) in (int, float)
        and math.isfinite(deadline),
        "Invalid pending deadline",
    )

    return pending


def _check_routes(routes, expected):
    _require(
        isinstance(routes, dict),
        "Missing NPM routing evidence",
    )
    _require(
        set(routes) == set(OTHER_HOSTS) | {HOST_ID},
        "Unexpected NPM routing coverage",
    )
    _require(
        all(
            routes[host] == UPSTREAMS["blue"]
            for host in OTHER_HOSTS
        ),
        "Non-canary production route changed",
    )
    _require(
        routes[HOST_ID] == expected,
        "Unexpected canary upstream",
    )


def confirm_transition(
    active,
    pending,
    *,
    routes,
    healthy_slots,
    now,
):
    validate_pending(active, pending)

    _require(
        type(now) in (int, float) and math.isfinite(now),
        "Invalid clock",
    )
    _require(
        now < pending["deadline_epoch"],
        "Confirmation deadline expired",
    )

    _check_routes(routes, pending["candidate_upstream"])

    _require(
        isinstance(healthy_slots, dict)
        and all(
            healthy_slots.get(slot) is True
            for slot in UPSTREAMS
        ),
        "Both slot pairs must be healthy",
    )

    updated = {
        "schema": SCHEMA,
        "active_slot": pending["candidate_slot"],
        "generation": pending["generation"],
        "deployment_id": pending["deployment_id"],
    }

    record = dict(
        pending,
        status="confirmed",
        completed_epoch=now,
    )

    return updated, record


def rollback_transition(
    active,
    pending,
    *,
    routes,
    healthy_slots,
    now,
):
    validate_pending(active, pending)

    _require(
        type(now) in (int, float) and math.isfinite(now),
        "Invalid clock",
    )
    _require(
        now >= pending["deadline_epoch"],
        "Rollback deadline not reached",
    )
    _require(
        isinstance(healthy_slots, dict)
        and healthy_slots.get(active["active_slot"]) is True,
        "Previous active slot must be healthy",
    )
    _require(
        isinstance(routes, dict),
        "Missing NPM routing evidence",
    )
    _require(
        routes.get(HOST_ID) in (
            pending["candidate_upstream"],
            pending["previous_upstream"],
        ),
        "Unknown active upstream; refusing overwrite",
    )

    _check_routes_other_hosts(routes)

    record = dict(
        pending,
        status="rolled_back",
        completed_epoch=now,
    )

    return (
        pending["previous_upstream"],
        deepcopy(active),
        record,
    )


def _check_routes_other_hosts(routes):
    _require(
        set(routes) == set(OTHER_HOSTS) | {HOST_ID},
        "Unexpected NPM routing coverage",
    )
    _require(
        all(
            routes[host] == UPSTREAMS["blue"]
            for host in OTHER_HOSTS
        ),
        "Non-canary production route changed",
    )
