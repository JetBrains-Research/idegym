from pydantic import BaseModel, Field


class BashCommandRequest(BaseModel):
    command: str = Field(description="Command to execute as a bash script")
    timeout: float = Field(default=600.0)
    graceful_termination_timeout: float = Field(default=2.0)


class BashCommandResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class BashCommandErrorResponse(BaseModel):
    message: str
