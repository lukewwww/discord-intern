from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from community_intern.knowledge_cache.utils import format_rfc3339, utc_now
from community_intern.team_kb.models import QAPair, Turn

logger = logging.getLogger(__name__)


def get_week_filename(dt: datetime) -> str:
    iso_calendar = dt.isocalendar()
    return f"{iso_calendar.year}-W{iso_calendar.week:02d}.txt"


def format_raw_qa_pair(qa_pair: QAPair) -> str:
    lines = ["--- QA ---", f"timestamp: {qa_pair.timestamp}"]
    if qa_pair.conversation_id:
        lines.append(f"conversation_id: {qa_pair.conversation_id}")
    if qa_pair.message_ids:
        lines.append(f"message_ids: {', '.join(qa_pair.message_ids)}")
    for turn in qa_pair.turns:
        prefix = "Q:" if turn.role == "user" else "A:"
        lines.append(f"{prefix} {turn.content}")
    lines.append("")
    return "\n".join(lines)


def parse_raw_file(content: str) -> list[QAPair]:
    qa_pairs: list[QAPair] = []
    entries = content.split("--- QA ---")

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        lines = entry.split("\n")
        timestamp = ""
        conversation_id = ""
        message_ids: list[str] = []
        turns: list[Turn] = []

        for line in lines:
            line = line.strip()
            if line.startswith("timestamp:"):
                timestamp = line[len("timestamp:"):].strip()
            elif line.startswith("conversation_id:"):
                conversation_id = line[len("conversation_id:"):].strip()
            elif line.startswith("message_ids:"):
                ids_str = line[len("message_ids:"):].strip()
                message_ids = [mid.strip() for mid in ids_str.split(",") if mid.strip()]
            elif line.startswith("Q:"):
                turns.append(Turn(role="user", content=line[2:].strip()))
            elif line.startswith("A:"):
                turns.append(Turn(role="team", content=line[2:].strip()))

        if timestamp and turns:
            qa_id = f"qa_{timestamp.replace('-', '').replace(':', '').replace('T', '_').replace('Z', '')}"
            qa_pairs.append(QAPair(
                id=qa_id,
                timestamp=timestamp,
                turns=turns,
                conversation_id=conversation_id,
                message_ids=message_ids,
            ))

    return qa_pairs


def deduplicate_by_conversation(qa_pairs: list[QAPair]) -> list[QAPair]:
    """Keep only the most complete version of each conversation.

    For pairs with the same conversation_id, keep the one with the most message_ids.
    Pairs without conversation_id are kept as-is.
    """
    no_conv_id: list[QAPair] = []
    by_conv_id: dict[str, QAPair] = {}

    for qa in qa_pairs:
        if not qa.conversation_id:
            no_conv_id.append(qa)
            continue

        existing = by_conv_id.get(qa.conversation_id)
        if existing is None or len(qa.message_ids) > len(existing.message_ids):
            by_conv_id[qa.conversation_id] = qa

    result = no_conv_id + list(by_conv_id.values())
    result.sort(key=lambda p: p.timestamp)
    return result


class RawArchive:
    def __init__(self, raw_dir: str) -> None:
        self._raw_dir = Path(raw_dir)

    def ensure_dir(self) -> None:
        self._raw_dir.mkdir(parents=True, exist_ok=True)

    async def append(self, qa_pair: QAPair) -> None:
        self.ensure_dir()

        dt = utc_now()
        filename = get_week_filename(dt)
        file_path = self._raw_dir / filename

        content = format_raw_qa_pair(qa_pair)

        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(content)
            logger.info(
                "Appended QA pair to raw archive. file=%s qa_id=%s",
                filename,
                qa_pair.id,
            )
        except OSError:
            logger.exception("Failed to append QA pair to raw archive. file=%s", filename)
            raise

    def load_all(self, *, deduplicate: bool = True) -> list[QAPair]:
        """Load all QA pairs from raw archive files.

        Args:
            deduplicate: If True, deduplicate by conversation_id keeping the most
                complete version. Defaults to True for regeneration use case.
        """
        if not self._raw_dir.exists():
            return []

        all_pairs: list[QAPair] = []
        files = sorted(self._raw_dir.glob("*.txt"))

        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
                pairs = parse_raw_file(content)
                all_pairs.extend(pairs)
                logger.debug("Loaded %d QA pairs from %s", len(pairs), file_path.name)
            except (OSError, UnicodeDecodeError):
                logger.exception("Failed to read raw archive file. file=%s", file_path.name)

        if deduplicate:
            before_count = len(all_pairs)
            all_pairs = deduplicate_by_conversation(all_pairs)
            if before_count != len(all_pairs):
                logger.info(
                    "Deduplicated QA pairs by conversation. before=%d after=%d",
                    before_count,
                    len(all_pairs),
                )
        else:
            all_pairs.sort(key=lambda p: p.timestamp)

        return all_pairs
