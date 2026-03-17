from pydantic import BaseModel, Field


class BashCommandRequest(BaseModel):
    """Request model for executing a bash command."""

    command: str = Field(description="Command to execute as a bash script")
    timeout: float = Field(description="Timeout for the command execution", default=600.0)
    graceful_termination_timeout: float = Field(
        description="Timeout in seconds for graceful process termination", default=2.0
    )


class BashCommandResponse(BaseModel):
    """Response model for bash command execution."""

    stdout: str = Field(description="Standard output of the command")
    stderr: str = Field(description="Standard error output of the command")
    exit_code: int = Field(description="Exit code of the command")


class BashCommandErrorResponse(BaseModel):
    """Response model for bash command execution with error."""

    message: str = Field(description="Error message")
