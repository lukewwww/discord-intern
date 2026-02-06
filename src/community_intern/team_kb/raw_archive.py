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
    lines = ["--- QA ---", f"id: {qa_pair.id}", f"timestamp: {qa_pair.timestamp}"]
    if qa_pair.conversation_id:
        lines.append(f"conversation_id: {qa_pair.conversation_id}")
    if qa_pair.message_ids:
        lines.append(f"message_ids: {', '.join(qa_pair.message_ids)}")
    for turn in qa_pair.turns:
        if turn.role == "user":
            prefix = "User:"
        elif turn.role == "bot":
            prefix = "You:"
        else:
            prefix = "Team:"
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
        qa_id = ""
        timestamp = ""
        conversation_id = ""
        message_ids: list[str] = []
        turns: list[Turn] = []

        for line in lines:
            stripped_line = line.strip()
            if stripped_line.startswith("id:"):
                qa_id = stripped_line[len("id:") :].strip()
            elif stripped_line.startswith("timestamp:"):
                timestamp = stripped_line[len("timestamp:"):].strip()
            elif stripped_line.startswith("conversation_id:"):
                conversation_id = stripped_line[len("conversation_id:"):].strip()
            elif stripped_line.startswith("message_ids:"):
                ids_str = stripped_line[len("message_ids:"):].strip()
                message_ids = [mid.strip() for mid in ids_str.split(",") if mid.strip()]
            elif stripped_line.startswith("User:"):
                turns.append(Turn(role="user", content=stripped_line[len("User:") :].strip()))
            elif stripped_line.startswith("Team:"):
                turns.append(Turn(role="team", content=stripped_line[len("Team:") :].strip()))
            elif stripped_line.startswith("You:"):
                turns.append(Turn(role="bot", content=stripped_line[len("You:") :].strip()))
            elif turns:
                # If the line doesn't start with a known prefix, it's a continuation of the previous turn
                # We use rstrip() to preserve indentation but remove trailing whitespace
                turns[-1].content += "\n" + line.rstrip()

        if not qa_id:
            logger.warning("Raw QA entry missing id. Skipping entry.")
            continue
        if not timestamp or not turns:
            logger.warning("Raw QA entry missing required fields. qa_id=%s Skipping entry.", qa_id)
            continue

        if not qa_id.startswith("qa_"):
            logger.warning("Raw QA entry has invalid id prefix. qa_id=%s Skipping entry.", qa_id)
            continue

        if qa_id != f"qa_{timestamp.replace('-', '').replace(':', '').replace('T', '_').replace('Z', '')}":
            logger.warning(
                "Raw QA entry id does not match timestamp. qa_id=%s timestamp=%s Skipping entry.",
                qa_id,
                timestamp,
            )
            continue

        if timestamp and turns:
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

    def _parse_qa_id_datetime(self, qa_id: str) -> datetime:
        value = qa_id.strip()
        if value.startswith("qa_"):
            value = value[3:]

        try:
            return datetime.strptime(value, "%Y%m%d_%H%M%S.%f")
        except ValueError:
            return datetime.strptime(value, "%Y%m%d_%H%M%S")

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

    def load_since(self, last_processed_qa_id: str) -> list[QAPair]:
        """Load pending QA pairs that are newer than the last processed ID."""
        if not self._raw_dir.exists():
            return []

        if not last_processed_qa_id:
            return self.load_all(deduplicate=True)

        try:
            last_dt = self._parse_qa_id_datetime(last_processed_qa_id)
        except ValueError as e:
            raise ValueError(f"Invalid last_processed_qa_id format: {last_processed_qa_id}") from e

        start_week_file = get_week_filename(last_dt)
        files = sorted(self._raw_dir.glob("*.txt"))

        # Filter files that might contain newer entries
        # Since files are named YYYY-WWW, string comparison works
        relevant_files = [f for f in files if f.name >= start_week_file]

        if not relevant_files:
            return []

        all_pairs: list[QAPair] = []
        for file_path in relevant_files:
            try:
                content = file_path.read_text(encoding="utf-8")
                pairs = parse_raw_file(content)
                all_pairs.extend(pairs)
            except (OSError, UnicodeDecodeError):
                logger.exception("Failed to read raw archive file. file=%s", file_path.name)

        # Filter strictly newer pairs
        filtered = [p for p in all_pairs if p.id > last_processed_qa_id]

        if filtered:
            # Deduplicate to ensure we only process the most complete version of any new conversations
            filtered = deduplicate_by_conversation(filtered)

        return filtered
