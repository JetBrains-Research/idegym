from typing import Optional

from idegym.api.type import AuthType, HttpUrl
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Authorization(BaseModel):
    """Authorization credentials. Both fields are excluded from serialization."""

    type: Optional[AuthType] = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    token: Optional[str] = Field(
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
    name: str = Field(pattern=r"^.+\.(?:zip|tar\.gz)$")
    url: HttpUrl

    model_config = ConfigDict(frozen=True)


class DownloadRequest(BaseModel):
    descriptor: ArchiveDescriptor
    auth: Authorization = Field(default_factory=Authorization)

    model_config = ConfigDict(frozen=True)
