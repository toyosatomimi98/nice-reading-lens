"""一张书页照片进，一份「原文 + 译文 + 坐标」的文档出。"""

from __future__ import annotations

import asyncio
import difflib
import json
import time
from pathlib import Path

from . import imaging
from .gate import PageGate
from .layout import build_blocks, word_score
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
        image = await asyncio.to_thread(imaging.enhance, raw)

        # 只跑检测不跑识别，比整条流程便宜得多。这一次拿到的文字框同时用来
        # 判页面朝向，以及反推页面四边形。
        boxes: list = []
        if s.auto_rotate or s.rectify:
            boxes = await asyncio.to_thread(self.ocr.detect, image)

        # 手机横着拿的时候，书页在画面里是躺着的。先转正，后面的分割和识别才有意义。
        rotated = False
        if s.auto_rotate and imaging.looks_sideways(boxes):
            turns = await asyncio.to_thread(self._pick_turn, image)
            image = await asyncio.to_thread(imaging.quarter_turn, image, turns)
            rotated = True
            boxes = await asyncio.to_thread(self.ocr.detect, image)

        # 截取并摆正。优先用文字块反推：纸的轮廓经常靠不住（白纸浅桌、书页弯曲、
        # 页边被手压住），但文字一定在纸上。文字框太少时才退回去找纸边。
        rectified = False
        if s.rectify:
            quad = imaging.page_quad_from_text(boxes, image.shape)
            if quad is not None:
                warped = await asyncio.to_thread(imaging.warp_quad, image, quad)
                if warped is not None:
                    image, rectified = warped, True
            if not rectified:
                image, rectified = await asyncio.to_thread(imaging.rectify, image)

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
            "rotated": rotated,
            "split": [p["label"] for p in pieces],
            "hash": signature,
            "source_text": " ".join(texts)[:4000],
            "blocks": blocks,
            "elapsed_ms": {
                "ocr": ocr_ms,
                "translate": translate_ms,
                "total": int((time.time() - started) * 1000),
            },
            "engine": {"ocr": "RapidOCR", "mt": self.translator.engine_name(), "source": source},
        }

        await asyncio.to_thread(self._persist, page, image, raw)
        self.session.add(page)
        self.session.set_status(
            "ready",
            f"第 {page['index']} 页就绪（识别 {ocr_ms/1000:.1f}s，翻译 {translate_ms/1000:.1f}s）",
            page_id,
        )

    # ---- 辅助 ----

    def _pick_turn(self, image):
        """躺倒的页面往哪边转？两个方向各识别一遍，谁读出来的行多就听谁的。

        只在确认页面躺倒时才走这条路，正常页面一次都不会付这个成本。

        为什么要看行数而不是词频：方向转错时文字上下颠倒，识别器基本读不出东西，
        实测正确方向 17 行、错误方向 2 行，差得很开。词频反而会骗人——糊掉的
        文本照样能凑出像词的东西，两个方向各 0.11 分不出高下，所以只当平局时的参考。

        另外判方向必须用原分辨率，而且要先转再裁：缩到 900 宽文字只剩十几个像素，
        两边一样烂；横着裁一条会在竖排的页面上切断每一行。
        """
        best_turn, best_key = 1, (-1, -1.0)
        for turns in (1, 3):  # 1 逆时针，3 顺时针
            rotated = imaging.quarter_turn(image, turns)
            height = rotated.shape[0]
            strip = rotated[int(height * 0.08):int(height * 0.55)]
            lines = self.ocr.read(strip)
            key = (len(lines), word_score([line["text"] for line in lines]))
            if key > best_key:
                best_turn, best_key = turns, key
        return best_turn

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
