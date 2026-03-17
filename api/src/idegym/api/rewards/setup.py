from idegym.api.status import Status
from pydantic import BaseModel, Field


class SetupRequest(BaseModel):
    setup_check_script: str = Field(description="Setup check script", default="true")
    timeout: float = Field(description="Timeout for the setup", default=600.0)
    graceful_termination_timeout: float = Field(
        description="Timeout in seconds for graceful process termination", default=2.0
    )


class SetupResult(BaseModel):
    status: Status = Field(description="Setup status")
    output: str = Field(description="Setup output")


class SetupError(BaseModel):
    message: str = Field(description="Error message")
