from idegym.api.config import Config
from idegym.api.memory import MemoryUnit
from idegym.api.orchestrator.servers import StartServerRequest


def extract_resources_request(config: Config, request: StartServerRequest) -> tuple[float, float]:
    cpu_request = config.orchestrator.resources.default_cpu_request
    ram_request = config.orchestrator.resources.default_ram_request
    if resources := request.resources:
        cpu_value = (resources.limits and resources.limits.cpu) or (resources.requests and resources.requests.cpu)
        if cpu_value is not None:
            cpu_request = cpu_value.cores
        memory_value = (resources.limits and resources.limits.memory) or (
            resources.requests and resources.requests.memory
        )
        if memory_value is not None:
            ram_request = memory_value.bytes / MemoryUnit.Gi

    return cpu_request, ram_request
