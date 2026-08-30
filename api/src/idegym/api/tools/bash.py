from typing import Optional

from pydantic import BaseModel, Field

DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


class BashCommandRequest(BaseModel):
    command: str = Field(description="Command to execute as a bash script")
    timeout: float = Field(default=600.0, description="Timeout for the command execution in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0, description="Timeout in seconds for graceful process termination"
    )
    max_output_bytes: Optional[int] = Field(
        default=DEFAULT_MAX_OUTPUT_BYTES,
        ge=1,
        strict=True,
        description="Maximum retained bytes for each output stream; null retains complete output",
    )


class BashCommandResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class BashCommandErrorResponse(BaseModel):
    message: str
