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
        # 手机那边的取景状态。在线与否只看「多久没收到预览帧」。
        self.camera: dict = {
            "verdict": "—",
            "frames": 0,
            "video": [0, 0],
            "last_seen": 0.0,
            "requested_at": 0.0,
        }
        self.preview: bytes | None = None
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

    def update_page(self, page: dict) -> None:
        """页面内容被改过（比如整页翻转），存盘并通知前端。"""
        self._save()
        self.hub.publish({"type": "page.update", "page": page})

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

    # ---- 取景状态 ----

    def touch_camera(self, verdict: str | None = None, size=None, preview: bytes | None = None) -> dict:
        self.camera["last_seen"] = time.time()
        self.camera["frames"] += 1
        if verdict:
            self.camera["verdict"] = verdict
        if size and size[0] and size[1]:
            self.camera["video"] = [int(size[0]), int(size[1])]
        if preview:
            self.preview = preview
        return self.camera

    def camera_state(self) -> dict:
        """给前端的取景状态。十来秒没收到预览帧就当掉线——采集一页要几秒，
        阈值定太紧会在抓图期间闪一下离线。"""
        last = float(self.camera["last_seen"])
        age = time.time() - last if last else None
        state = dict(self.camera)
        state["online"] = bool(last) and age is not None and age < 10.0
        state["age"] = round(age, 1) if age is not None else None
        state["has_preview"] = self.preview is not None
        return state

    def request_capture(self) -> None:
        """电脑端点「采集」。手机下一次回传预览帧时会收到这个请求。"""
        self.camera["requested_at"] = time.time()
        self.hub.publish({"type": "camera", "camera": self.camera_state()})

    def take_capture_request(self) -> bool:
        asked = float(self.camera["requested_at"])
        if not asked or time.time() - asked > 15.0:
            return False
        self.camera["requested_at"] = 0.0
        return True

    def snapshot(self) -> dict:
        return {"pages": self.pages, "status": self.status, "camera": self.camera_state()}

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
