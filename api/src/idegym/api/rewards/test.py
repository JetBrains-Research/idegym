from idegym.api.status import Status
from pydantic import BaseModel, Field


class TestRequest(BaseModel):
    test_script: str = Field(default="python -m pytest --junitxml=test-results.xml")
    timeout: float = Field(default=600.0, description="Timeout for the test in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0, description="Timeout in seconds for graceful process termination"
    )


class TestScores(BaseModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class TestReport(BaseModel):
    status: Status
    scores: TestScores


class TestError(BaseModel):
    message: str
