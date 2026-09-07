"""Конфиги проекта: YAML → pydantic, .env → отчёт по площадкам."""

from pathlib import Path

from lab.config.env import (
    DATA_ENV,
    SERVICE_ENV,
    VENUE_ENV,
    VenueStatus,
    apply_dotenv,
    data_keys_report,
    environment,
    format_venues_table,
    load_dotenv,
    venues_report,
)
from lab.config.loader import ConfigError, load_config
from lab.config.models import (
    BranchGroupLimits,
    BranchStop,
    JobSpec,
    LimitsConfig,
    ScheduleConfig,
    ThresholdConfig,
)

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def load_limits(path: Path | None = None) -> LimitsConfig:
    return load_config(path or CONFIG_DIR / "limits.yaml", LimitsConfig)


def load_threshold(path: Path | None = None) -> ThresholdConfig:
    return load_config(path or CONFIG_DIR / "threshold.yaml", ThresholdConfig)


def load_schedule(path: Path | None = None) -> ScheduleConfig:
    return load_config(path or CONFIG_DIR / "schedule.yaml", ScheduleConfig)


__all__ = [
    "CONFIG_DIR",
    "DATA_ENV",
    "SERVICE_ENV",
    "VENUE_ENV",
    "BranchGroupLimits",
    "BranchStop",
    "ConfigError",
    "JobSpec",
    "LimitsConfig",
    "ScheduleConfig",
    "ThresholdConfig",
    "VenueStatus",
    "apply_dotenv",
    "data_keys_report",
    "environment",
    "format_venues_table",
    "load_config",
    "load_dotenv",
    "load_limits",
    "load_schedule",
    "load_threshold",
    "venues_report",
]
