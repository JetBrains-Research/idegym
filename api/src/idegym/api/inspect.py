from typing import Optional

from pydantic import BaseModel, Field


class InspectRequest(BaseModel):
    """Parameters for a JetBrains IDE inspection run (mirrors ``inspect.sh`` arguments).

    Positional arguments map to the first three fields; optional flags are keyword-only.

    See https://www.jetbrains.com/help/idea/command-line-code-inspector.html for
    the full ``inspect.sh`` reference.
    """

    project_path: str = Field(description="Absolute path to the project directory inside the container")
    profile_path: str = Field(
        description=(
            "Absolute path to the inspection profile XML file. "
            "Use a project-relative ``.idea/inspectionProfiles/`` file "
            "or an absolute path to any valid profile."
        )
    )
    output_dir: str = Field(description="Directory where result files will be written (created if absent)")
    changes_only: bool = Field(default=False, description="Only inspect locally changed files (-changes)")
    directory: Optional[str] = Field(
        default=None, description="Limit inspection scope to this subdirectory of the project (-d)"
    )
    format: str = Field(default="xml", description="Output format: 'xml' (default) or 'json' (-format)")
    verbosity: int = Field(default=0, ge=0, le=2, description="Verbosity level 0–2 (-v0/-v1/-v2)")
    timeout: float = Field(default=600.0, description="Maximum seconds to wait for inspect.sh to finish")


class InspectResponse(BaseModel):
    """Result of a JetBrains IDE inspection run."""

    output_dir: str = Field(description="Directory containing the inspection result files")
    exit_code: int = Field(description="Exit code returned by inspect.sh (0 = success)")
