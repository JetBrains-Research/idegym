from idegym.api.status import Status
from pydantic import BaseModel, Field


class ResetRequest(BaseModel):
    timeout: float = Field(default=600.0, description="Timeout for the reset operation in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0, description="Timeout in seconds for graceful process termination"
    )


class ResetResult(BaseModel):
    status: Status
    output: str
