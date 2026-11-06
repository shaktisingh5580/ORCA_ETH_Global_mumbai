"""Purpose: reject incomplete runtime configuration before the server starts.
Behavior: validate monitoring credentials and documentation exposure only.
Refs: API sections 2 and 3.1; Phase: P01. OIDC user auth belongs to P02.
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCA_", extra="forbid")
    environment: Literal["development", "test", "production"] = "development"
    monitoring_token: SecretStr = Field(min_length=32)
    docs_public: bool = False

    @model_validator(mode="after")
    def secure_defaults(self) -> Self:
        value = self.monitoring_token.get_secret_value()
        if len(set(value)) < 8 or value.lower().startswith(("replace", "example", "changeme")):
            raise ValueError("provide a generated monitoring token")
        if self.environment == "production" and self.docs_public:
            raise ValueError("public schema exposure is disabled in production")
        return self
