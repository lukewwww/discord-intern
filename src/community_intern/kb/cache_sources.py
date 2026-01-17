from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from community_intern.config.models import KnowledgeBaseSettings

logger = logging.getLogger(__name__)


def discover_file_sources(config: KnowledgeBaseSettings) -> Dict[str, Path]:
    sources_dir = Path(config.sources_dir)
    if not sources_dir.exists():
        logger.warning("Knowledge base sources directory is missing. path=%s", sources_dir)
        return {}
    file_sources: Dict[str, Path] = {}
    for file_path in sources_dir.rglob("*"):
        if file_path.is_file() and not file_path.name.startswith("."):
            try:
                rel_path = file_path.relative_to(sources_dir).as_posix()
                file_sources[rel_path] = file_path
            except ValueError:
                continue
    return file_sources


def discover_url_sources(config: KnowledgeBaseSettings) -> Dict[str, str]:
    links_file = Path(config.links_file_path)
    url_sources: Dict[str, str] = {}
    if not links_file.exists():
        return url_sources
    try:
        content = links_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            url = line.strip()
            if url and not url.startswith("#"):
                url_sources[url] = url
        return url_sources
    except Exception as e:
        logger.warning("Failed to read knowledge base links file. path=%s error=%s", links_file, e)
        return url_sources
