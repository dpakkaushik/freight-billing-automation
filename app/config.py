"""Application settings loaded from environment / .env file.

All knobs are documented in `.env.example`. Reading config from a single
module keeps secrets out of source files and lets us swap values per
environment (dev, staging, prod) without code changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # --- Storage ---
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: Path = Path("./data/uploads")
    log_dir: Path = Path("./logs")

    # --- OCR / Extraction ---
    ocr_languages: str = "en"
    ocr_use_gpu: bool = False
    # "gemini" | "ollama" | "llm" | "easyocr"
    extraction_engine: str = "gemini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_text_model: str = "llama3.2:3b"

    # --- Gemini ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # --- TMS ---
    tms_base_url: str = "https://pallia.tmslive.in/"
    tms_username: str = "changeme"
    tms_password: str = "changeme"
    tms_headless: bool = True
    tms_slow_mo_ms: int = 0

    # --- Queue ---
    worker_concurrency: int = 1
    job_max_retries: int = 2

    # --- EEE-Taxi ---
    eee_taxi_output_dir: Path = Path("./data/eee_taxi")
    mtoken_pkcs11_lib: str = r"C:\Windows\System32\CryptoIDA_pkcs11.dll"

    # --- Authentication ---
    auth_jwt_secret: str = "changeme"
    auth_token_expire_minutes: int = 480

    # --- Supabase ---
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_analytics_table: str = "api_usage_events"

    @field_validator("upload_dir", "log_dir", mode="before")
    @classmethod
    def _expand_path(cls, v) -> Path:
        return Path(v).expanduser().resolve()

    @property
    def ocr_lang_list(self) -> List[str]:
        return [code.strip() for code in self.ocr_languages.split(",") if code.strip()]

    def ensure_dirs(self) -> None:
        """Create runtime directories if missing. Called once on startup."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.eee_taxi_output_dir.mkdir(parents=True, exist_ok=True)
        # SQLite file directory
        if self.database_url.startswith("sqlite"):
            db_path = self.database_url.split("///", 1)[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
