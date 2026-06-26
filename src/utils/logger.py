import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import yaml


def get_logger(name: str) -> logging.Logger:
    config = _load_log_config()
    log_dir = Path(config.get("log_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = getattr(logging, config.get("level", "INFO").upper(), logging.INFO)
    logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    fh = RotatingFileHandler(
        log_dir / "pipeline.log",
        maxBytes=config.get("max_bytes", 10_485_760),
        backupCount=config.get("backup_count", 5),
        encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


def _load_log_config() -> dict:
    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        return {}
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("logging", {})
