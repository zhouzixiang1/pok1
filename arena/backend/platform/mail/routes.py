"""管理员邮件模板管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.dependencies import require_admin
from ..store import Store
from . import Mailer, render_template

router = APIRouter(prefix="/api/admin", tags=["admin-mail"])


class TemplateUpdate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    body_html: str = ""
    body_text: str = ""


class TestSendReq(BaseModel):
    to: str = Field(..., min_length=3)
    username: str = "测试用户"
    code: str = "123456"
    expires_minutes: int = 30


@router.get("/email-templates")
async def list_templates(request: Request,
                         _: dict = Depends(require_admin)) -> dict:
    store: Store = request.app.state.platform_store
    return {"templates": store.list_email_templates()}


@router.get("/email-outbox")
async def list_outbox(request: Request, limit: int = 50,
                      _: dict = Depends(require_admin)) -> dict:
    store: Store = request.app.state.platform_store
    return {"items": store.list_email_outbox(limit=min(limit, 200))}


@router.get("/email-templates/{key}")
async def get_template(key: str, request: Request,
                       _: dict = Depends(require_admin)) -> dict:
    store: Store = request.app.state.platform_store
    tpl = store.get_email_template(key)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"template": tpl}


@router.put("/email-templates/{key}")
async def update_template(key: str, body: TemplateUpdate, request: Request,
                          _: dict = Depends(require_admin)) -> dict:
    store: Store = request.app.state.platform_store
    if not store.get_email_template(key):
        raise HTTPException(status_code=404, detail="模板不存在")
    tpl = store.upsert_email_template(
        key, subject=body.subject,
        body_html=body.body_html, body_text=body.body_text)
    return {"template": tpl}


@router.post("/email-templates/{key}/test-send")
async def test_send(key: str, body: TestSendReq, request: Request,
                    _: dict = Depends(require_admin)) -> dict:
    store: Store = request.app.state.platform_store
    mailer: Mailer = request.app.state.platform_mailer
    tpl = store.get_email_template(key)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    ctx = {
        "username": body.username,
        "code": body.code,
        "expires_minutes": body.expires_minutes,
    }
    subject = render_template(tpl["subject"], ctx)
    html = render_template(tpl["body_html"], ctx)
    text = render_template(tpl["body_text"], ctx)
    try:
        mailer.send(body.to, subject, body_text=text, body_html=html)
        store.add_email_outbox(body.to, subject, template_key=key, status="sent")
    except Exception as exc:
        store.add_email_outbox(body.to, subject, template_key=key,
                               status="failed", error=str(exc)[:500])
        raise HTTPException(status_code=502, detail=f"发信失败: {exc}") from exc
    return {"ok": True, "message": f"已试发到 {body.to}"}
