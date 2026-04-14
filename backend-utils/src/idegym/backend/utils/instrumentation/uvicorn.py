from threading import RLock as ReentrantLock
from typing import Any, Callable, Collection, Iterator, Optional
from weakref import WeakSet

from idegym.backend import __version__
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.metrics import CallbackOptions, Meter, MeterProvider, Observation, get_meter
from uvicorn.config import Config
from uvicorn.server import Server

ServerInit = Callable[[Server, Config], None]


# Use a no-op function instead of `None` to avoid type checker complaints
def stub_server_init(_server: Server, _config: Config):
    pass


class UvicornInstrumentor(BaseInstrumentor):
    def __init__(self):
        super().__init__()
        self._original_server_init: ServerInit = stub_server_init
        self._servers: WeakSet[Server] = WeakSet()
        self._lock: ReentrantLock = ReentrantLock()
        self._meter: Optional[Meter] = None
        self._instrumented: bool = False

    def instrumentation_dependencies(self) -> Collection[str]:
        return (  # language=requirements
            "uvicorn >= 0.35.0",
        )

    def _instrument(self, **kwargs: Any):
        self.__instrument(
            meter_provider=kwargs.get("meter_provider"),
        )

    def _uninstrument(self, **kwargs: Any):
        # Gauges are kept allocated; callbacks simply yield nothing when no servers are tracked.
        with self._lock:
            if not self._instrumented:
                return
            self._original_server_init = stub_server_init
            self._servers = WeakSet()
            self._instrumented = False

    def __instrument(self, meter_provider: Optional[MeterProvider] = None):
        with self._lock:
            if self._instrumented:
                return

            self._original_server_init = Server.__init__

            # Capture self in a closure variable to avoid relying on Server.__init__ being
            # looked up at call time (it will already be patched by then).
            instrumentor = self

            def patched_server_init(_self, *_args, **_kwargs):
                instrumentor._original_server_init(_self, *_args, **_kwargs)
                instrumentor._register_server(_self)

            Server.__init__ = patched_server_init

            self._meter = get_meter(
                name=__name__,
                version=__version__,
                meter_provider=meter_provider,
            )

            self._meter.create_observable_up_down_counter(
                name="uvicorn.server.state.connections",
                callbacks=[self._observe_connections],
                description="Number of currently open server connections",
                unit="count",
            )

            self._meter.create_observable_up_down_counter(
                name="uvicorn.server.state.tasks",
                callbacks=[self._observe_tasks],
                description="Number of currently running server asyncio tasks",
                unit="count",
            )

            self._meter.create_observable_counter(
                name="uvicorn.server.state.requests",
                callbacks=[self._observe_total_requests],
                description="Total number of server requests",
                unit="count",
            )

            self._instrumented = True

    def _register_server(self, server: Server) -> None:
        with self._lock:
            self._servers.add(server)

    def _observe_connections(self, _options: CallbackOptions) -> Iterator[Observation]:
        with self._lock:
            servers = list(self._servers)
        for server in servers:
            yield Observation(
                value=len(server.server_state.connections),
                attributes={
                    "server": hex(id(server)),
                },
            )

    def _observe_tasks(self, _options: CallbackOptions) -> Iterator[Observation]:
        with self._lock:
            servers = list(self._servers)
        for server in servers:
            yield Observation(
                value=len(server.server_state.tasks),
                attributes={
                    "server": hex(id(server)),
                },
            )

    def _observe_total_requests(self, _options: CallbackOptions) -> Iterator[Observation]:
        with self._lock:
            servers = list(self._servers)
        for server in servers:
            yield Observation(
                value=server.server_state.total_requests,
                attributes={
                    "server": hex(id(server)),
                },
            )
