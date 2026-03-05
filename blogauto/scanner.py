from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json


@dataclass
class ScanReport:
    added: list[str]
    modified: list[str]
    removed: list[str]


class DirectoryScanner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.snapshot_file = workspace / ".scan_snapshot.json"

    def scan_content(self, content_dir: Path) -> ScanReport:
        current = self._snapshot(content_dir)
        previous = self._load_snapshot()

        added = sorted([k for k in current if k not in previous])
        removed = sorted([k for k in previous if k not in current])
        modified = sorted([k for k in current if k in previous and current[k] != previous[k]])

        self.snapshot_file.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return ScanReport(added=added, modified=modified, removed=removed)

    def _snapshot(self, content_dir: Path) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for path in sorted(content_dir.glob("**/*.txt")):
            try:
                key = str(path.relative_to(self.workspace))
            except ValueError:
                key = str(path.resolve())
            mapping[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return mapping

    def _load_snapshot(self) -> dict[str, str]:
        if not self.snapshot_file.exists():
            return {}
        return json.loads(self.snapshot_file.read_text(encoding="utf-8"))
