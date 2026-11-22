from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://miniharness:miniharness@localhost:55432/miniharness"
    data_dir: Path = Path("./data")
    workspace_dir: Path = Path(__file__).resolve().parents[3] / "workspace"
    default_python_environment: str = ""
    environment_timeout: float = Field(default=300, ge=30)
    llm_model: str = ""
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "local"
    embedding_model: str = ""
    embedding_base_url: str = "http://localhost:11434/v1"
    embedding_api_key: str = "local"
    embedding_dimensions: int = 768
    memory_recommendations: int = Field(default=3, ge=0, le=10)
    memory_context_tokens: int = Field(default=2000, ge=0)
    memory_auto_share: float = Field(default=0.4, ge=0, le=1)
    memory_match_limit: int = Field(default=3, ge=1, le=10)
    max_context_tokens: int = Field(default=262144, ge=0)
    compression_threshold: float = Field(default=0.95, ge=0, le=1)
    max_output_tokens: int = Field(default=0, ge=0)
    max_run_tokens: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(default=0, ge=0)
    run_timeout: float = Field(default=0, ge=0, allow_inf_nan=False)
    command_timeout: float = 30
    tool_output_preview_bytes: int = Field(default=2000, ge=2)
    llm_timeout: float = Field(default=180, ge=0, allow_inf_nan=False)
    skill_dirs: list[Path] = [Path("../skills")]
    cors_origins: list[str] = ["http://localhost:5174", "http://127.0.0.1:5174"]

    @property
    def postgres_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")
