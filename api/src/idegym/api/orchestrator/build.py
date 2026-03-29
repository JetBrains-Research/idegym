from typing import List

from idegym.api.image_build import parse_image_build_pipeline
from idegym.api.orchestrator.jobs import JobPollResult
from pydantic import BaseModel, Field, field_validator, model_validator


class BuildFromYamlRequest(BaseModel):
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    yaml_content: str = Field(description="YAML content containing build jobs")

    @field_validator("yaml_content")
    def validate_yaml_content(cls, value: str) -> str:
        try:
            parse_image_build_pipeline(value)
            return value
        except Exception as ex:
            raise ValueError("Yaml content is not valid") from ex


class BuildFromYamlResponse(BaseModel):
    job_names: List[str] = Field(description="Started kubernetes job names")


class BuildJobsSummary(BaseModel):
    total_jobs: int = Field(description="Total number of started build jobs", ge=0)
    failed_jobs: int = Field(description="Number of failed build jobs", ge=0)
    total_time: str = Field(description="Total time elapsed to complete all jobs (e.g., '12.34s')")
    jobs_results: List[JobPollResult] = Field(description="Final poll results for each job")

    @model_validator(mode="after")
    def validate_jobs_results(self):
        if not len(self.jobs_results) == self.total_jobs:
            raise ValueError("Number of jobs results does not match total jobs")
        return self
