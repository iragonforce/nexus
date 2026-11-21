#!/usr/bin/env python3
"""V2 recovery entrypoint. Requires compatible systemd service."""

import argparse
import json
import os

from glance_cicd_v2_runtime import (
    make_engine,
    require_compatible_service,
    require_root,
)


def main(argv=None, *, engine_factory=None):
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)

    require_root()
    require_compatible_service()

    engine = (
        engine_factory() if engine_factory is not None
        else make_engine()
    )

    # An installed v2 guard must not silently report success
    # before the explicit active-slot bootstrap has occurred.
    if not os.path.lexists(engine.store.active):
        raise RuntimeError(
            "V2 active-slot checkpoint not initialized"
        )

    try:
        result = engine.recover()
    except BlockingIOError:
        # Controller currently holds the shared deployment lock.
        # The next timer invocation can retry recovery.
        result = {"status": "busy"}

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
