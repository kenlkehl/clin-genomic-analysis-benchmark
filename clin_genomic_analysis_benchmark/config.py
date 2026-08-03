"""Global configuration: paths, model IDs, env-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_ROOT_ENV = "CLINGEN_DATA_ROOT"
DEFAULT_DATA_ROOT = REPO_ROOT / "bpc_from_synapse"

# The gold root holds everything the agent-under-eval must NOT see: the gold
# question bank, the computed gold scripts/results, and the review workbook. It
# lives OUTSIDE the repo so an agent exploring the repo (from runs/.../scratch)
# cannot reach it. Overridable via CLINGEN_GOLD_ROOT.
GOLD_ROOT_ENV = "CLINGEN_GOLD_ROOT"
DEFAULT_GOLD_ROOT = REPO_ROOT.parent / "chatbpc" / "chatbpc_benchmark_gold"
WORKBOOK_NAME = "bpc_benchmark_review_6-19-26.xlsx"

# Public, gold-free, agent-facing question bank (safe to keep in the repo).
QUESTIONS_DIR = REPO_ROOT / "questions"
RUNS_DIR = REPO_ROOT / "runs"
SCORING_CONFIG_DIR = REPO_ROOT / "scoring_configs"
PROMPTS_DIR = REPO_ROOT / "clin_genomic_analysis_benchmark" / "prompts"
ADAPTERS_DIR = REPO_ROOT / "adapters"

# Cache for cohort context (file inventories, samples)
CACHE_DIR = REPO_ROOT / ".cache"


def data_root() -> Path:
    """Path to the BPC cohort data root. Overridable via CLINGEN_DATA_ROOT env."""
    p = os.environ.get(DATA_ROOT_ENV)
    return Path(p) if p else DEFAULT_DATA_ROOT


def gold_root() -> Path:
    """Root for all gold-bearing artifacts, kept out of the repo. Overridable
    via CLINGEN_GOLD_ROOT (default: `../chatbpc/chatbpc_benchmark_gold`)."""
    p = os.environ.get(GOLD_ROOT_ENV)
    return Path(p) if p else DEFAULT_GOLD_ROOT


def gold_questions_dir() -> Path:
    """Full (gold-bearing) per-cohort question bank, read only at scoring time."""
    return gold_root() / "questions"


def gold_standard_dir() -> Path:
    """Computed gold scripts + result.json, per unambiguous question."""
    return gold_root() / "gold_standard"


def workbook_path() -> Path:
    """The human-curated review workbook (source of truth for `sync`)."""
    return gold_root() / WORKBOOK_NAME


@dataclass(frozen=True)
class ClaudeConfig:
    """Anthropic Claude on Vertex configuration."""
    project_id: str
    region: str = "global"
    model: str = "claude-opus-4-8"

    @classmethod
    def from_env(cls) -> "ClaudeConfig":
        return cls(
            project_id=os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", ""),
            region=os.environ.get("ANTHROPIC_VERTEX_REGION", "global"),
            model=os.environ.get("CLINGEN_CLAUDE_MODEL", "claude-opus-4-8"),
        )


@dataclass(frozen=True)
class AzureConfig:
    """Azure OpenAI configuration (BAA-covered endpoint)."""
    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-12-01-preview"

    @classmethod
    def from_env(cls) -> "AzureConfig":
        return cls(
            endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
            api_key=os.environ.get("AZURE_OPENAI_API_KEY", ""),
            deployment=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5"),
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        )


@dataclass(frozen=True)
class TimeoutConfig:
    """Per-stage agent timeouts (seconds)."""
    classify: int = 600
    disambiguate: int = 300
    analyze: int = 1800


@dataclass(frozen=True)
class Settings:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig.from_env)
    azure: AzureConfig = field(default_factory=AzureConfig.from_env)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    data_root: Path = field(default_factory=data_root)


def settings() -> Settings:
    """Load settings from environment. Re-reads each call so .env updates apply."""
    return Settings()


def ensure_dirs() -> None:
    """Create benchmark output directories if missing. Gold dirs are created by
    the gold tooling under the (out-of-repo) gold root, never here."""
    for d in (QUESTIONS_DIR, RUNS_DIR, SCORING_CONFIG_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
