"""Generate OpenAPI schemas for the orchestrator and in-pod server.

Builds minimal FastAPI apps that mount the real routers (without the heavy
create_app() init: Hydra config, k8s, DB, telemetry) and dumps app.openapi().
Run with the project venv:  .venv/bin/python gen_openapi.py <out_dir>
"""

import json
import sys
from pathlib import Path

from fastapi import FastAPI


def _dump(app: FastAPI, path: Path) -> None:
    schema = app.openapi()
    path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"wrote {path} ({len(schema.get('paths', {}))} paths)")


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
    app = FastAPI(
        title="IdeGYM Server API",
        description=(
            "In-pod FastAPI server exposing tools (bash, file ops) and reward "
            "endpoints. Mounted under the /api prefix in the running container."
        ),
        version="1.0.0",
    )
    from idegym.rewards.router import router as rewards_router
    from idegym.tools.router import router as tools_router

    app.include_router(tools_router, prefix="/api")
    app.include_router(rewards_router, prefix="/api")
    _dump(app, out_dir / "server.json")


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    gen_orchestrator(out_dir)
    try:
        gen_server(out_dir)
    except Exception as exc:  # best-effort; orchestrator is the required one
        print(f"server openapi skipped: {exc!r}")


if __name__ == "__main__":
    main()
