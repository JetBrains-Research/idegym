from typing import Optional

from pydantic import BaseModel, Field

DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


class BashCommandRequest(BaseModel):
    command: str = Field(description="Command to execute as a bash script")
    cwd: Optional[str] = Field(
        default=None,
        description=(
            "Working directory for the command. Defaults to the server's project directory. "
            "A relative path is resolved against it."
        ),
        examples=["/root/work", "src"],
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Environment variables added to the command's environment, overriding inherited ones. "
            "Prefer this over writing 'export' lines into the script: the values never enter the "
            "command text, so they are not logged with it."
        ),
        examples=[{"CI": "1"}],
    )
    user: Optional[str] = Field(
        default=None,
        description=(
            "Run the command as this user via 'runuser'. Requires the server to run as root; "
            "leave unset to run as the server's own user."
        ),
        examples=["devuser"],
    )
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
    strip_output: bool = Field(
        default=False,
        description=(
            "Trim leading and trailing whitespace from stdout and stderr. Off by default so that "
            "output is byte-for-byte what the command wrote; undecodable bytes are still replaced."
        ),
    )


class BashCommandResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class BashCommandErrorResponse(BaseModel):
    message: str
