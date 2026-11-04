"""课堂记录：把每句原文和译文写到 transcripts/日期_时间.md，课后可复习、生成笔记。

文件在第一句话写入时才创建，启动后没人说话就关掉不会留下空文件。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class TranscriptLog:
    def __init__(self, directory: str | Path, enabled: bool = True, title: str = "课堂记录") -> None:
        self.enabled = enabled
        self.directory = Path(directory)
        self._title = title
        self._lock = threading.Lock()
        self._stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        self.path: Path | None = self.directory / f"{self._stamp}.md" if enabled else None
        self.count = 0

    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.exists()

    def write(self, source: str, translation: str) -> None:
        if not self.enabled or self.path is None:
            return
        now = dt.datetime.now().strftime("%H:%M:%S")
        line = f"**[{now}]** {source}\n\n> {translation}\n\n"
        with self._lock:
            if not self.path.exists():
                self.directory.mkdir(parents=True, exist_ok=True)
                self.path.write_text(f"# {self._title} {self._stamp}\n\n", encoding="utf-8")
                log.info("课堂记录写入：%s", self.path)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
            self.count += 1
