"""Append-only JSONL journal of everything the agent sees and does."""
from __future__ import annotations

import json
import os
from typing import Any


class Journal:
    def __init__(self, run_dir: str | None) -> None:
        self.run_dir = run_dir
        self._files: dict[str, Any] = {}
        if run_dir:
            os.makedirs(run_dir, exist_ok=True)

    def write(self, stream: str, record: dict) -> None:
        if not self.run_dir:
            return
        f = self._files.get(stream)
        if f is None:
            f = self._files[stream] = open(os.path.join(self.run_dir, f"{stream}.jsonl"), "a")
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        f.flush()

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]
