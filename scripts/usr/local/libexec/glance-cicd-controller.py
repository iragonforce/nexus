#!/usr/bin/env python3
"""
Purpose:
    Coordinate a guarded Glance Blue-to-Green NPM deployment.

Input:
    preflight, promote or confirm operation; deployment ID and TTL.
    Protected NPM credentials are required for routing operations.

Output:
    Durable deployment checkpoint, verified NPM routing and history.

Safety:
    Shared filesystem lock protects every state transition.
    Pending state is durable before changing NPM.
    Live promotion requires an active rollback timer.
    Only NPM host 198 may be changed.
    No Docker images, Compose files or volumes are modified.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from glance_cicd_state import StateStore

HOST_ID = 198
DOMAIN = "glance.blinkwired.com"
BLUE = "glance"
GREEN = "glance-green-cicd"


def load_module(name, filename):
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(name, path)

    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {filename}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = load_module(
    "glance_cicd_recovery",
    "glance-cicd-rollback.py",
)

verification = load_module(
    "glance_cicd_verification",
    "glance-cicd-verify.py",
)


def state_store():
    return StateStore()


def validate_host(host, expected):
    if not isinstance(host, dict):
        raise RuntimeError("Invalid NPM host response")

    if host.get("domain_names") != [DOMAIN]:
        raise RuntimeError("Unexpected NPM domain configuration")

    if host.get("forward_host") != expected:
        raise RuntimeError(
            f"Unexpected NPM upstream: {host.get('forward_host')}"
        )

    if int(host.get("forward_port", 0)) != 8080:
        raise RuntimeError("Unexpected NPM forwarding port")

    if host.get("forward_scheme") != "http":
        raise RuntimeError("Unexpected NPM forwarding scheme")


def candidate_ready():
    # Green can be healthy before NPM host 198 points to it.
    for name in verification.GREEN_CONTAINERS:
        verification.check_health(name)

    verification.check_http(GREEN)


def preflight():
    # Verify all four existing Blue routes and Blue health.
    verification.verify("blue")

    # Check Green independently of the current NPM route.
    candidate_ready()

    token = recovery.authenticate()
    host = recovery.get_host(token)

    validate_host(host, BLUE)

    return token, host


def switch_to_green(token, host):
    # Copy the existing NPM host settings rather than rebuilding them.
    payload = {
        key: host[key]
        for key in recovery.FIELDS
        if key in host and host[key] is not None
    }

    payload["forward_host"] = GREEN

    recovery.api(
        "PUT",
        f"/nginx/proxy-hosts/{HOST_ID}",
        token,
        payload,
    )


def promote(deployment_id, ttl_seconds=600):
    # Enforce the safety gate for both CLI and Python callers.
    require_active_guard()
    store = state_store()

    with store.locked():
        if store.read_pending() is not None:
            raise RuntimeError("Another deployment is pending")

        # All readiness checks happen before creating pending state.
        token, host = preflight()

        # This must be durable BEFORE modifying NPM.
        pending = store.begin(deployment_id, ttl_seconds)

        # An exception below intentionally leaves pending.json intact.
        # The timer will restore Blue after the deadline.
        switch_to_green(token, host)

        # Verify generated Nginx configuration and Green HTTP.
        verification.verify("green")

        # Check the authoritative NPM API result as well.
        validate_host(recovery.get_host(token), GREEN)

        return pending


def require_persistent_green():
    """
    Fail closed until a restart-resilient, Git-managed Green
    lifecycle and reboot recovery have been implemented.

    The current isolated candidate has restart policy 'no'.
    It must never become a permanently confirmed upstream.
    """
    raise RuntimeError(
        "Confirmation disabled: Green restart lifecycle "
        "is not yet managed"
    )


def confirm(deployment_id):
    require_active_guard()
    require_persistent_green()
    store = state_store()

    with store.locked():

        def verify_confirmation(pending):
            if pending["deployment_id"] != deployment_id:
                return False

            verification.verify("green")

            token = recovery.authenticate()
            validate_host(recovery.get_host(token), GREEN)

            return True

        return store.confirm(
            deployment_id,
            verify_confirmation,
        )


def require_active_guard():
    for command in (
        ["systemctl", "is-enabled", "--quiet",
         "glance-cicd-rollback.timer"],
        ["systemctl", "is-active", "--quiet",
         "glance-cicd-rollback.timer"],
    ):
        result = subprocess.run(
            command,
            check=False,
            timeout=10,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "Rollback timer must be enabled and active"
            )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "operation",
        choices=("preflight", "promote", "confirm"),
    )
    parser.add_argument("--deployment-id")
    parser.add_argument("--ttl", type=int, default=600)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly authorize a state-changing operation",
    )

    args = parser.parse_args()

    if args.operation == "preflight":
        preflight()
        print(json.dumps({
            "status": "ready",
            "blue": "verified",
            "green_candidate": "verified",
            "routing_modified": False,
        }, indent=2))
        return

    if not args.execute:
        raise RuntimeError(
            "State-changing operations require --execute"
        )

    if not args.deployment_id:
        raise RuntimeError("--deployment-id is required")

    if args.operation == "promote":
        result = promote(args.deployment_id, args.ttl)
    else:
        result = confirm(args.deployment_id)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"GLANCE CONTROLLER FAILED: {exc}",
              file=sys.stderr)
        sys.exit(1)
