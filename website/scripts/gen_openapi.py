"""Generate OpenAPI schemas for the orchestrator and in-pod server.

Builds minimal FastAPI apps that mount the real routers (without the heavy
create_app() init: config, k8s, DB, telemetry) and dumps app.openapi().

Run with the project venv:
    .venv/bin/python website/scripts/gen_openapi.py website/static/openapi

By default every schema must generate successfully — if one fails the script
exits non-zero, so we never ship the site with a missing/stale OpenAPI. Pass
--allow-partial to downgrade a failure to a warning.
"""

import argparse
import json
import logging
from pathlib import Path

from fastapi import FastAPI

logger = logging.getLogger("gen_openapi")


def _dump(app: FastAPI, path: Path) -> None:
    schema = app.openapi()
    path.write_text(json.dumps(schema, indent=2) + "\n")
    logger.info("wrote %s (%d paths)", path, len(schema.get("paths", {})))


def gen_orchestrator(out_dir: Path) -> None:
    from idegym.orchestrator.router import (
        async_operation,
        build_images,
        client,
        forwarding,
        server,
        snapshot,
    )

    app = FastAPI(
        title="IdeGYM Orchestrator API",
        description=(
            "REST API for the IdeGYM orchestrator: client registration, server "
            "lifecycle, in-cluster image builds, request forwarding, async "
            "operations, and pod snapshots."
        ),
        version="1.0.0",
    )
    for mod in (client, server, build_images, forwarding, async_operation, snapshot):
        app.include_router(mod.router)
    _dump(app, out_dir / "orchestrator.json")


def gen_server(out_dir: Path) -> None:
    from idegym.rewards.router import router as rewards_router
    from idegym.tools.router import router as tools_router

    app = FastAPI(
        title="IdeGYM Server API",
        description=(
            "In-pod FastAPI server exposing tools (bash, file ops) and reward "
            "endpoints. Mounted under the /api prefix in the running container."
        ),
        version="1.0.0",
    )
    app.include_router(tools_router, prefix="/api")
    app.include_router(rewards_router, prefix="/api")
    _dump(app, out_dir / "server.json")


GENERATORS = {"orchestrator": gen_orchestrator, "server": gen_server}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the orchestrator and server OpenAPI schemas for the website.",
    )
    parser.add_argument(
        "out_dir",
        nargs="?",
        default=Path("."),
        type=Path,
        help="directory to write orchestrator.json and server.json into (default: current dir)",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="log and continue if a schema fails, instead of exiting non-zero",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    for name, generate in GENERATORS.items():
        try:
            generate(out_dir)
        except Exception:
            logger.exception("failed to generate the %s OpenAPI schema", name)
            failed.append(name)

    if failed:
        if args.allow_partial:
            logger.warning("continuing with a partial result; failed: %s", ", ".join(failed))
            return 0
        logger.error(
            "aborting — these schemas failed to generate: %s (pass --allow-partial to ignore)",
            ", ".join(failed),
        )
        return 1

    logger.info("all OpenAPI schemas generated in %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
