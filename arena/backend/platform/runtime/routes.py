"""Bot 管理 API 路由:上传/列表/详情/版本/上下架/删除(里程碑 3)。

挂载到主 app 的 ``/api/bots`` 前缀(里程碑 5 的 main.py 集成)。

端点:
- POST   /api/bots                 上传新 bot(multipart: zip + 元数据)
- POST   /api/bots/{id}/versions   上传新版本
- GET    /api/bots                 列 bot(我的/公开的/含内置)
- GET    /api/bots/{id}            详情
- GET    /api/bots/{id}/versions   版本历史
- PATCH  /api/bots/{id}            更新元数据(display_name/description/is_public)
- POST   /api/bots/{id}/activate   上架
- POST   /api/bots/{id}/deactivate 下架
- DELETE /api/bots/{id}            删除(有对局引用时拒绝)
- POST   /api/bots/register-builtin admin 注册内置 bot 库
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..auth.dependencies import require_admin, require_user
from .bot_manager import BotManager
from .builder import BotBuildError

router = APIRouter(prefix="/api/bots", tags=["bots"])


def _get_bot_manager(request: Request) -> BotManager:
    mgr = getattr(request.app.state, "platform_bot_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="bot 管理未启用")
    return mgr


def _err(exc: BotBuildError) -> HTTPException:
    code_to_status = {
        "entry_not_found": 400, "bad_zip": 400, "too_large": 413,
        "too_many_files": 400, "path_traversal": 400, "bad_protocol": 400,
        "unsupported_lang": 400, "build_failed": 500, "no_bot": 404,
        "no_bridge": 503,
    }
    return HTTPException(status_code=code_to_status.get(exc.code, 400),
                         detail=exc.message)


class BotUpdateReq(BaseModel):
    display_name: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=500)
    is_public: bool | None = None


@router.post("")
async def create_bot(request: Request,
                     file: UploadFile = File(...),
                     name: str = Form(...),
                     protocol: str = Form("json"),
                     entry_file: str = Form("main.py"),
                     runtime_lang: str = Form("python"),
                     argv_style: str = Form("flags"),
                     display_name: str = Form(""),
                     description: str = Form(""),
                     upload_note: str = Form(""),
                     user: dict = Depends(require_user)) -> dict:
    """上传新 bot。需登录。argv_style 仅 TCP 协议生效。"""
    if argv_style not in ("flags", "positional", "env"):
        raise HTTPException(status_code=400,
                            detail="argv_style 必须是 flags/positional/env")
    mgr = _get_bot_manager(request)
    raw = await file.read()
    try:
        bot = mgr.create_bot_from_upload(
            user["id"], name, raw, protocol=protocol, entry_file=entry_file,
            runtime_lang=runtime_lang, display_name=display_name,
            description=description, argv_style=argv_style,
            upload_note=upload_note)
    except BotBuildError as exc:
        raise _err(exc)
    return {"bot": bot, "message": "bot 创建成功"}


@router.post("/{bot_id}/versions")
async def upload_version(bot_id: int, request: Request,
                         file: UploadFile = File(...),
                         upload_note: str = Form(""),
                         user: dict = Depends(require_user)) -> dict:
    """上传新版本。只能上传自己的 bot。"""
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    if bot["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="只能给自己的 bot 上传版本")
    raw = await file.read()
    try:
        bot = mgr.upload_new_version(bot_id, raw, upload_note=upload_note)
    except BotBuildError as exc:
        raise _err(exc)
    return {"bot": bot, "message": "新版本上传成功"}


@router.get("")
async def list_bots(request: Request,
                    scope: str = "public",
                    user: dict = Depends(require_user)) -> dict:
    """列 bot。scope=public(可选作对手的全部,含内置)| mine(我的)。"""
    mgr = _get_bot_manager(request)
    if scope == "mine":
        bots = mgr.list_bots(owner_id=user["id"], include_builtin=False)
    else:  # public
        bots = mgr.list_bots(public_only=True, include_builtin=True)
    return {"bots": bots}


@router.get("/{bot_id}")
async def get_bot(bot_id: int, request: Request,
                  user: dict = Depends(require_user)) -> dict:
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    return {"bot": bot}


@router.get("/{bot_id}/versions")
async def list_versions(bot_id: int, request: Request,
                        user: dict = Depends(require_user)) -> dict:
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    versions = mgr.list_bot_versions(bot_id)
    return {"versions": versions}


@router.patch("/{bot_id}")
async def update_bot(bot_id: int, req: BotUpdateReq, request: Request,
                     user: dict = Depends(require_user)) -> dict:
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    if bot["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权修改")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    # is_public 转 int
    if "is_public" in fields:
        fields["is_public"] = 1 if fields["is_public"] else 0
    updated = mgr.store.update_bot(bot_id, **fields)
    from .bot_manager import _bot_view
    return {"bot": _bot_view(updated)}


@router.post("/{bot_id}/activate")
async def activate_bot(bot_id: int, request: Request,
                       user: dict = Depends(require_user)) -> dict:
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    if bot["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权操作")
    return {"bot": mgr.set_active(bot_id, True)}


@router.post("/{bot_id}/deactivate")
async def deactivate_bot(bot_id: int, request: Request,
                         user: dict = Depends(require_user)) -> dict:
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    if bot["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权操作")
    return {"bot": mgr.set_active(bot_id, False)}


@router.delete("/{bot_id}")
async def delete_bot(bot_id: int, request: Request,
                     user: dict = Depends(require_user)) -> dict:
    mgr = _get_bot_manager(request)
    bot = mgr.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="bot 不存在")
    if bot["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权删除")
    try:
        ok = mgr.delete_bot(bot_id)
    except Exception:  # 外键约束(有对局引用)
        raise HTTPException(status_code=409,
                            detail="该 bot 有对局记录,不能删除(可下架)")
    if not ok:
        raise HTTPException(status_code=404, detail="bot 不存在")
    return {"ok": True}


@router.post("/register-builtin")
async def register_builtin(request: Request,
                           _: dict = Depends(require_admin)) -> dict:
    """admin 注册内置 bot 库(national_v*)。幂等。"""
    mgr = _get_bot_manager(request)
    system_uid = getattr(request.app.state, "platform_system_user_id", None)
    if system_uid is None:
        raise HTTPException(status_code=500, detail="系统用户未初始化")
    bots = mgr.register_builtin_bots(system_uid)
    return {"bots": bots, "count": len(bots)}
