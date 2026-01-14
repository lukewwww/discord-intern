from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from community_intern.config.models import LoggingSettings


def init_logging(settings: LoggingSettings) -> None:
    """
    Initialize application logging.

    Requirements are defined in docs/logging.md.
    """

    root_logger = logging.getLogger()

    level_name = settings.level.upper()
    level = logging.getLevelNamesMapping().get(level_name)
    if level is None:
        raise ValueError(f"Invalid logging level: {settings.level}")

    root_logger.setLevel(level)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_path = settings.file.path.strip()
    if not file_path:
        return

    try:
        file_path_obj = Path(file_path)
        if file_path_obj.parent and not file_path_obj.parent.exists():
            file_path_obj.parent.mkdir(parents=True, exist_ok=True)

        file_handler = TimedRotatingFileHandler(
            filename=str(file_path_obj),
            when="midnight",
            interval=1,
            backupCount=settings.file.rotation.backup_count,
            encoding="utf-8",
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError:
        root_logger.error(
            "File logging handler failed to initialize path=%s",
            file_path,
            exc_info=True,
        )


__all__ = ["init_logging"]
