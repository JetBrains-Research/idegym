from base64 import b64encode
from typing import Optional

from pydantic import BaseModel, Field, SecretStr


class BasicAuth(BaseModel):
    """Basic HTTP authentication credentials. Both fields are excluded from serialization."""

    username: Optional[str] = Field(default=None, exclude=True)
    password: Optional[SecretStr] = Field(default=None, exclude=True)

    @property
    def base64(self) -> Optional[str]:
        """Returns Base64-encoded 'username:password' string, or None if username is not set."""
        if not self.username:
            return None
        password = self.password.get_secret_value() if self.password else ""
        value = self.username + ":" + password
        encoded = value.encode()
        return b64encode(encoded).decode()
