from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from community_intern.knowledge_cache.io import atomic_write_text
from community_intern.knowledge_cache.utils import hash_text
from community_intern.team_kb.models import QAPair

logger = logging.getLogger(__name__)

QA_BLOCK_MARKER = "--- QA ---"


def _format_turn_lines(*, role: str, content: str) -> list[str]:
    lines = content.splitlines() or [""]
    first = f"{role}: {lines[0]}"
    rest = [f"  {line}" for line in lines[1:]]
    return [first, *rest]

def format_topic_block(qa: QAPair) -> str:
    lines: list[str] = [QA_BLOCK_MARKER, f"id: {qa.id}", f"timestamp: {qa.timestamp}"]
    for turn in qa.turns:
        if turn.role == "user":
            lines.extend(_format_turn_lines(role="User", content=turn.content))
        elif turn.role == "team":
            lines.extend(_format_turn_lines(role="Team", content=turn.content))
        elif turn.role == "bot":
            lines.extend(_format_turn_lines(role="Bot", content=turn.content))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _remove_qa_blocks_by_id(*, text: str, remove_ids: set[str]) -> tuple[str, int]:
    """
    Remove QA blocks whose `id:` line matches one of remove_ids.

    This is a minimal parser used only for remove-by-id operations. It preserves the
    rest of the topic file verbatim as much as possible.
    """
    if not text.strip():
        return "", 0

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    prefix: list[str] = []
    blocks: list[list[str]] = []

    i = 0
    while i < len(lines) and lines[i] != QA_BLOCK_MARKER:
        prefix.append(lines[i])
        i += 1

    while i < len(lines):
        if lines[i] != QA_BLOCK_MARKER:
            i += 1
            continue
        block: list[str] = [lines[i]]
        i += 1
        while i < len(lines) and lines[i] != QA_BLOCK_MARKER:
            block.append(lines[i])
            i += 1
        blocks.append(block)

    kept_blocks: list[list[str]] = []
    removed = 0
    for block in blocks:
        qa_id = ""
        for line in block:
            if line.startswith("id:"):
                qa_id = line[len("id:") :].strip()
                break
        if qa_id and qa_id in remove_ids:
            removed += 1
            continue
        kept_blocks.append(block)

    out_lines: list[str] = []
    if any(line.strip() for line in prefix):
        out_lines.extend(prefix)

    for block in kept_blocks:
        if out_lines and out_lines[-1] != "":
            out_lines.append("")
        out_lines.extend(block)

    normalized = "\n".join(out_lines).strip() + "\n" if out_lines else ""
    return normalized, removed


class TopicStorage:
    def __init__(self, topics_dir: str, index_path: str) -> None:
        self._topics_dir = Path(topics_dir)
        self._index_path = Path(index_path)

    def ensure_dirs(self) -> None:
        self._topics_dir.mkdir(parents=True, exist_ok=True)
        self._index_path.parent.mkdir(parents=True, exist_ok=True)

    def add_to_topic(
        self,
        filename: str,
        new_qa: QAPair,
        remove_ids: Optional[list[str]] = None,
    ) -> None:
        self.ensure_dirs()
        file_path = self._topics_dir / filename
        new_block = format_topic_block(new_qa)

        if remove_ids:
            remove_set = set(remove_ids)
            try:
                existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
            except OSError:
                logger.exception("Failed to read topic file for removal. filename=%s", filename)
                raise
            rewritten, removed_count = _remove_qa_blocks_by_id(text=existing, remove_ids=remove_set)
            if removed_count:
                logger.debug(
                    "Removed obsolete QA blocks from topic. filename=%s removed_count=%d removed_ids=%s",
                    filename,
                    removed_count,
                    remove_ids,
                )
            base = rewritten
        else:
            try:
                base = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
            except OSError:
                logger.exception("Failed to read topic file for append. filename=%s", filename)
                raise

        if base and not base.endswith("\n"):
            base += "\n"

        combined = (base + new_block).lstrip("\n")
        atomic_write_text(file_path, combined)
        logger.debug("Appended QA block to topic file. filename=%s", filename)

    def create_topic(self, filename: str, first_qa: QAPair) -> None:
        self.ensure_dirs()
        file_path = self._topics_dir / filename
        atomic_write_text(file_path, format_topic_block(first_qa))
        logger.debug("Created topic file. filename=%s", filename)

    def topic_exists(self, filename: str) -> bool:
        file_path = self._topics_dir / filename
        return file_path.exists()

    def list_topics(self) -> list[str]:
        if not self._topics_dir.exists():
            return []
        return [f.name for f in self._topics_dir.glob("*.txt")]

    def clear_all(self) -> None:
        if self._topics_dir.exists():
            for pattern in ("*.txt", "*.json"):
                for f in self._topics_dir.glob(pattern):
                    f.unlink()
            logger.info("Cleared all topic files.")

        if self._index_path.exists():
            self._index_path.unlink()
            logger.info("Cleared index file.")

    def load_index_text(self) -> str:
        if not self._index_path.exists():
            return ""
        return self._index_path.read_text(encoding="utf-8")

    def save_index(self, entries: list[tuple[str, str]], *, source_id_prefix: str = "") -> None:
        self.ensure_dirs()
        lines = []
        for source_id, description in entries:
            identifier = source_id.strip()
            prefix = source_id_prefix.strip()
            if prefix and not identifier.startswith(prefix):
                identifier = f"{prefix}{identifier}"

            lines.append(identifier)
            lines.append(description)
            lines.append("")
        text = "\n".join(lines).strip() + "\n"
        atomic_write_text(self._index_path, text)
        logger.debug("Saved index file. entry_count=%d", len(entries))

    def get_topic_hash(self, filename: str) -> Optional[str]:
        file_path = self._topics_dir / filename
        if not file_path.exists():
            return None
        try:
            content = file_path.read_text(encoding="utf-8")
            return hash_text(content)
        except OSError:
            return None

    def load_topic_as_text(self, filename: str) -> str:
        """Load topic file as plain text for LLM consumption."""
        file_path = self._topics_dir / filename
        if not file_path.exists():
            return ""
        return file_path.read_text(encoding="utf-8")
