from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lockstep:lockstep@localhost:5432/lockstep"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    # Fallback narration provider when there's no Anthropic key (or it's invalid) —
    # same role, same prompt, same "facts in, plain English out" contract. Never
    # used for matching; that stays rule-only regardless of which provider narrates.
    groq_api_key: str = ""
    # A reasoning model — see the `reasoning_effort` call in ai_summaries.py, which
    # is specific to this family of Groq-hosted models and keeps reasoning-token
    # overhead low enough that a short summary fits in max_tokens.
    groq_model: str = "openai/gpt-oss-120b"

    # "stub" returns a fixed real sandbox sample regardless of GSTIN/period — there's
    # no live GSP subscription configured yet. See services/gsp.py.
    gsp_provider: str = "stub"

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    storage_backend: str = "local"
    upload_dir: str = "./data/uploads"

    api_v1_prefix: str = "/api/v1"

    # Vendor risk bands, from on_time_rate. A judge will ask to change these live,
    # so they are config, not constants in the scoring code.
    risk_on_time_rate_low: float = 0.9      # >= this -> LOW
    risk_on_time_rate_medium: float = 0.6   # >= this -> MEDIUM, below -> HIGH
    risk_history_window: int = 6            # periods used for avg_days_past_cutoff

    # Action thresholds, in rupees of tax at risk.
    # Auto-notify always; propose a hold above X; require human approval above Y.
    action_hold_proposal_threshold: float = 50_000.0
    action_approval_threshold: float = 200_000.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
