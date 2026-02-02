import tempfile
import unittest
from pathlib import Path

from community_intern.team_kb.raw_archive import RawArchive


class RawArchiveLoadSinceTests(unittest.TestCase):
    def test_load_since_accepts_microseconds_in_last_processed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            (raw_dir / "2026-W05.txt").write_text(
                "\n".join(
                    [
                        "--- QA ---",
                        "id: qa_20260201_031056.228000",
                        "timestamp: 2026-02-01T03:10:56.228000Z",
                        "conversation_id: c1",
                        "User: u1",
                        "Team: t1",
                        "",
                        "--- QA ---",
                        "id: qa_20260201_031057.000000",
                        "timestamp: 2026-02-01T03:10:57.000000Z",
                        "conversation_id: c2",
                        "User: u2",
                        "Team: t2",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            archive = RawArchive(str(raw_dir))
            pending = archive.load_since("qa_20260201_031056.228000")

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].conversation_id, "c2")


if __name__ == "__main__":
    unittest.main()
