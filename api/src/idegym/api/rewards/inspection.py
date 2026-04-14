from typing import Optional

from idegym.api.status import Status
from pydantic import BaseModel, Field


class InspectionRequest(BaseModel):
    files_to_analyze: Optional[list[str]] = Field(default=None)
    timeout: float = Field(default=600.0, description="Timeout for the analysis in seconds")


class InspectionResult(BaseModel):
    status: Status
    output: str = Field(description="Analysis output, or error message if analysis failed", default="")


class InspectionError(BaseModel):
    message: str
