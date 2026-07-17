"""Entrypoint for the loopback OpenHands Tools Service (supervised inside the container).

Binds only to ``127.0.0.1``: the external surface is IdeGYM's normal
forwarded ``/api/...`` and ``/mcp``. The service is *not* run inside a terminal backend; the backends
are command-execution substrates it owns.
"""

import uvicorn
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.service.app import build_app


def main() -> None:
    config = RuntimeConfig.from_env()
    app = build_app(config)
    uvicorn.run(app, host=config.service_host, port=config.service_port, log_level="info")


if __name__ == "__main__":
    main()
