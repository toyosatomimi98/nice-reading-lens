"""会话状态：已处理的页面、处理进度，以及推给浏览器的事件通道。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


class Hub:
    """最简发布订阅：每个订阅者一个队列，队列满了丢最旧的一条。"""

    def __init__(self, maxsize: int = 64):
        self._queues: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


class Session:
    """页面的唯一真相来源，同时负责落盘。"""

    def __init__(self, pages_dir: Path):
        self.dir = Path(pages_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hub = Hub()
        self.pages: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.seq = 0
        self.status: dict = {
            "stage": "idle",
            "text": "等待翻页",
            "page": None,
            "since": time.time(),
        }
        self._load()

    # ---- 落盘 ----

    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except Exception:
            return
        self.pages = list(data.get("pages", []))
        self.by_id = {p["id"]: p for p in self.pages}
        self.seq = int(data.get("seq", len(self.pages)))

    def _save(self) -> None:
        payload = {"seq": self.seq, "pages": self.pages}
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        tmp.replace(self.index_path)

    # ---- 页面 ----

    def next_id(self) -> str:
        self.seq += 1
        return f"p{self.seq:03d}"

    def add(self, page: dict) -> int:
        self.pages.append(page)
        self.by_id[page["id"]] = page
        self._save()
        index = len(self.pages) - 1
        self.hub.publish({"type": "page.add", "page": page, "index": index})
        return index

    def path_for(self, page_id: str, kind: str) -> Path:
        return self.dir / f"{page_id}.{kind}.jpg"

    # ---- 状态 ----

    def set_status(self, stage: str, text: str, page: str | None = None) -> dict:
        self.status = {
            "stage": stage,
            "text": text,
            "page": page,
            "since": time.time(),
        }
        self.hub.publish({"type": "status", "status": self.status})
        return self.status

    def focus(self, page_id: str, reason: str = "") -> None:
        self.hub.publish(
            {"type": "page.focus", "id": page_id, "reason": reason}
        )

    def snapshot(self) -> dict:
        return {"pages": self.pages, "status": self.status}

    def reset(self) -> None:
        for page in self.pages:
            for kind in ("view", "raw", "thumb"):
                try:
                    self.path_for(page["id"], kind).unlink()
                except OSError:
                    pass
        self.pages = []
        self.by_id = {}
        self.seq = 0
        self._save()
        self.hub.publish({"type": "reset"})
