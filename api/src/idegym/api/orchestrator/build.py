from idegym.api.orchestrator.jobs import JobPollResult
from pydantic import BaseModel, Field, field_validator, model_validator
from yaml import YAMLError
from yaml import safe_load as parse_yaml


class BuildFromYamlRequest(BaseModel):
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    yaml_content: str = Field(description="YAML content containing build jobs")

    @field_validator("yaml_content")
    def validate_yaml_content(cls, value: str) -> str:
        try:
            parse_yaml(value)
            return value
        except YAMLError as ex:
            raise ValueError("Yaml content is not valid") from ex


class BuildFromYamlResponse(BaseModel):
    job_names: list[str] = Field(description="Started kubernetes job names")


class BuildJobsSummary(BaseModel):
    total_jobs: int = Field(description="Total number of started build jobs", ge=0)
    failed_jobs: int = Field(description="Number of failed build jobs", ge=0)
    total_time: str = Field(description="Total time elapsed to complete all jobs (e.g., '12.34s')")
    jobs_results: list[JobPollResult] = Field(description="Final poll results for each job")

    @model_validator(mode="after")
    def validate_jobs_results(self):
        if not len(self.jobs_results) == self.total_jobs:
            raise ValueError("Number of jobs results does not match total jobs")
        return self
