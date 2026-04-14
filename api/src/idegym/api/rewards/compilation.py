from idegym.api.status import Status
from pydantic import BaseModel, Field


class CompilationRequest(BaseModel):
    compilation_script: str = Field(default="./gradlew assemble")
    timeout: float = Field(default=600.0, description="Timeout for the compilation in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0, description="Timeout in seconds for graceful process termination"
    )


class CompilationResult(BaseModel):
    status: Status
    output: str


class CompilationError(BaseModel):
    message: str
