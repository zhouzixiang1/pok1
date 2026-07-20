"""Bot Docker 镜像构建器(里程碑 3)。

用户上传 bot 源码包 → 解压校验 → 生成 Dockerfile → docker build。

两种协议两套基础镜像:
- **JSON bot**(protocol='json'):``FROM python:3.12-slim``,挂源码,
  ``CMD ["python", "main.py"]``。平台与 bot 用 stdin/stdout JSON 通信。
- **TCP bot**(protocol='tcp'):在 JSON 镜像基础上,额外打入 ``tcp_bridge.py``
  (里程碑 4),启动时先起桥监听 127.0.0.1:50101,再起 bot 连桥。
  用户 bot 代码零改动(``--host 127.0.0.1 --port 50101`` 在容器内回环成立)。

镜像命名:``arena-bot-<bot_id>:v<version>``

资源限制(里程碑 4 runner 用,构建时设默认):CPU 0.5 核 / 内存 512M。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 镜像 tag 前缀(避免与本机其他镜像冲突)
IMAGE_PREFIX = "arena-bot"
# 允许的入口文件(防路径穿越)
ALLOWED_ENTRY = {"main.py", "national_bot.py", "bot.py", "run.py"}
# 最大源码包大小(50MB,防滥用)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# 解压后最大总大小(200MB)
MAX_EXTRACT_BYTES = 200 * 1024 * 1024
# 解压最多文件数(防 zip bomb)
MAX_FILE_COUNT = 2000


class BotBuildError(Exception):
    """bot 构建/上传业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _dockerfile_json(entry_file: str, runtime_lang: str = "python") -> str:
    """JSON bot 的 Dockerfile(stdin/stdout JSON 通信,无网络)。"""
    if runtime_lang == "python":
        return f"""FROM python:3.12-slim
WORKDIR /app
COPY . /app/
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser
# 无网络模式运行(平台 docker run --network=none)
# stdin/stdout 通信:平台写 JSON 到 stdin,bot 从 stdout 读 JSON
ENTRYPOINT ["python", "{entry_file}"]
"""
    # 预留 cpp/java(里程碑 3 先实现 python,多语言后续)
    raise BotBuildError("unsupported_lang", f"暂不支持 {runtime_lang},目前仅 python")


def _dockerfile_tcp(entry_file: str, runtime_lang: str = "python") -> str:
    """TCP bot 的 Dockerfile(容器内挂 tcp_bridge.py,bot 连回环)。

    桥进程(里程碑 4 的 tcp_bridge.py)被打入镜像,启动时:
    1. 桥先起,监听 127.0.0.1:50101
    2. bot 启动,连 127.0.0.1:50101(回环,容器内永远成立)
    3. 桥用 stdin/stdout 与平台(JSON),用 socket 与 bot(TCP 文本)
    用户 bot 代码零改动。
    """
    if runtime_lang != "python":
        raise BotBuildError("unsupported_lang", f"暂不支持 {runtime_lang},目前仅 python")
    # entrypoint 脚本:先起桥后台,再起 bot(连回环)
    return f"""FROM python:3.12-slim
WORKDIR /app
COPY . /app/
# 桥代理(里程碑 4 提供,构建时从 platform/runtime/ 复制)
COPY tcp_bridge.py /app/_bridge/tcp_bridge.py
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser
# 桥先起(后台监听 50101),bot 再连回环 --host 127.0.0.1 --port 50101
# 平台只与桥 stdin/stdout 通信(JSON),桥翻译成 TCP 喂给 bot
ENTRYPOINT ["python", "/app/_bridge/tcp_bridge.py", "--bot-entry", "{entry_file}"]
"""


def make_dockerfile(*, protocol: str, entry_file: str,
                    runtime_lang: str = "python") -> str:
    """按协议生成 Dockerfile。"""
    if protocol == "json":
        return _dockerfile_json(entry_file, runtime_lang)
    if protocol == "tcp":
        return _dockerfile_tcp(entry_file, runtime_lang)
    raise BotBuildError("bad_protocol", f"未知协议 {protocol}")


def _safe_extract(zip_path: Path, dest: Path) -> list[str]:
    """安全解压 zip:防路径穿越 / 防 zip bomb。返回相对文件路径列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    total_size = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILE_COUNT:
                raise BotBuildError("too_many_files",
                                    f"zip 含 {len(infos)} 个文件,超过上限 {MAX_FILE_COUNT}")
            for info in infos:
                # 防路径穿越(../ 或绝对路径)
                target = (dest / info.filename).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise BotBuildError("path_traversal",
                                        f"非法路径:{info.filename}")
                total_size += info.file_size
                if total_size > MAX_EXTRACT_BYTES:
                    raise BotBuildError("too_large",
                                        f"解压后超 {MAX_EXTRACT_BYTES // 1024 // 1024}MB")
                zf.extract(info, dest)
                if not info.is_dir():
                    files.append(info.filename)
    except zipfile.BadZipFile as exc:
        raise BotBuildError("bad_zip", f"不是有效 zip 文件:{exc}") from exc
    return files


def _find_entry(extracted_files: list[str], entry_file: str) -> str:
    """确认入口文件存在(zip 根目录或任意子目录)。返回相对路径。"""
    # 精确匹配根目录
    if entry_file in extracted_files:
        return entry_file
    # 匹配子目录下的同名入口(取最浅的)
    candidates = [f for f in extracted_files if f.endswith("/" + entry_file)
                  or f == entry_file]
    if candidates:
        return min(candidates, key=len)
    raise BotBuildError("entry_not_found",
                        f"zip 中找不到入口文件 {entry_file}")


def save_upload(raw_bytes: bytes, dest: Path) -> None:
    """保存上传的原始字节。校验大小。"""
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise BotBuildError("too_large",
                            f"上传包 {len(raw_bytes)} 字节,超过 "
                            f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw_bytes)


def checksum(raw_bytes: bytes) -> str:
    """sha256(防重复上传 / 完整性校验)。"""
    return hashlib.sha256(raw_bytes).hexdigest()


def build_bot_image(*, source_dir: Path, protocol: str, entry_file: str,
                    runtime_lang: str, bot_id: int, version: int,
                    bridge_src: Path | None = None) -> str:
    """构建 bot Docker 镜像。返回镜像名 ``arena-bot-<id>:v<version>``。

    source_dir: 已解压的源码目录(含入口文件)。
    bridge_src: TCP 协议时,tcp_bridge.py 的源路径(里程碑 4 提供);
                JSON 协议忽略。
    """
    image = f"{IMAGE_PREFIX}-{bot_id}:v{version}"
    dockerfile = make_dockerfile(protocol=protocol, entry_file=entry_file,
                                 runtime_lang=runtime_lang)
    # 写 Dockerfile 到源码目录
    (source_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    # TCP bot 需要打入桥代理(里程碑 4 的 tcp_bridge.py)
    if protocol == "tcp":
        if bridge_src is None or not bridge_src.exists():
            raise BotBuildError("no_bridge",
                                "TCP 协议构建需要 tcp_bridge.py(里程碑4提供)")
        bridge_dir = source_dir / "tcp_bridge.py"  # Dockerfile COPY 到 /app/_bridge/
        shutil.copy2(bridge_src, bridge_dir)
    # docker build
    cmd = ["docker", "build", "-t", image, str(source_dir)]
    logger.info("building bot image %s (proto=%s entry=%s)",
                image, protocol, entry_file)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        tail = proc.stderr[-2000:] if proc.stderr else proc.stdout[-2000:]
        raise BotBuildError("build_failed",
                            f"docker build 失败(exit={proc.returncode}):{tail}")
    logger.info("built %s", image)
    return image


def remove_bot_image(image: str) -> bool:
    """删除 bot 镜像(bot 下架/删版本时清理)。"""
    proc = subprocess.run(["docker", "rmi", "-f", image],
                          capture_output=True, text=True, timeout=30)
    return proc.returncode == 0
