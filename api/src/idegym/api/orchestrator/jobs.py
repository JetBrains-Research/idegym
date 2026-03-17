from typing import Optional

from pydantic import BaseModel, Field


class JobStatusResponse(BaseModel):
    id: int = Field(default=0, description="Job status record ID")
    job_name: str = Field(description="Job name")
    status: str = Field(description="Job status")
    created_at: int = Field(default=0, description="Created at (ms)")
    updated_at: int = Field(default=0, description="Updated at (ms)")
    details: Optional[str] = Field(default=None, description="Optional details")
    tag: Optional[str] = Field(default=None, description="Optional tag")
    request_id: Optional[str] = Field(default=None, description="Optional request id")


class JobPollResult(BaseModel):
    job_name: str = Field(description="Job name")
    status: str = Field(description="Final job status")
    tag: Optional[str] = Field(default=None, description="Optional tag")
    details: Optional[str] = Field(default=None, description="Optional details")
