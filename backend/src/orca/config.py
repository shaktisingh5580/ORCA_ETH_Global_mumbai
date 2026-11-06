"""Purpose: reject incomplete runtime configuration before the server starts.
Behavior: validate monitoring, persistence, object-store and OIDC settings before startup.
Refs: API sections 2, 3.1-3.3; main plan P02; Phase: P02.
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from orca.contracts.acquisition import SourceBinding


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCA_", extra="forbid")
    environment: Literal["development", "test", "production"] = "development"
    monitoring_token: SecretStr = Field(min_length=32)
    docs_public: bool = False
    cors_origins: tuple[str, ...] = ()
    database_url: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    auth_mode: Literal["oidc", "supabase"] = "oidc"
    supabase_anon_key: SecretStr | None = None
    object_store_endpoint: str | None = None
    object_store_bucket: str = "orca-research"
    object_store_access_key: SecretStr | None = None
    object_store_secret_key: SecretStr | None = None
    object_store_region: str = "ap-south-1"
    acquisition_enabled: bool = False
    source_probes_enabled: bool = False
    research_demo_enabled: bool = False
    acquisition_sources: tuple[SourceBinding, ...] = ()

    @model_validator(mode="after")
    def secure_defaults(self) -> Self:
        value = self.monitoring_token.get_secret_value()
        if len(set(value)) < 8 or value.lower().startswith(("replace", "example", "changeme")):
            raise ValueError("provide a generated monitoring token")
        if self.environment == "production" and self.docs_public:
            raise ValueError("public schema exposure is disabled in production")
        configured_oidc = (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        if any(configured_oidc) and not all(configured_oidc):
            raise ValueError("OIDC issuer, audience and JWKS URL must be configured together")
        if self.auth_mode == "supabase" and (
            not self.oidc_issuer
            or not self.supabase_anon_key
            or self.oidc_audience != "authenticated"
        ):
            raise ValueError(
                "Supabase mode requires project issuer, anon key and authenticated audience"
            )
        configured_store = (
            self.object_store_endpoint,
            self.object_store_access_key,
            self.object_store_secret_key,
        )
        if any(configured_store) and not all(configured_store):
            raise ValueError(
                "object-store endpoint and both credentials must be configured together"
            )
        if self.environment == "production":
            if not self.database_url:
                raise ValueError("production requires the non-owner database URL")
            if not all(configured_oidc):
                raise ValueError("production requires OIDC issuer, audience and JWKS URL")
            if not all(configured_store):
                raise ValueError("production requires object-store endpoint and credentials")
        if self.source_probes_enabled and (not self.database_url or not self.acquisition_sources):
            raise ValueError("source probes require database and approved sources")
        if self.research_demo_enabled and (not self.database_url or not all(configured_store)):
            raise ValueError("research demo requires runtime database and private storage")
        if self.acquisition_enabled:
            if not self.database_url or not all(configured_store) or not self.acquisition_sources:
                raise ValueError(
                    "acquisition requires database, cloud storage and approved sources"
                )
            identities = {(s.tenant_id, s.version_id) for s in self.acquisition_sources}
            if len(identities) != len(self.acquisition_sources):
                raise ValueError("duplicate acquisition source binding")
        return self
