from idegym.api.status import Status
from pydantic import BaseModel, Field


class SetupRequest(BaseModel):
    setup_check_script: str = Field(default="true")
    timeout: float = Field(default=600.0, description="Timeout for the setup in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0, description="Timeout in seconds for graceful process termination"
    )


class SetupResult(BaseModel):
    status: Status
    output: str


class SetupError(BaseModel):
    message: str
