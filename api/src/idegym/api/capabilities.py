from pydantic import BaseModel, Field


class CapabilitiesResponse(BaseModel):
    plugins: list[str] = Field(description="Names of server plugins loaded in the running container")
