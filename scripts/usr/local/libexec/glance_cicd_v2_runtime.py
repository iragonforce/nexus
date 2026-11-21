#!/usr/bin/env python3
"""V2 runtime adapter for existing Glance infrastructure.

Not installed or enabled until the coordinated migration.
"""

import importlib.util
import os
import subprocess
from pathlib import Path

from glance_cicd_state import V2StateStore
from glance_cicd_v2_engine import Engine

HERE = Path(__file__).resolve().parent

SERVICE = "glance-cicd-rollback.service"
TIMER = "glance-cicd-rollback.timer"

EXPECTED_GUARD = (
    "/usr/local/libexec/glance-cicd-v2-rollback.py"
)


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError("Glance deployment requires root")


def _systemctl(*arguments):
    result = subprocess.run(
        ["systemctl", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result


def require_compatible_service():
    """Prevent v2 operation while systemd still runs the v1 guard."""
    require_root()

    result = _systemctl(
        "show", SERVICE, "-p", "ExecStart", "--value"
    )

    if result.returncode != 0:
        raise RuntimeError("Cannot inspect recovery service")

    command = result.stdout

    if (
        EXPECTED_GUARD not in command
        or "/usr/local/libexec/glance-cicd-rollback.py"
        in command
    ):
        raise RuntimeError(
            "Recovery service does not use the compatible v2 guard"
        )


def guard_ready():
    require_compatible_service()

    for operation in ("is-enabled", "is-active"):
        result = _systemctl(operation, "--quiet", TIMER)

        if result.returncode != 0:
            return False

    return True


def _load_sibling(name, filename):
    path = HERE / filename

    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Unsafe runtime dependency: {filename}")

    spec = importlib.util.spec_from_file_location(name, path)

    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {filename}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_engine():
    # Reuse the proven NPM and verifier implementations.
    verification = _load_sibling(
        "glance_v2_verifier",
        "glance-cicd-verify.py",
    )

    recovery = _load_sibling(
        "glance_v2_npm",
        "glance-cicd-rollback.py",
    )

    return Engine(
        V2StateStore(),
        verification,
        recovery,
        guard_ready=guard_ready,
    )
