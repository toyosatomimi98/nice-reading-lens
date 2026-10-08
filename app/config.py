"""运行期配置。改动集中写进 config.json，改完即时生效，不用重启。

这里要区分两类路径，打包之后它们不在一棵树上：
  - 只读资源（web 页面、OCR 模型）跟着 exe 走，PyInstaller 解包在临时目录里；
  - 可写数据（页面图、配置、证书）不能放在 Program Files，放到 %LOCALAPPDATA%。
源码直接运行时，两者都还在项目目录里，跟以前一样。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def _resource_dir() -> Path:
    """只读资源在哪儿。"""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parents[1]


def _data_dir() -> Path:
    """可写数据在哪儿。"""
    if not FROZEN:
        return Path(__file__).resolve().parents[1] / "data"
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "NiceReadingLens"


ROOT = _resource_dir()
DATA_DIR = _data_dir()
PAGES_DIR = DATA_DIR / "pages"
WEB_DIR = ROOT / "web"
CERT_DIR = DATA_DIR / "certs"


@dataclass
class Settings:
    # ---- 翻译（走本地 ollama）----
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    temperature: float = 0.2
    num_ctx: int = 8192
    context_chars: int = 320          # 上一页结尾带入的前文长度
    batch_chars: int = 1500           # 单次请求最多塞多少英文字符

    # ---- 翻页检测（只作用于手机回传的低分辨率预览帧）----
    probe_interval_ms: int = 800
    probe_width: int = 480
    # 判据用「墨迹掩码的杰卡德距离」，对页面有多满、整体亮不亮都不敏感。
    # 实测：同一页带噪点约 0.03，翻页约 0.77，整页变亮约 0.04。
    change_jaccard: float = 0.30      # 与基准帧差这么多，算「疑似翻页」
    stable_jaccard: float = 0.10      # 相邻两帧差低于此值，算「画面稳住了」
    stable_frames: int = 3
    cooldown_ms: int = 4000           # 确认一次之后静默多久

    # ---- 版面 ----
    split_mode: str = "auto"          # auto / on / off
    split_order: str = "lr"           # lr 左页在前；rl 右页在前
    rectify: bool = True
    auto_rotate: bool = True          # 横屏拍歪的书页自动转正

    # ---- 阅读呈现 ----
    reading_mode: str = "standard"    # light / standard / immersive
    auto_switch: bool = True
    keep_anchor: bool = True
    show_original: bool = False       # 轻量/沉浸模式改用未摆正的原图，方便核对
    immersive_source: bool = False    # 沉浸模式下是否叠一层原文

    # ---- 识别 ----
    min_confidence: float = 0.55


_lock = threading.Lock()


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (DATA_DIR / "config.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._s = self._load()

    def _load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            return Settings()
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in raw.items() if k in known})

    @property
    def value(self) -> Settings:
        return self._s

    def patch(self, changes: dict) -> Settings:
        """只接受已知字段，并按现有字段的类型做一次收敛转换。"""
        with _lock:
            data = asdict(self._s)
            allowed = {f.name for f in fields(Settings)}
            for key, raw in (changes or {}).items():
                if key not in allowed or raw is None:
                    continue
                current = getattr(self._s, key)
                try:
                    if isinstance(current, bool):
                        value = bool(raw)
                    elif isinstance(current, int):
                        value = int(round(float(raw)))
                    elif isinstance(current, float):
                        value = float(raw)
                    else:
                        value = str(raw)
                except (TypeError, ValueError):
                    continue
                data[key] = value
            self._s = Settings(**data)
            self.save()
        return self._s

    def save(self) -> None:
        self.path.write_text(
            json.dumps(asdict(self._s), ensure_ascii=False, indent=2), "utf-8"
        )
