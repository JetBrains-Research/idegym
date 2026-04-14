from pydantic import BaseModel, Field


class BashCommandRequest(BaseModel):
    command: str = Field(description="Command to execute as a bash script")
    timeout: float = Field(default=600.0, description="Timeout for the command execution in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0, description="Timeout in seconds for graceful process termination"
    )


class BashCommandResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class BashCommandErrorResponse(BaseModel):
    message: str
