"""启动入口。

生成自签证书（iOS 上必须走 HTTPS 才能开摄像头），起服务，打印手机端地址。
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import CERT_DIR, DATA_DIR, FROZEN, Config  # noqa: E402


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def find_openssl() -> tuple[str, Path | None] | None:
    """返回 (openssl 可执行文件, 配置文件)。Windows 上打包的 openssl
    常常找不到自带的 openssl.cnf，必须把 -config 显式指给它。"""
    candidates = [
        shutil.which("openssl"),
        str(Path(sys.prefix) / "Library" / "bin" / "openssl.exe"),
        "D:/Git/usr/bin/openssl.exe",
        "C:/Program Files/Git/usr/bin/openssl.exe",
        "C:/Program Files/OpenSSL-Win64/bin/openssl.exe",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        exe = Path(candidate)
        for conf in (exe.parent.parent / "ssl" / "openssl.cnf", exe.parent / "openssl.cnf"):
            if conf.exists():
                return str(exe), conf
        return str(exe), None
    return None


def _cert_with_cryptography(ip: str, cert: Path, key: Path) -> None:
    """用 cryptography 自己签一张。打包之后机器上不一定有 openssl，这条路最稳。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nice-reading-lens")])
    hosts = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        hosts.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        hosts.append(x509.DNSName(ip))

    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=820))
        .add_extension(x509.SubjectAlternativeName(hosts), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    key.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _cert_with_openssl(ip: str, cert: Path, key: Path) -> None:
    tool = find_openssl()
    if not tool:
        raise RuntimeError("系统里没有 openssl")
    openssl, conf = tool
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "820",
        "-subj", "/CN=nice-reading-lens",
        "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{ip}",
    ]
    if conf:
        cmd[2:2] = ["-config", str(conf)]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip())


def ensure_cert(ip: str) -> tuple[Path, Path]:
    """证书里必须带上局域网 IP，否则 iOS 会把它当成另一个站点。"""
    cert = CERT_DIR / "cert.pem"
    key = CERT_DIR / "key.pem"
    stamp = CERT_DIR / "issued-for.txt"
    # SAN 写在 DER 里，纯文本搜不到，所以另存一份「这张是给谁签的」
    if (
        cert.exists()
        and key.exists()
        and stamp.exists()
        and stamp.read_text("utf-8", "ignore").strip() == ip
    ):
        return cert, key

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  正在为 {ip} 签发一张自签证书…")
    try:
        _cert_with_cryptography(ip, cert, key)
    except ImportError:
        try:
            _cert_with_openssl(ip, cert, key)
        except RuntimeError as exc:
            sys.exit(f"生成证书失败：{exc}")
    stamp.write_text(ip, "utf-8")
    return cert, key


def check_ollama() -> None:
    """启动时体检一下翻译后端，别等用户翻完页才发现没有中文。

    这只是个提示，任何意外都不该拦住服务——之前就因为少 import 一个模块，
    整个程序打印完地址之后直接崩掉。
    """
    try:
        settings = Config().value
    except Exception:
        return

    url = settings.ollama_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=2.0) as response:
            payload = json.loads(response.read())
        models = [str(m.get("name", "")) for m in payload.get("models", [])]
    except Exception:
        print(f"  ⚠ 连不上 Ollama（{url}）。识别照常，但不会出中文。")
        print("    去 https://ollama.com/download 装一个，然后 ollama pull qwen3:8b")
        return

    if not models:
        print("  ⚠ Ollama 在跑，但一个模型都没有。先执行：ollama pull qwen3:8b")
    elif settings.model not in models:
        print(f"  ⚠ 没找到模型 {settings.model}，当前有：{', '.join(models[:6])}")
        print(f"    可以 ollama pull {settings.model}，或在设置里换成已有的模型")
    else:
        print(f"  翻译模型：{settings.model}")


def print_qr(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="纸质书阅读助手")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--no-tls", action="store_true", help="本地调试用，手机浏览器不给开摄像头")
    parser.add_argument("--open", action="store_true", help="启动时打开电脑端阅读页")
    parser.add_argument("--no-open", action="store_true", help="打包版默认会开浏览器，用它关掉")
    args = parser.parse_args()

    import uvicorn

    ip = lan_ip()
    scheme = "http" if args.no_tls else "https"
    ssl_args: dict = {}
    if not args.no_tls:
        cert, key = ensure_cert(ip)
        ssl_args = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    view_url = f"{scheme}://localhost:{args.port}/view"
    capture_url = f"{scheme}://{ip}:{args.port}/capture"

    print("\n  纸质书阅读助手已启动\n")
    print(f"  电脑端阅读：{view_url}")
    print(f"  手机端取景：{capture_url}")
    print(f"  数据目录：　{DATA_DIR}")
    check_ollama()
    if not args.no_tls:
        print("\n  手机首次打开会提示证书不受信任，点「显示详情 → 继续访问」即可。")
    print("\n  这个窗口关掉服务就停了。按 Ctrl+C 也可以。\n")
    print_qr(capture_url)
    print()

    open_browser = args.open or (FROZEN and not args.no_open)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(view_url)).start()

    os.environ["READING_HELPER_HOST"] = ip
    os.environ["READING_HELPER_PORT"] = str(args.port)
    os.environ["READING_HELPER_SCHEME"] = scheme

    from app.server import create_app

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
        **ssl_args,
    )


if __name__ == "__main__":
    main()
