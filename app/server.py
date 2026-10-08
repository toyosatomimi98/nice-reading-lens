"""FastAPI 服务：给手机用的是取景页，给电脑用的是阅读页。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import CERT_DIR, DATA_DIR, PAGES_DIR, WEB_DIR, Config
from .pipeline import Pipeline
from .store import Session


def lan_ip() -> str:
    """拿本机在局域网里的地址。连不出去就退回回环地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def create_app() -> FastAPI:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    CERT_DIR.mkdir(parents=True, exist_ok=True)

    host = os.environ.get("READING_HELPER_HOST", "")
    port = int(os.environ.get("READING_HELPER_PORT", "8443"))
    scheme = os.environ.get("READING_HELPER_SCHEME", "https")

    config = Config()
    session = Session(PAGES_DIR)
    pipeline = Pipeline(
        session, config, DATA_DIR / "glossary.json", DATA_DIR / "mt_cache.json"
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pipeline.start()
        yield
        await pipeline.stop()

    app = FastAPI(
        title="纸质书阅读助手", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.config = config
    app.state.session = session
    app.state.pipeline = pipeline
    app.state.info = {
        "ip": host or lan_ip(),
        "port": port,
        "scheme": scheme,
    }
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # ---- 页面 ----

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/capture")
    async def capture() -> FileResponse:
        return FileResponse(WEB_DIR / "capture.html")

    @app.get("/view")
    async def viewer() -> FileResponse:
        return FileResponse(WEB_DIR / "viewer.html")

    # ---- 取景接口（手机）----

    @app.post("/api/probe")
    async def probe(
        frame: UploadFile = File(...),
        w: int = Form(default=0),
        h: int = Form(default=0),
    ) -> JSONResponse:
        data = await frame.read()
        # 电脑端点过「采集」就先把它兑现，优先级高于自动翻页判定
        manual = session.take_capture_request()
        try:
            result = await asyncio.to_thread(pipeline.probe, data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if manual:
            result["action"] = "capture"
            result["verdict"] = "manual"
        session.touch_camera(result.get("verdict"), (w, h), data)
        session.hub.publish({"type": "camera", "camera": session.camera_state()})
        s = config.value
        result["probe_interval_ms"] = s.probe_interval_ms
        result["probe_width"] = s.probe_width
        return JSONResponse(result)

    @app.get("/api/camera")
    async def camera_state() -> JSONResponse:
        return JSONResponse(session.camera_state())

    @app.get("/api/camera/frame")
    async def camera_frame() -> Response:
        """最新一帧预览。前端拿它当「手机还在看着呢」的可视化。"""
        if not session.preview:
            return Response(status_code=204)
        return Response(
            content=session.preview,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/capture")
    async def request_capture() -> JSONResponse:
        """电脑端让手机马上抓一张，不用等它自己判定翻页。"""
        session.request_capture()
        return JSONResponse({"ok": True, "camera": session.camera_state()})

    @app.post("/api/page")
    async def submit_page(image: UploadFile = File(...)) -> JSONResponse:
        data = await image.read()
        if len(data) < 1024:
            raise HTTPException(status_code=400, detail="图像太小")
        dropped = pipeline.submit(data, "phone")
        return JSONResponse({"accepted": True, "dropped_pending": dropped})

    @app.post("/api/ingest")
    async def ingest(image: UploadFile = File(...)) -> JSONResponse:
        """桌面端手动丢一张图进来，方便没有手机时验证。"""
        data = await image.read()
        pipeline.submit(data, "manual")
        return JSONResponse({"accepted": True})

    # ---- 阅读接口（电脑）----

    @app.get("/api/doc")
    async def doc() -> JSONResponse:
        return JSONResponse(session.snapshot())

    @app.get("/api/page/{page_id}/{kind}")
    async def page_image(page_id: str, kind: str) -> FileResponse:
        if kind not in ("view", "raw", "thumb"):
            raise HTTPException(status_code=404, detail="未知的图像类型")
        path = session.path_for(page_id, kind)
        if not path.exists():
            raise HTTPException(status_code=404, detail="图像不存在")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        from dataclasses import asdict

        return JSONResponse(asdict(config.value))

    @app.post("/api/config")
    async def set_config(patch: dict) -> JSONResponse:
        from dataclasses import asdict

        updated = config.patch(patch)
        session.hub.publish({"type": "settings", "settings": asdict(updated)})
        return JSONResponse(asdict(updated))

    @app.get("/api/glossary")
    async def get_glossary() -> JSONResponse:
        return JSONResponse(pipeline.glossary())

    @app.put("/api/glossary")
    async def put_glossary(payload: dict) -> JSONResponse:
        return JSONResponse(pipeline.save_glossary(payload))

    @app.post("/api/reset")
    async def reset() -> JSONResponse:
        session.reset()
        pipeline.gate.reset()
        session.set_status("idle", "已清空，等待翻页")
        return JSONResponse({"ok": True})

    @app.get("/api/info")
    async def info() -> JSONResponse:
        base = f"{app.state.info['scheme']}://{app.state.info['ip']}:{app.state.info['port']}"
        return JSONResponse(
            {
                **app.state.info,
                "capture_url": f"{base}/capture",
                "view_url": f"{base}/view",
            }
        )

    @app.get("/api/qr")
    async def qr() -> Response:
        import qrcode

        base = f"{app.state.info['scheme']}://{app.state.info['ip']}:{app.state.info['port']}"
        img = qrcode.make(f"{base}/capture", box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    # ---- 实时通道 ----

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        from dataclasses import asdict

        await websocket.accept()
        queue = session.hub.subscribe()

        async def pump() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        sender = asyncio.create_task(pump())
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    **session.snapshot(),
                    "settings": asdict(config.value),
                }
            )
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = message.get("type")
                if kind == "config":
                    config.patch(message.get("patch") or {})
                    session.hub.publish(
                        {"type": "settings", "settings": asdict(config.value)}
                    )
                elif kind == "reset":
                    session.reset()
                    pipeline.gate.reset()
                    session.set_status("idle", "已清空，等待翻页")
                elif kind == "focus" and message.get("id"):
                    session.focus(str(message["id"]), "manual")
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            session.hub.unsubscribe(queue)

    return app
