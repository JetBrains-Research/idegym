from typing import Optional

from pydantic import BaseModel, Field


class JobStatusResponse(BaseModel):
    id: int = Field(default=0)
    job_name: str
    status: str
    created_at: int = Field(default=0, description="Epoch milliseconds")
    updated_at: int = Field(default=0, description="Epoch milliseconds")
    details: Optional[str] = Field(default=None)
    tag: Optional[str] = Field(default=None)
    request_id: Optional[str] = Field(default=None)


class JobPollResult(BaseModel):
    job_name: str
    status: str
    tag: Optional[str] = Field(default=None)
    details: Optional[str] = Field(default=None)
