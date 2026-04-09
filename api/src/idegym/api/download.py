from typing import Optional

from idegym.api.type import AuthType, HttpUrl
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Authorization(BaseModel):
    type: Optional[AuthType] = Field(
        description="Authorization type",
        default=None,
        exclude=True,
        repr=False,
    )
    token: Optional[str] = Field(
        description="Authorization token",
        default=None,
        exclude=True,
        repr=False,
    )

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_auth(self):
        if self.type is not None and self.token is None:
            raise ValueError("Authorization token is required when type is specified")
        return self


class ArchiveDescriptor(BaseModel):
    name: str = Field(description="Archive name", pattern=r"^.+\.(?:zip|tar\.gz)$")
    url: HttpUrl = Field(description="Archive URL")

    model_config = ConfigDict(frozen=True)


class DownloadRequest(BaseModel):
    descriptor: ArchiveDescriptor = Field(description="Download task subject")
    auth: Authorization = Field(description="Authorization details", default_factory=Authorization)

    model_config = ConfigDict(frozen=True)
