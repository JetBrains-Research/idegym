from typing import Optional

from pydantic import BaseModel, Field


class HealthCheckResponse(BaseModel):
    status: str = Field(description="Service health status, e.g. 'healthy'")
    message: Optional[str] = Field(default=None, description="Optional message with details")
