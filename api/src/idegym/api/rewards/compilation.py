from idegym.api.status import Status
from pydantic import BaseModel, Field


class CompilationRequest(BaseModel):
    compilation_script: str = Field(description="Compilation script", default="./gradlew assemble")
    timeout: float = Field(description="Timeout for the compilation", default=600.0)
    graceful_termination_timeout: float = Field(
        description="Timeout in seconds for graceful process termination", default=2.0
    )


class CompilationResult(BaseModel):
    status: Status = Field(description="Compilation status")
    output: str = Field(description="Compilation output")


class CompilationError(BaseModel):
    message: str = Field(description="Error message")
