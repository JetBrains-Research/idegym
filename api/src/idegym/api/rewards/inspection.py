from typing import Optional

from idegym.api.status import Status
from pydantic import BaseModel, Field


class InspectionRequest(BaseModel):
    files_to_analyze: Optional[list[str]] = Field(description="File list for analysis", default=None)
    timeout: float = Field(description="Timeout for the analysis", default=600.0)


class InspectionResult(BaseModel):
    status: Status = Field(description="Analysis status")
    output: str = Field(description="Output from analysis or error message if analysis failed", default="")


class InspectionError(BaseModel):
    message: str = Field(description="Error message")
