# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本。

    .venv\\Scripts\\pyinstaller.exe packaging\\NiceReadingLens.spec --noconfirm --clean

出的是「一个目录」而不是单文件 exe。onnxruntime + opencv 加起来两百多兆，
单文件模式每次启动都要先把这些解包到临时目录，开一次要十几秒，不值得。
安装包负责把整个目录铺到 Program Files，用户看到的一样是一个快捷方式。
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

PROJECT = Path(SPECPATH).resolve().parent
ICON = Path(SPECPATH).resolve() / "build" / "icon.ico"

# web 页面是运行时读的，必须一起打进去
datas = [(str(PROJECT / "web"), "web")]
# RapidOCR 的 onnx 模型和 yaml 配置都在包里，要显式捞出来
datas += collect_data_files("rapidocr_onnxruntime")

# onnxruntime 的原生 dll
binaries = collect_dynamic_libs("onnxruntime")

hiddenimports = collect_submodules("rapidocr_onnxruntime") + [
    # uvicorn 的这些模块是按字符串动态导入的，静态分析看不到
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

excludes = [
    "tkinter",
    "matplotlib",
    "pandas",
    "IPython",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "torch",
    "paddle",
]

a = Analysis(
    [str(PROJECT / "run.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NiceReadingLens",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台：手机端地址和二维码要打在这儿
    icon=str(ICON) if ICON.exists() else None,
    version=str(Path(SPECPATH).resolve() / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NiceReadingLens",
)
