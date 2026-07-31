"""Bot 管理器:上传 / 构建 / 版本管理 / 内置 bot 注册(里程碑 3)。

编排 Store(元数据)+ builder(Docker 镜像)。

职责:
- 上传新 bot(.zip)→ 解压校验 → 建 bots 记录 + bot_versions → docker build
- 新版本上传(已存在的 bot)→ 版本 +1 → 重建镜像
- 内置 bot 注册:把现有 national_v* 打包为预置 bot(无需上传)
- 列表/查询/删除/上下架

文件布局:
    bot_uploads/<bot_id>/v<version>/
        source.zip          原始上传包
        src/                解压后的源码(含 Dockerfile,构建上下文)
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..auth.auth_manager import _safe_user  # 复用脱敏
from ..store import Store
from ..store.schema import PROTO_JSON, PROTO_TCP
from .builder import (BotBuildError, IMAGE_PREFIX, build_bot_image, checksum,
                      remove_bot_image, save_upload, _safe_extract, _find_entry)

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path("bot_uploads")
# 内置 bot 源(本仓库的 bots/national_v*,每个版本一个)
BUILTIN_SOURCE_ROOT = Path("bots")
# tcp_bridge.py 源(里程碑 4 提供,构建 TCP bot 镜像时打入)
BRIDGE_SRC = Path(__file__).parent / "tcp_bridge.py"


class BotManager:
    """bot 上传/构建/版本管理。"""

    def __init__(self, store: Store, *,
                 upload_root: Path | str = UPLOAD_ROOT,
                 bridge_src: Path | None = None) -> None:
        self.store = store
        self.upload_root = Path(upload_root)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.bridge_src = Path(bridge_src) if bridge_src else BRIDGE_SRC

    # ── 上传新 bot ──────────────────────────────────────────

    def create_bot_from_upload(self, owner_id: int, name: str, raw_zip: bytes, *,
                               protocol: str = PROTO_JSON,
                               entry_file: str = "main.py",
                               runtime_lang: str = "python",
                               display_name: str = "",
                               description: str = "",
                               argv_style: str = "flags",
                               upload_note: str = "",
                               build: bool = True) -> dict:
        """上传并创建新 bot。返回 bot 记录(含构建出的镜像名)。

        流程:校验 → 建 bots 记录(临时无镜像)→ 解压 → 建版本 → 构建。
        构建失败回滚(删 bot 记录 + 文件)。argv_style 仅 TCP 协议生效。
        """
        # 1. 先建 bots 记录(占位,镜像构建成功后回填)
        bot = self.store.create_bot(
            owner_id, name, protocol=protocol, entry_file=entry_file,
            runtime_lang=runtime_lang, display_name=display_name,
            description=description, argv_style=argv_style)
        try:
            version = self._ingest_version(
                bot["id"], raw_zip, entry_file=entry_file,
                upload_note=upload_note, build=build)
        except (BotBuildError, Exception) as exc:
            # 失败回滚:删 bot 记录(级联删 versions)+ 清文件
            self.store.delete_bot(bot["id"])
            self._cleanup_bot_dir(bot["id"])
            raise
        bot = self.store.get_bot(bot["id"])
        return _bot_view(bot)

    def upload_new_version(self, bot_id: int, raw_zip: bytes, *,
                           entry_file: str | None = None,
                           upload_note: str = "",
                           build: bool = True) -> dict:
        """已存在的 bot 上传新版本。版本号 +1。"""
        bot = self.store.get_bot(bot_id)
        if bot is None:
            raise BotBuildError("no_bot", "bot 不存在")
        entry = entry_file or bot["entry_file"]
        version = self._ingest_version(
            bot_id, raw_zip, entry_file=entry,
            upload_note=upload_note, build=build)
        return _bot_view(self.store.get_bot(bot_id))

    def _ingest_version(self, bot_id: int, raw_zip: bytes, *,
                        entry_file: str, upload_note: str,
                        build: bool) -> dict:
        """处理一次上传:存包 → 解压 → 校验入口 → 建版本记录 → 构建镜像。"""
        cs = checksum(raw_zip)
        bot = self.store.get_bot(bot_id)
        version_dir = self.upload_root / str(bot_id) / f"v{bot['current_version'] + 1}"
        version_dir.mkdir(parents=True, exist_ok=True)
        zip_path = version_dir / "source.zip"
        save_upload(raw_zip, zip_path)
        src_dir = version_dir / "src"
        files = _safe_extract(zip_path, src_dir)
        # 找入口文件
        entry_rel = _find_entry(files, entry_file)
        # 建版本记录
        vrec = self.store.add_bot_version(
            bot_id, source_path=str(version_dir / "src"),
            upload_note=upload_note, checksum=cs)
        version = vrec["version"]
        # 构建镜像
        image = ""
        if build:
            image = build_bot_image(
                source_dir=src_dir, protocol=bot["protocol"],
                entry_file=entry_rel, runtime_lang=bot["runtime_lang"],
                bot_id=bot_id, version=version,
                bridge_src=self.bridge_src if bot["protocol"] == PROTO_TCP else None,
                argv_style=bot.get("argv_style", "flags"))
        # 回填 bots 表的 docker_image / source_path
        self.store.update_bot(
            bot_id, docker_image=image,
            source_path=str(version_dir / "src"),
            entry_file=entry_rel)
        return vrec

    # ── 查询 / 列表 ─────────────────────────────────────────

    def get_bot(self, bot_id: int) -> dict | None:
        bot = self.store.get_bot(bot_id)
        return _bot_view(bot) if bot else None

    def list_bots(self, *, owner_id: int | None = None,
                  public_only: bool = False,
                  include_builtin: bool = True) -> list[dict]:
        rows = self.store.list_bots(owner_id=owner_id, public_only=public_only,
                                    active_only=True, include_builtin=include_builtin)
        return [_bot_view(r) for r in rows]

    def list_bot_versions(self, bot_id: int) -> list[dict]:
        return self.store.list_bot_versions(bot_id)

    # ── 下架 / 删除 ─────────────────────────────────────────

    def set_active(self, bot_id: int, active: bool) -> dict | None:
        """上架/下架(软开关,不删镜像)。"""
        return _bot_view(self.store.update_bot(bot_id, is_active=1 if active else 0))

    def delete_bot(self, bot_id: int) -> bool:
        """删 bot:删镜像 + 删文件 + 删 DB 记录(级联删 versions)。

        注意:有对局引用的 bot 不能删(外键保护历史),会抛 IntegrityError。
        """
        bot = self.store.get_bot(bot_id)
        if not bot:
            return False
        # 删镜像(各版本)
        # 简化:只删当前镜像;历史版本镜像靠 docker prune 清
        if bot["docker_image"]:
            remove_bot_image(bot["docker_image"])
        ok = self.store.delete_bot(bot_id)
        if ok:
            self._cleanup_bot_dir(bot_id)
        return ok

    def _cleanup_bot_dir(self, bot_id: int) -> None:
        d = self.upload_root / str(bot_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # ── 内置 bot 注册 ───────────────────────────────────────

    def register_builtin_bots(self, system_user_id: int, *,
                              versions: list[str] | None = None) -> list[dict]:
        """把现有 bots/national_v* 注册为平台预置 bot。

        用 national_bot.py(TCP 入口)注册,这样既能走 TCP 通道也能在新平台用。
        system_user_id:内置 bot 的"拥有者"(通常是一个 system 账号)。
        versions:要注册的版本(如 ['national_v141','national_v142']);None=全部。
        """
        results: list[dict] = []
        if not BUILTIN_SOURCE_ROOT.exists():
            logger.warning("内置 bot 源目录不存在:%s", BUILTIN_SOURCE_ROOT)
            return results
        if versions is None:
            versions = sorted(d.name for d in BUILTIN_SOURCE_ROOT.iterdir()
                              if d.is_dir() and d.name.startswith("national_v"))
        for vname in versions:
            src = BUILTIN_SOURCE_ROOT / vname
            if not src.exists():
                continue
            # 已注册则跳过(幂等),但补设 argv_style=flags(national_v* 用旗标解析)
            existing = self.store.get_bot_by_owner_name(system_user_id, vname)
            if existing:
                # 内置 bot 用 --host/--port 旗标;旧记录可能缺该字段或被改错
                if existing.get("argv_style") != "flags":
                    self.store.update_bot(existing["id"], argv_style="flags")
                    existing = self.store.get_bot(existing["id"])
                results.append(_bot_view(existing))
                continue
            # 注册为 TCP 协议内置 bot(用 national_bot.py 入口,兼容 TCP 通道)
            bot = self.store.create_bot(
                system_user_id, vname, protocol=PROTO_TCP,
                entry_file="national_bot.py", runtime_lang="python",
                display_name=vname, description=f"平台预置 {vname}",
                argv_style="flags",  # national_v* 的 national_bot.py 用 argparse 旗标
                is_builtin=True, is_public=True)
            # 内置 bot 的源码路径直接指向仓库 bots/ 目录(不复制)
            self.store.update_bot(
                bot["id"], source_path=str(src),
                docker_image="")  # 镜像按需构建(build_builtin_image 懒构建)
            results.append(_bot_view(bot))
        return results

    def build_builtin_image(self, bot_id: int) -> str:
        """为内置 bot 构建镜像(懒构建:首次 challenge 时 orchestrator 调用)。

        内置 bot 源码在 bots/<name>/(仓库内),复制到临时目录后构建。
        镜像名 ``arena-bot-<id>:builtin``。构建完回填 bots.docker_image。
        返回镜像名。已构建则跳过返回现有镜像。
        """
        bot = self.store.get_bot(bot_id)
        if not bot:
            raise BotBuildError("no_bot", "bot 不存在")
        if bot.get("docker_image"):
            return bot["docker_image"]  # 已构建
        if not bot.get("is_builtin"):
            raise BotBuildError("not_builtin", "仅内置 bot 可用此方法")
        source_path = bot.get("source_path") or ""
        if not source_path:
            # source_path 为空 → Path('')=CWD,会把整个项目根打包进镜像
            # (曾导致 national_v143 镜像含整个仓库、入口文件丢失)
            raise BotBuildError(
                "no_source",
                f"内置 bot {bot['name']} 缺少 source_path,无法定位源码")
        src = Path(source_path)
        if not src.exists():
            raise BotBuildError("no_source", f"内置 bot 源码不存在: {src}")
        if not src.is_dir():
            raise BotBuildError("no_source", f"内置 bot 源码不是目录: {src}")
        # 复制到临时构建目录(避免污染源码目录)
        import tempfile
        build_dir = Path(tempfile.mkdtemp(prefix=f"builtin_{bot_id}_"))
        try:
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy2(f, build_dir / f.name)
            image = build_bot_image(
                source_dir=build_dir, protocol=bot["protocol"],
                entry_file=bot["entry_file"], runtime_lang=bot["runtime_lang"],
                bot_id=bot_id, version=999,  # builtin 用固定 tag
                bridge_src=self.bridge_src if bot["protocol"] == PROTO_TCP else None,
                argv_style=bot.get("argv_style", "flags"))
            # 镜像名 build_bot_image 生成的是 arena-bot-<id>:v999,
            # retag 成 arena-bot-<id>:builtin 更语义化
            import subprocess
            builtin_image = f"{IMAGE_PREFIX}-{bot_id}:builtin"
            if image != builtin_image:
                subprocess.run(["docker", "tag", image, builtin_image],
                               check=False, capture_output=True, timeout=30)
            self.store.update_bot(bot_id, docker_image=builtin_image)
            logger.info("builtin bot %s image built: %s", bot["name"], builtin_image)
            return builtin_image
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)


def _bot_view(bot: dict) -> dict:
    """bot 记录的对外视图(隐藏内部字段,补计算字段)。"""
    return {
        "id": bot["id"],
        "owner_id": bot["owner_id"],
        "name": bot["name"],
        "display_name": bot["display_name"],
        "description": bot["description"],
        "protocol": bot["protocol"],
        "entry_file": bot["entry_file"],
        "runtime_lang": bot["runtime_lang"],
        "argv_style": bot.get("argv_style", "flags"),  # TCP bot 连接参数风格
        "current_version": bot["current_version"],
        "has_image": bool(bot["docker_image"]),
        "is_builtin": bool(bot["is_builtin"]),
        "is_public": bool(bot["is_public"]),
        "is_active": bool(bot["is_active"]),
        "created_at": bot["created_at"],
        "updated_at": bot["updated_at"],
    }
