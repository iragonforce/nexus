#!/usr/bin/env python3
"""Guarded v2 Glance controller. Not installed in production."""

import argparse
import json

from glance_cicd_v2_runtime import (
    guard_ready,
    make_engine,
    require_root,
)


def main(argv=None, *, engine_factory=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=("preflight", "promote", "confirm"),
    )
    parser.add_argument("--deployment-id")
    parser.add_argument("--ttl", type=int, default=600)
    parser.add_argument("--execute", action="store_true")

    args = parser.parse_args(argv)

    require_root()

    if args.operation != "preflight":
        if not args.execute:
            raise RuntimeError(
                "State changes require --execute"
            )

        if not args.deployment_id:
            raise RuntimeError(
                "A deployment identifier is required"
            )

        if guard_ready() is not True:
            raise RuntimeError(
                "Compatible v2 rollback guard is not armed"
            )

    engine = (
        engine_factory() if engine_factory is not None
        else make_engine()
    )

    if args.operation == "preflight":
        result = engine.preflight()
    elif args.operation == "promote":
        result = engine.promote(
            args.deployment_id, args.ttl
        )
    else:
        result = engine.confirm(args.deployment_id)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
