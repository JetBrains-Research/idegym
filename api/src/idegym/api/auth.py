from base64 import b64encode
from typing import Optional

from pydantic import BaseModel, Field, SecretStr


class BasicAuth(BaseModel):
    username: Optional[str] = Field(description="Username for authentication", default=None, exclude=True)
    password: Optional[SecretStr] = Field(description="Password for authentication", default=None, exclude=True)

    @property
    def base64(self) -> Optional[str]:
        if not self.username:
            return None
        password = self.password.get_secret_value() if self.password else ""
        value = self.username + ":" + password
        encoded = value.encode()
        return b64encode(encoded).decode()
