from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from community_intern.kb.cache_io import atomic_write_json, atomic_write_text
from community_intern.kb.cache_utils import hash_text
from community_intern.team_kb.models import QAPair

logger = logging.getLogger(__name__)


def qa_pair_to_dict(qa: QAPair) -> dict:
    from community_intern.team_kb.models import Turn

    return {
        "id": qa.id,
        "timestamp": qa.timestamp,
        "turns": [{"role": t.role, "content": t.content} for t in qa.turns],
    }


def dict_to_qa_pair(data: dict) -> QAPair:
    from community_intern.team_kb.models import Turn

    turns = [
        Turn(role=t["role"], content=t["content"])
        for t in data.get("turns", [])
    ]
    return QAPair(
        id=data["id"],
        timestamp=data["timestamp"],
        turns=turns,
    )


class TopicStorage:
    def __init__(self, topics_dir: str, index_path: str) -> None:
        self._topics_dir = Path(topics_dir)
        self._index_path = Path(index_path)

    def ensure_dirs(self) -> None:
        self._topics_dir.mkdir(parents=True, exist_ok=True)
        self._index_path.parent.mkdir(parents=True, exist_ok=True)

    def load_topic(self, filename: str) -> list[QAPair]:
        file_path = self._topics_dir / filename
        if not file_path.exists():
            return []

        try:
            content = file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            return [dict_to_qa_pair(item) for item in data]
        except (OSError, json.JSONDecodeError, KeyError):
            logger.exception("Failed to load topic file. filename=%s", filename)
            return []

    def save_topic(self, filename: str, qa_pairs: list[QAPair]) -> None:
        self.ensure_dirs()
        file_path = self._topics_dir / filename
        data = [qa_pair_to_dict(qa) for qa in qa_pairs]
        atomic_write_json(file_path, data)
        logger.debug("Saved topic file. filename=%s qa_count=%d", filename, len(qa_pairs))

    def add_to_topic(
        self,
        filename: str,
        new_qa: QAPair,
        remove_ids: Optional[list[str]] = None,
    ) -> None:
        qa_pairs = self.load_topic(filename)

        if remove_ids:
            remove_set = set(remove_ids)
            qa_pairs = [qa for qa in qa_pairs if qa.id not in remove_set]
            logger.debug(
                "Removed %d obsolete QA pairs from topic. filename=%s removed_ids=%s",
                len(remove_ids),
                filename,
                remove_ids,
            )

        qa_pairs.append(new_qa)
        self.save_topic(filename, qa_pairs)

    def create_topic(self, filename: str, first_qa: QAPair) -> None:
        self.save_topic(filename, [first_qa])
        logger.info("Created new topic file. filename=%s", filename)

    def topic_exists(self, filename: str) -> bool:
        file_path = self._topics_dir / filename
        return file_path.exists()

    def list_topics(self) -> list[str]:
        if not self._topics_dir.exists():
            return []
        return [f.name for f in self._topics_dir.glob("*.json")]

    def clear_all(self) -> None:
        if self._topics_dir.exists():
            for f in self._topics_dir.glob("*.json"):
                f.unlink()
            logger.info("Cleared all topic files.")

        if self._index_path.exists():
            self._index_path.unlink()
            logger.info("Cleared index file.")

    def load_index_text(self) -> str:
        if not self._index_path.exists():
            return ""
        return self._index_path.read_text(encoding="utf-8")

    def save_index(self, entries: list[tuple[str, str]]) -> None:
        self.ensure_dirs()
        lines = []
        for filename, description in entries:
            lines.append(filename)
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
        """Load topic file and format as readable conversation text for LLM."""
        qa_pairs = self.load_topic(filename)
        return format_qa_pairs_as_text(qa_pairs)


def format_qa_pairs_as_text(qa_pairs: list[QAPair]) -> str:
    """Format QA pairs into readable conversation text for LLM consumption."""
    lines = []
    for qa in qa_pairs:
        if qa.timestamp:
            lines.append(f"--- {qa.timestamp} ---")

        for turn in qa.turns:
            if turn.role == "user":
                lines.append(f"User: {turn.content}")
            elif turn.role == "team":
                lines.append(f"Team: {turn.content}")

        lines.append("")

    return "\n".join(lines)
