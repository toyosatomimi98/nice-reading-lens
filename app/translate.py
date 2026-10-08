"""调本地 ollama 做英译中。

要点：段落编号严格对齐、带词表和上一页结尾做上下文、结果落盘缓存避免重复算。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import httpx

from .config import Settings

MARK = re.compile(r"@@\s*(\d+)\s*@@")
# 模型偶尔会把思考开关当正文吐出来，顺手擦掉
CONTROL = re.compile(r"/?(?:no_?think|think)\b|<\|[^|]*\|>", re.I)


def clean(text: str) -> str:
    return CONTROL.sub("", text or "").strip()

SYSTEM = (
    "你是中文技术图书的译者。把用户给出的英文段落译成简体中文。\n"
    "要求：\n"
    "1. 术语优先按【词表】；词表没有的，同一本书里保持同一种译法。\n"
    "2. 保持原文的段落切分和编号，不要合并、不要拆分。\n"
    "3. 数学符号、变量名、代码、缩写、书名、人名保持原样。\n"
    "4. 语气平实书面，不要口语化，不要解释，不要加译者注。\n"
    "5. 只输出译文。每段译文前面用 @@序号@@ 单独占一行开头。"
)


class Translator:
    def __init__(self, config, cache_path: Path):
        self.config = config
        self.cache_path = Path(cache_path)
        self.cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    # ---- 缓存 ----

    def _load(self) -> None:
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text("utf-8"))
            except Exception:
                self.cache = {}

    def _save(self) -> None:
        if len(self.cache) > 6000:
            self.cache = dict(list(self.cache.items())[-4000:])
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False), "utf-8"
        )

    def _key(self, engine: str, text: str) -> str:
        """缓存按「后端+模型」分桶，换后端不会串到另一边的译文。"""
        return hashlib.sha1(f"{engine}\x00{text}".encode("utf-8")).hexdigest()

    def engine_name(self) -> str:
        s = self.config()
        if s.mt_backend == "openai":
            return f"openai/{s.api_model}"
        return f"ollama/{s.model}"

    # ---- 提示词 ----

    def _prompt(self, segments: list[str], glossary: dict, context: dict) -> str:
        parts: list[str] = []
        if glossary:
            terms = "\n".join(f"- {k} → {v}" for k, v in list(glossary.items())[:80])
            parts.append(f"【词表】\n{terms}")
        if context.get("en"):
            parts.append(f"【上一页结尾的原文】\n{context['en']}")
        if context.get("zh"):
            parts.append(f"【上一页结尾的译文】\n{context['zh']}")
        body = "\n\n".join(
            f"@@{i}@@ {text}" for i, text in enumerate(segments, start=1)
        )
        parts.append(f"【待译段落】\n{body}")
        return "\n\n".join(parts)

    # ---- 调用 ----

    async def _chat(self, client: httpx.AsyncClient, system: str, prompt: str) -> str:
        s = self.config()
        if s.mt_backend == "openai":
            return await self._chat_openai(client, s, system, prompt)
        return await self._chat_ollama(client, s, system, prompt)

    @staticmethod
    def api_key(s) -> str:
        """config 里没填就读环境变量，免得把 key 明文写进配置文件。"""
        return (s.api_key or "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()

    async def _chat_ollama(self, client, s, system: str, prompt: str) -> str:
        payload = {
            "model": s.model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": s.temperature,
                "num_ctx": s.num_ctx,
                "num_predict": 2048,
            },
        }
        response = await client.post(
            f"{s.ollama_url.rstrip('/')}/api/chat", json=payload, timeout=300.0
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Ollama 返回 {response.status_code}：{response.text[:200]}")
        return (response.json().get("message") or {}).get("content", "").strip()

    async def _chat_openai(self, client, s, system: str, prompt: str) -> str:
        """OpenAI 兼容接口。DeepSeek、Moonshot、智谱、硅基流动都是这个形状。"""
        response = await client.post(
            f"{s.api_base.rstrip('/')}/chat/completions",
            json={
                "model": s.api_model,
                "temperature": s.temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            headers={"Authorization": f"Bearer {self.api_key(s)}"},
            timeout=300.0,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"接口返回 {response.status_code}：{response.text[:200]}")
        choices = response.json().get("choices") or []
        if not choices:
            raise RuntimeError("接口没返回内容")
        return (choices[0].get("message") or {}).get("content", "").strip()

    def _parse(self, raw: str, expected: int) -> list[str] | None:
        marks = list(MARK.finditer(raw))
        if not marks:
            return None
        chunks: dict[int, str] = {}
        for i, match in enumerate(marks):
            start = match.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            chunks[int(match.group(1))] = clean(raw[start:end])
        if set(chunks) != set(range(1, expected + 1)):
            return None
        return [chunks[i] for i in range(1, expected + 1)]

    async def translate(
        self,
        texts: list[str],
        glossary: dict | None = None,
        context: dict | None = None,
    ) -> list[str]:
        s = self.config()
        glossary = glossary or {}
        context = context or {}
        engine = self.engine_name()

        if s.mt_backend == "openai" and not self.api_key(s):
            raise RuntimeError("选了云端翻译，但没填 API Key（也可以放环境变量 DEEPSEEK_API_KEY）")

        results: list[str | None] = [None] * len(texts)
        pending: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            hit = self.cache.get(self._key(engine, text))
            if hit:
                results[i] = hit
            else:
                pending.append((i, text))

        if not pending:
            return [r or "" for r in results]

        batches: list[list[int]] = []
        current: list[int] = []
        size = 0
        for index, text in pending:
            if current and size + len(text) > s.batch_chars:
                batches.append(current)
                current, size = [], 0
            current.append(index)
            size += len(text)
        if current:
            batches.append(current)

        async with httpx.AsyncClient() as client:
            for batch in batches:
                segments = [texts[i] for i in batch]
                prompt = self._prompt(segments, glossary, context)
                translated = None
                for attempt in range(2):
                    try:
                        raw = await self._chat(client, SYSTEM, prompt)
                    except Exception:
                        raw = ""
                    translated = self._parse(raw, len(segments))
                    if translated:
                        break
                    prompt = prompt + "\n\n注意：上一次输出缺少编号，请严格按 @@1@@ @@2@@ … 的格式重来一遍。"
                if not translated:
                    # 批量失败就退化成逐段翻译，宁慢不乱
                    translated = []
                    for text in segments:
                        try:
                            raw = await self._chat(
                                client, SYSTEM, self._prompt([text], glossary, {})
                            )
                            parsed = self._parse(raw, 1)
                            translated.append(parsed[0] if parsed else clean(raw))
                        except Exception as exc:
                            translated.append(f"（翻译失败：{exc}）")
                for slot, value in zip(batch, translated):
                    results[slot] = value
                    with self._lock:
                        self.cache[self._key(engine, texts[slot])] = value

        with self._lock:
            self._save()
        return [r or "" for r in results]
