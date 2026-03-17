from idegym.api.status import Status
from pydantic import BaseModel, Field


class TestRequest(BaseModel):
    test_script: str = Field(description="Test script", default="python -m pytest --junitxml=test-results.xml")
    timeout: float = Field(description="Timeout for the test", default=600.0)
    graceful_termination_timeout: float = Field(
        description="Timeout in seconds for graceful process termination", default=2.0
    )


class TestScores(BaseModel):
    total: int = Field(ge=0, description="Total number of tests")
    passed: int = Field(ge=0, description="Number of passed tests")
    failed: int = Field(ge=0, description="Number of failed tests")
    skipped: int = Field(ge=0, description="Number of skipped tests")


class TestReport(BaseModel):
    status: Status = Field(description="Overall running tests status")
    scores: TestScores = Field(description="Unit test scores")


class TestError(BaseModel):
    message: str = Field(description="Error message")
