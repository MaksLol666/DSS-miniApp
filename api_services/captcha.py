import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api_services.auth import get_current_user

router = APIRouter(prefix="/api/captcha", tags=["captcha"])

SITE_KEY = os.getenv("DDOS_GUARD_SITE_KEY", "").strip()
PRIVATE_KEY = os.getenv("DDOS_GUARD_SECRET_KEY", "").strip()
VERIFY_URL = os.getenv("DDOS_GUARD_VERIFY_URL", "").strip() or "https://captcha.ddos-guard.net/siteverify"
COOKIE_NAME = "ddg_captcha_session"
SESSION_TTL = max(3600, int(os.getenv("DDOS_GUARD_SESSION_TTL_SECONDS", "2592000")))
SESSION_SECRET = os.getenv("DDOS_GUARD_SESSION_SECRET", "").strip()


class CaptchaVerifyRequest(BaseModel):
    token: str = Field(min_length=1, max_length=16384)


def _sign(value: str) -> str:
    secret = SESSION_SECRET or os.getenv("BOT_TOKEN", "")
    if not secret:
        raise RuntimeError("Captcha session signing secret is not configured")
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(user_id: int, expires_at: int) -> str:
    payload = f"{user_id}:{expires_at}"
    return f"{payload}:{_sign(payload)}"


def verify_session(value: str | None, user_id: int) -> bool:
    if not value:
        return False
    try:
        session_user, expires, signature = value.split(":", 2)
        payload = f"{session_user}:{expires}"
        if int(session_user) != user_id or int(expires) < int(time.time()):
            return False
        return hmac.compare_digest(_sign(payload), signature)
    except (ValueError, RuntimeError):
        return False


def _siteverify_request(token: str) -> dict[str, Any]:
    body = json.dumps({"response": token, "private_key": PRIVATE_KEY}).encode("utf-8")
    req = urllib.request.Request(
        VERIFY_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError("CAPTCHA provider returned a non-200 response")
        data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("CAPTCHA provider returned an invalid response")
        return data


@router.get("/config")
async def captcha_config():
    # Site key is public; the private key is never returned.
    return {"enabled": bool(SITE_KEY and PRIVATE_KEY), "site_key": SITE_KEY if PRIVATE_KEY else ""}


@router.get("/status")
async def captcha_status(request: Request, user=Depends(get_current_user)):
    return {"verified": verify_session(request.cookies.get(COOKIE_NAME), user["id"])}


@router.post("/verify")
async def verify_captcha(
    req: CaptchaVerifyRequest,
    request: Request,
    response: Response,
    user=Depends(get_current_user),
):
    if not SITE_KEY or not PRIVATE_KEY:
        raise HTTPException(status_code=503, detail="CAPTCHA не настроена на сервере")
    if not req.token.strip():
        raise HTTPException(status_code=400, detail="Пройди CAPTCHA")

    try:
        result = await asyncio.to_thread(_siteverify_request, req.token.strip())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
        raise HTTPException(status_code=503, detail="Не удалось проверить CAPTCHA. Попробуй ещё раз.")

    if result.get("success") is not True:
        raise HTTPException(status_code=400, detail="CAPTCHA не пройдена. Попробуй ещё раз.")

    expires = int(time.time()) + SESSION_TTL
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session(user["id"], expires),
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )
    return {"ok": True, "expires_at": expires}
