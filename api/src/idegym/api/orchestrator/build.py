from idegym.api.orchestrator.jobs import JobPollResult
from pydantic import BaseModel, Field, field_validator, model_validator
from yaml import YAMLError
from yaml import safe_load as parse_yaml


class BuildFromYamlRequest(BaseModel):
    namespace: str = Field(default="idegym", description="Kubernetes namespace for the image build jobs")
    yaml_content: str = Field(description="YAML content containing image build job definitions")

    @field_validator("yaml_content")
    def validate_yaml_content(cls, value: str) -> str:
        try:
            parse_yaml(value)
            return value
        except YAMLError as ex:
            raise ValueError("Yaml content is not valid") from ex


class BuildFromYamlResponse(BaseModel):
    job_names: list[str] = Field(description="Names of started Kubernetes build jobs")


class BuildJobsSummary(BaseModel):
    total_jobs: int = Field(ge=0)
    failed_jobs: int = Field(ge=0)
    total_time: str = Field(description="Total elapsed time, e.g. '12.34s'")
    jobs_results: list[JobPollResult]

    @model_validator(mode="after")
    def validate_jobs_results(self):
        if not len(self.jobs_results) == self.total_jobs:
            raise ValueError("Number of jobs results does not match total jobs")
        return self
