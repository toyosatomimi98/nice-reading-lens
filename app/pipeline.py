"""一张书页照片进，一份「原文 + 译文 + 坐标」的文档出。"""

from __future__ import annotations

import asyncio
import difflib
import json
import time
from pathlib import Path

from . import imaging
from .gate import PageGate
from .layout import build_blocks
from .ocr import OcrEngine
from .store import Session
from .translate import Translator


class Pipeline:
    def __init__(self, session: Session, config, glossary_path: Path, cache_path: Path):
        self.session = session
        self.config = config
        self.glossary_path = Path(glossary_path)
        self.ocr = OcrEngine()
        self.gate = PageGate(lambda: config.value)
        self.translator = Translator(lambda: config.value, cache_path)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._task: asyncio.Task | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ---- 词表 ----

    def glossary(self) -> dict:
        if not self.glossary_path.exists():
            return {}
        try:
            return json.loads(self.glossary_path.read_text("utf-8"))
        except Exception:
            return {}

    def save_glossary(self, data: dict) -> dict:
        clean = {str(k).strip(): str(v).strip() for k, v in (data or {}).items()}
        clean = {k: v for k, v in clean.items() if k and v}
        self.glossary_path.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), "utf-8"
        )
        return clean

    # ---- 入口 ----

    def submit(self, data: bytes, source: str = "phone") -> bool:
        """丢一张整页照片进队列。队列满时丢掉排队中的旧帧。"""
        dropped = False
        if self.queue.full():
            try:
                self.queue.get_nowait()
                dropped = True
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait((data, source))
        return dropped

    def probe(self, data: bytes) -> dict:
        """低分辨率预览帧，只做翻页判断，不做识别。"""
        img = imaging.decode(data)
        verdict = self.gate.push(imaging.to_gray(img))
        response = {"verdict": verdict, "action": "capture" if verdict == "confirm" else "wait"}
        if verdict == "confirm":
            self.session.set_status("capturing", "检测到翻页，等待清晰图像")
        return response

    # ---- 工作线程 ----

    async def _worker(self) -> None:
        while True:
            data, source = await self.queue.get()
            try:
                await self._process(data, source)
            except Exception as exc:  # 单页失败不该拖垮整个服务
                self.session.set_status("error", f"这一页没处理成功：{exc}")

    async def _process(self, data: bytes, source: str) -> None:
        s = self.config.value
        started = time.time()
        self.session.set_status("prepare", "收到书页，正在摆正")

        raw = await asyncio.to_thread(imaging.decode, data)
        image, rectified = raw, False
        if s.rectify:
            image, rectified = await asyncio.to_thread(imaging.rectify, raw)
        image = await asyncio.to_thread(imaging.enhance, image)
        signature = imaging.dhash(imaging.to_gray(imaging.shrink(image, 480)))

        pieces = imaging.split_spread(image, s.split_mode, s.split_order)
        self.session.set_status("ocr", "正在识别文字", None)
        ocr_started = time.time()
        blocks: list[dict] = []
        for piece in pieces:
            local = await asyncio.to_thread(
                self.ocr.read, piece["image"], s.min_confidence
            )
            blocks.extend(
                build_blocks(local, origin=piece["offset"], side=piece["label"])
            )
        ocr_ms = int((time.time() - ocr_started) * 1000)

        if not blocks:
            self.session.set_status("error", "这一页没识别出文字，换个角度再试试")
            return

        for i, block in enumerate(blocks, start=1):
            block["id"] = f"{i:02d}"
            block["order"] = i

        duplicate = self._find_duplicate(signature, blocks)
        if duplicate is not None:
            self.session.focus(duplicate["id"], "duplicate")
            self.session.set_status(
                "ready", f"这页之前处理过，已跳回第 {duplicate['index']} 页", duplicate["id"]
            )
            return

        self.session.set_status("translate", f"正在翻译 {len(blocks)} 段")
        translate_started = time.time()
        texts = [b["text"] for b in blocks]
        translated = await self.translator.translate(
            texts, self.glossary(), self._context(s.context_chars)
        )
        translate_ms = int((time.time() - translate_started) * 1000)
        for block, zh in zip(blocks, translated):
            block["zh"] = zh

        page_id = self.session.next_id()
        for block in blocks:
            block["id"] = f"{page_id}b{block['order']:02d}"

        page = {
            "id": page_id,
            "index": len(self.session.pages) + 1,
            "created_at": time.time(),
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "rectified": rectified,
            "split": [p["label"] for p in pieces],
            "hash": signature,
            "source_text": " ".join(texts)[:4000],
            "blocks": blocks,
            "elapsed_ms": {
                "ocr": ocr_ms,
                "translate": translate_ms,
                "total": int((time.time() - started) * 1000),
            },
            "engine": {"ocr": "RapidOCR", "mt": s.model, "source": source},
        }

        await asyncio.to_thread(self._persist, page, image, raw)
        self.session.add(page)
        self.session.set_status(
            "ready",
            f"第 {page['index']} 页就绪（识别 {ocr_ms/1000:.1f}s，翻译 {translate_ms/1000:.1f}s）",
            page_id,
        )

    # ---- 辅助 ----

    def _persist(self, page: dict, image, raw) -> None:
        self.session.path_for(page["id"], "view").write_bytes(
            imaging.encode_jpeg(image, 88)
        )
        self.session.path_for(page["id"], "raw").write_bytes(
            imaging.encode_jpeg(raw, 88)
        )
        self.session.path_for(page["id"], "thumb").write_bytes(imaging.thumb(image))

    def _context(self, limit: int) -> dict:
        for page in reversed(self.session.pages):
            blocks = page.get("blocks") or []
            en = " ".join(b.get("text", "") for b in blocks).strip()
            zh = "".join(b.get("zh", "") for b in blocks).strip()
            if en:
                return {"en": en[-limit:], "zh": zh[-limit:]}
        return {}

    def _find_duplicate(self, signature: int, blocks: list[dict]) -> dict | None:
        text = " ".join(b["text"] for b in blocks)[:1500]
        for page in self.session.pages:
            if imaging.hamming(signature, int(page.get("hash", 0))) > 6:
                continue
            previous = str(page.get("source_text", ""))[:1500]
            if difflib.SequenceMatcher(None, text, previous).ratio() > 0.55:
                return page
        return None
