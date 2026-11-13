import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

import bot
from config import (
    ADMIN_ID, BOT_TOKEN, CATEGORIES, DELIVERY_PRICE, MAX_PROMOS_PER_ORDER,
    REFERRAL_BONUS_PERCENT, REFERRAL_MILESTONE, REFERRAL_MILESTONE_DISCOUNT,
)
from database.db import SessionLocal, engine, init_db
from database.models import CartClubRedemption, CartItem, CartPromo, Category, ClubAccount, ClubAchievementUnlock, ClubTaskConfig, ClubTaskProgress, ClubTitleSelection, ClubTransaction, Favorite, Order, OrderItem, OrderStatusHistory, Product, Promocode, Review, User, ClubSeason, ClubSeasonPoint, ClubSeasonReward, ClubSetting, ClubCosmetic, ClubUserCosmetic, ClubCosmeticLoadout, ClubMonthlyCoupon
from image_utils import optimize_product_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("dss.api")
STATIC_DIR = Path(__file__).resolve().parent / "static"
SITE_KEY = os.getenv("DDOS_GUARD_SITE_KEY", "").strip()
PRIVATE_KEY = os.getenv("DDOS_GUARD_SECRET_KEY", "").strip()
VERIFY_URL = os.getenv("DDOS_GUARD_VERIFY_URL", "https://captcha.ddos-guard.net/siteverify").strip()
SESSION_SECRET = os.getenv("DDOS_GUARD_SESSION_SECRET", "").strip()
SESSION_TTL = max(3600, int(os.getenv("DDOS_GUARD_SESSION_TTL_SECONDS", "2592000")))
CLUB_DCP_PER_RUBLE = 100
CLUB_MIN_ORDER = 500
CLUB_REDEMPTION_TIERS = (
    (500, 100),
    (1000, 150),
    (1500, 250),
    (3000, 300),
    (5000, 400),
    (10000, 500),
)
CLUB_MIN_PRODUCT_PRICE = 500
COOKIE_NAME = "ddg_captcha_session"
INIT_DATA_MAX_AGE = max(60, int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "86400")))
RATE_WINDOW = max(5, int(os.getenv("SECURITY_RATE_WINDOW_SECONDS", "30")))
RATE_MAX = max(10, int(os.getenv("SECURITY_RATE_MAX_REQUESTS", "45")))
SUSPICIOUS_ADDS = max(5, int(os.getenv("DDOS_GUARD_SUSPICIOUS_CART_ADDS", "8")))
SUSPICIOUS_WINDOW = max(5, int(os.getenv("DDOS_GUARD_SUSPICIOUS_WINDOW_SECONDS", "20")))
_requests: dict[str, deque] = defaultdict(deque)
_cart_adds: dict[str, deque] = defaultdict(deque)
PRODUCT_UPLOAD_DIR = Path("data/uploads/products").resolve()
PRODUCT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
REVIEW_UPLOAD_DIR = Path("data/uploads/reviews").resolve()
REVIEW_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_PRODUCT_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_PRODUCT_IMAGE_SIZE = 5 * 1024 * 1024
CAPTCHA_EXEMPT = {"/api/health", "/api/auth", "/api/captcha/config", "/api/captcha/status", "/api/captcha/verify"}


def verify_init_data(raw: str) -> dict | None:
    if not raw or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
        got = pairs.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, got):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 30 or now - auth_date > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "null"))
        return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _sign_session(payload: str) -> str:
    key = SESSION_SECRET or BOT_TOKEN
    if not key:
        raise RuntimeError("CAPTCHA session secret is not configured")
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _captcha_session(user_id: int, expires_at: int) -> str:
    payload = f"{user_id}:{expires_at}"
    return f"{payload}:{_sign_session(payload)}"


def _captcha_valid(value: str | None, user_id: int) -> bool:
    if not value:
        return False
    try:
        uid, expiry, signature = value.split(":", 2)
        payload = f"{uid}:{expiry}"
        return int(uid) == user_id and int(expiry) >= int(time.time()) and hmac.compare_digest(_sign_session(payload), signature)
    except (ValueError, RuntimeError):
        return False


def _verify_captcha(token: str) -> dict:
    body = json.dumps({"response": token, "private_key": PRIVATE_KEY}).encode()
    request = urllib.request.Request(
        VERIFY_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        result = json.loads(response.read().decode())
    if not isinstance(result, dict):
        raise RuntimeError("Invalid CAPTCHA response")
    return result


async def current_user(x_init_data: str = Header(default="", alias="X-Init-Data")) -> dict:
    payload = verify_init_data(x_init_data)
    if not payload:
        raise HTTPException(401, "Данные Telegram недействительны. Открой Mini App через бота.")
    async with SessionLocal() as session:
        record = await session.get(User, payload["id"])
        if record and record.is_banned and payload["id"] != ADMIN_ID:
            raise HTTPException(403, "Аккаунт заблокирован администратором.")
    payload["is_admin"] = payload["id"] == ADMIN_ID
    return payload


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if not ADMIN_ID or user["id"] != ADMIN_ID:
        raise HTTPException(403, "Доступ только для администратора.")
    return user


class ProductInput(BaseModel):
    category_id: int = Field(gt=0)
    brand: str = Field(min_length=1, max_length=64)
    flavor: str = Field(min_length=1, max_length=64)
    price: float = Field(gt=0, le=10000000)
    stock_qty: int = Field(default=1, ge=0, le=1000000)
    cost_price: float | None = Field(default=None, ge=0, le=10000000)


class ProductPatch(BaseModel):
    category_id: int | None = Field(default=None, gt=0)
    brand: str | None = Field(default=None, min_length=1, max_length=64)
    flavor: str | None = Field(default=None, min_length=1, max_length=64)
    price: float | None = Field(default=None, gt=0, le=10000000)
    in_stock: bool | None = None
    stock_qty: int | None = Field(default=None, ge=0, le=1000000)
    cost_price: float | None = Field(default=None, ge=0, le=10000000)


class ProductId(BaseModel):
    product_id: int = Field(gt=0)


class CartQuantityInput(ProductId):
    quantity: int = Field(ge=1, le=1000000)


class PromoInput(BaseModel):
    code: str | None = Field(default=None, max_length=16)
    kind: str
    amount: float = Field(gt=0, le=10000000)
    owner_id: int = Field(default=0, ge=0)
    max_uses: int = Field(default=1, ge=1, le=1000000)
    target_type: str = "all"
    target_id: int | None = Field(default=None, ge=1)
    min_order: float = Field(default=0, ge=0, le=10000000)
    starts_at: str | None = None
    expires_at: str | None = None


class FavoriteInput(ProductId):
    pass


class OrderStatusInput(BaseModel):
    status: str = Field(min_length=1, max_length=24)


class CheckoutInput(BaseModel):
    delivery_type: str
    delivery_name: str | None = Field(default=None, max_length=128)
    delivery_phone: str | None = Field(default=None, max_length=32)
    delivery_address: str | None = Field(default=None, max_length=512)


class ClubRedeemInput(BaseModel):
    points: int = Field(ge=0, le=100000000)


class ReviewInput(BaseModel):
    order_id: int = Field(gt=0)
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=3, max_length=2048)


class BroadcastInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


async def _migrate_legacy_columns() -> None:
    columns = [
        ("orders", "delivery_type", "TEXT DEFAULT 'pickup'"),
        ("orders", "delivery_name", "TEXT"),
        ("orders", "delivery_phone", "TEXT"),
        ("orders", "delivery_address", "TEXT"),
        ("orders", "dcp_spent", "INTEGER DEFAULT 0"),
        ("orders", "club_discount", "REAL DEFAULT 0"),
        ("users", "is_banned", "INTEGER DEFAULT 0"),
        ("users", "is_unreachable", "INTEGER DEFAULT 0"),
        ("club_accounts", "orders_adjustment", "INTEGER DEFAULT 0"),
        ("club_cosmetics", "card_primary", "TEXT DEFAULT '#26133b'"),
        ("club_cosmetics", "card_secondary", "TEXT DEFAULT '#0b0812'"),
        ("club_cosmetics", "card_accent", "TEXT DEFAULT '#c49aff'"),
        ("club_cosmetics", "card_animation", "TEXT DEFAULT 'pulse'"),
        ("club_cosmetics", "requirement_type", "TEXT DEFAULT 'none'"),
        ("club_cosmetics", "requirement_value", "INTEGER DEFAULT 0"),
        ("club_cosmetics", "card_font", "TEXT DEFAULT 'orbitron'"),
        ("club_cosmetics", "max_supply", "INTEGER DEFAULT 0"),
        ("club_cosmetics", "issued_count", "INTEGER DEFAULT 0"),
    ]
    async with engine.begin() as connection:
        for table, column, sql_type in columns:
            try:
                await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            except Exception:
                # SQLite raises when this column already exists; that is expected.
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _migrate_legacy_columns()
    async with SessionLocal() as session:
        if not (await session.execute(select(Category.id).limit(1))).first():
            session.add_all([Category(name=name) for name in CATEGORIES])
            await session.commit()
    log.info("Frontend path: %s; index.html exists: %s", STATIC_DIR, (STATIC_DIR / "index.html").is_file())
    log.info("DDoS-Guard CAPTCHA enabled: %s", bool(SITE_KEY and PRIVATE_KEY))
    task = asyncio.create_task(bot.start_bot()) if BOT_TOKEN else None
    log.info("DSS Mini App API startup complete")
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await engine.dispose()


app = FastAPI(title="Dead Smoke Store", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def api_security(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    if SITE_KEY and PRIVATE_KEY and path not in CAPTCHA_EXEMPT:
        payload = verify_init_data(request.headers.get("X-Init-Data", ""))
        if not payload or not _captcha_valid(request.cookies.get(COOKIE_NAME), payload["id"]):
            response = JSONResponse(status_code=428, content={"detail": "CAPTCHA_REQUIRED"})
            response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="none")
            return response
    if SITE_KEY and PRIVATE_KEY and request.method == "POST" and path == "/api/cart/add":
        bucket = _cart_adds[ip]
        while bucket and now - bucket[0] > SUSPICIOUS_WINDOW:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) > SUSPICIOUS_ADDS:
            bucket.clear()
            response = JSONResponse(status_code=428, content={"detail": "CAPTCHA_REQUIRED"})
            response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="none")
            return response
    bucket = _requests[ip]
    while bucket and now - bucket[0] > RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_MAX:
        retry = max(1, int(RATE_WINDOW - (now - bucket[0])))
        return JSONResponse(status_code=429, content={"detail": "Слишком много запросов. Попробуй чуть позже."}, headers={"Retry-After": str(retry)})
    bucket.append(now)
    if len(_requests) > 5000:
        for old_ip in list(_requests):
            old = _requests[old_ip]
            while old and now - old[0] > RATE_WINDOW:
                old.popleft()
            if not old:
                _requests.pop(old_ip, None)
    return await call_next(request)


@app.get("/api/health")
async def health():
    return {"ok": True, "app": "dss-miniapp"}


@app.get("/health")
async def health_alias():
    return {"ok": True, "app": "dss-miniapp"}


@app.post("/api/auth")
async def authenticate(x_init_data: str = Header(default="", alias="X-Init-Data")):
    payload = verify_init_data(x_init_data)
    if not payload:
        raise HTTPException(401, "Данные Telegram недействительны. Открой Mini App через бота.")
    uid = payload["id"]
    username = payload.get("username") or payload.get("first_name") or "Пользователь"
    async with SessionLocal() as session:
        record = await session.get(User, uid)
        if record is None:
            record = User(id=uid, username=username)
            session.add(record)
        else:
            record.username = username
            record.is_unreachable = False
        await session.commit()
        return {"user_id": uid, "username": record.username, "is_admin": uid == ADMIN_ID, "is_banned": bool(record.is_banned)}


@app.get("/api/captcha/config")
async def captcha_config():
    return {"enabled": bool(SITE_KEY and PRIVATE_KEY), "site_key": SITE_KEY if SITE_KEY and PRIVATE_KEY else ""}


@app.get("/api/captcha/status")
async def captcha_status(request: Request, user: dict = Depends(current_user)):
    return {"verified": _captcha_valid(request.cookies.get(COOKIE_NAME), user["id"])}


@app.post("/api/captcha/verify")
async def captcha_verify(data: dict, response: Response, user: dict = Depends(current_user)):
    token = str(data.get("token", "")).strip()
    if not SITE_KEY or not PRIVATE_KEY:
        raise HTTPException(503, "CAPTCHA не настроена на сервере.")
    if not token or len(token) > 16384:
        raise HTTPException(400, "Токен CAPTCHA не получен.")
    try:
        valid = await asyncio.to_thread(_verify_captcha, token)
    except Exception:
        log.exception("DDoS-Guard CAPTCHA verification failed")
        raise HTTPException(503, "Не удалось проверить CAPTCHA. Попробуй ещё раз.")
    if valid.get("success") is not True:
        raise HTTPException(400, "CAPTCHA не пройдена. Попробуй ещё раз.")
    expires = int(time.time()) + SESSION_TTL
    response.set_cookie(COOKIE_NAME, _captcha_session(user["id"], expires), max_age=SESSION_TTL, httponly=True, secure=True, samesite="none", path="/")
    return {"ok": True, "expires_at": expires}


async def _promo_codes(session, uid: int) -> list[str]:
    return list((await session.execute(select(CartPromo.code).where(CartPromo.user_id == uid).order_by(CartPromo.id))).scalars().all())


def _promo_matches_item(promo: Promocode, item: dict) -> bool:
    if promo.created_by == "club" and "набор" in str(item.get("category_name", "")).casefold():
        return False
    if promo.target_type == "product":
        return int(item.get("product_id", 0)) == int(promo.target_id or 0)
    if promo.target_type == "category":
        return int(item.get("category_id", 0)) == int(promo.target_id or 0)
    return True


def _promo_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=3)))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        raise HTTPException(400, "Проверь дату и время действия промокода.")


def _promo_is_active(promo: Promocode, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return not ((promo.starts_at and now < promo.starts_at) or (promo.expires_at and now > promo.expires_at))


def _promo_datetime_to_local(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%dT%H:%M")


def _validate_promo_for_items(promo: Promocode, items: list[dict]) -> None:
    if not _promo_is_active(promo):
        raise HTTPException(409, "Срок действия промокода ещё не начался или уже закончился.")
    subtotal = sum(float(i["price"]) * int(i.get("quantity", 1)) for i in items)
    if subtotal + 0.001 < float(promo.min_order or 0):
        raise HTTPException(400, f"Для промокода минимальная сумма заказа — {float(promo.min_order or 0):.0f} ₽.")
    eligible = sum(float(i["price"]) * int(i.get("quantity", 1)) for i in items if _promo_matches_item(promo, i))
    if eligible <= 0:
        raise HTTPException(400, "Этот промокод не подходит к товарам в корзине.")


def _totals(items: list[dict], promos: list[Promocode]) -> dict:
    subtotal = round(sum(float(i["price"]) * int(i.get("quantity", 1)) for i in items), 2)
    remaining = [float(i["price"]) * int(i.get("quantity", 1)) for i in items]
    percent_amount = 0.0
    fixed_amount = 0.0
    for promo in promos:
        if promo.used or int(promo.used_count or 0) >= int(promo.max_uses or 1) or not _promo_is_active(promo) or subtotal + 0.001 < float(promo.min_order or 0):
            continue
        eligible_indexes = [idx for idx, item in enumerate(items) if _promo_matches_item(promo, item) and remaining[idx] > 0]
        eligible_total = sum(remaining[idx] for idx in eligible_indexes)
        if eligible_total <= 0:
            continue
        if promo.kind == "percent":
            discount = eligible_total * min(float(promo.amount), 100.0) / 100.0
            percent_amount += discount
        else:
            discount = min(float(promo.amount), eligible_total)
            fixed_amount += discount
        if discount > 0:
            factor = max(0.0, (eligible_total - discount) / eligible_total)
            for idx in eligible_indexes:
                remaining[idx] *= factor
    percent_amount = round(percent_amount, 2)
    fixed_amount = round(fixed_amount, 2)
    total_discount = min(subtotal, percent_amount + fixed_amount)
    return {
        "subtotal": subtotal,
        "percent_discount": round(sum(float(p.amount) for p in promos if p.kind == "percent"), 2),
        "percent_discount_amount": percent_amount,
        "fixed_discount": fixed_amount,
        "total": round(max(subtotal - total_discount, 0), 2),
    }


def _club_discount_for_cart(items: list[dict], subtotal: float, config: dict | None = None) -> dict:
    """Return the fixed DCP discount tier using the active DSS CLUB economy rules."""
    economy = (config or CLUB_DEFAULT_CONFIG).get("economy", {})
    min_order = int(economy.get("min_order", CLUB_MIN_ORDER))
    min_product_price = int(economy.get("min_product_price", CLUB_MIN_PRODUCT_PRICE))
    dcp_per_ruble = int(economy.get("dcp_per_ruble", CLUB_DCP_PER_RUBLE))
    discount = 0
    for minimum, amount in economy.get("redemption_tiers", CLUB_REDEMPTION_TIERS):
        if subtotal + 0.001 >= minimum:
            discount = amount
        else:
            break

    eligible_subtotal = 0.0
    eligible_gross_profit = 0.0
    for item in items:
        price = float(item.get("price") or 0)
        quantity = int(item.get("quantity") or 1)
        if price + 0.001 < min_product_price:
            continue
        eligible_subtotal += price * quantity
        cost_price = item.get("cost_price")
        if cost_price is not None:
            eligible_gross_profit += (price - float(cost_price)) * quantity

    eligible_subtotal = round(eligible_subtotal, 2)
    eligible_gross_profit = round(eligible_gross_profit, 2)
    available = bool(
        discount > 0
        and eligible_subtotal + 0.001 >= discount
    )
    if subtotal + 0.001 < min_order:
        reason = f"Минимальная сумма корзины для обмена — {min_order} ₽."
    elif discount <= 0:
        reason = "Для этой суммы корзины скидка DCP недоступна."
    elif eligible_subtotal <= 0:
        reason = "Нет товаров стоимостью от 500 ₽."
    elif eligible_subtotal + 0.001 < discount:
        reason = "Стоимость подходящих товаров недостаточна для этой фиксированной скидки."
    else:
        reason = ""
    return {
        "discount": discount,
        "required_points": discount * dcp_per_ruble,
        "eligible_subtotal": eligible_subtotal,
        "eligible_gross_profit": eligible_gross_profit,
        "available": available,
        "reason": reason,
    }


async def _cart(uid: int) -> dict:
    async with SessionLocal() as session:
        club_config = await _club_config(session)
        min_product_price = int(club_config.get("economy", {}).get("min_product_price", CLUB_MIN_PRODUCT_PRICE))
        rows = (await session.execute(select(CartItem, Product).join(Product, CartItem.product_id == Product.id).where(CartItem.user_id == uid).order_by(CartItem.id))).all()
        category_names = {c.id: c.name for c in (await session.execute(select(Category))).scalars().all()}
        items = [{
            "product_id": p.id, "category_id": p.category_id, "category_name": category_names.get(p.category_id, ""),
            "brand": p.brand, "flavor": p.flavor, "price": float(p.price),
            "quantity": int(item.quantity or 1), "stock_qty": int(p.stock_qty or 0),
            "club_eligible": float(p.price) + 0.001 >= min_product_price,
        } for item, p in rows]
        codes = await _promo_codes(session, uid)
        promos = list((await session.execute(select(Promocode).where(Promocode.code.in_(codes)))).scalars().all()) if codes else []
        account = await session.get(ClubAccount, uid)
        balance = int(account.balance or 0) if account else 0
        redemption = await session.get(CartClubRedemption, uid)
        subtotal = round(sum(float(i["price"]) * int(i.get("quantity", 1)) for i in items), 2)
        stored_points = int(redemption.points or 0) if redemption else 0
        club_offer = _club_discount_for_cart([
            {"price": float(p.price), "cost_price": float(p.cost_price) if p.cost_price is not None else None, "quantity": int(item.quantity or 1)}
            for item, p in rows
        ], subtotal, club_config)
        required_points = int(club_offer["required_points"])
        can_redeem = bool(club_offer["available"] and balance >= required_points and not codes)
        applied_points = required_points if can_redeem and stored_points == required_points else 0
        if redemption and int(redemption.points or 0) != applied_points:
            redemption.points = applied_points
            await session.commit()
        club_discount = float(club_offer["discount"]) if applied_points else 0.0
        totals = _totals(items, promos)
        if applied_points:
            totals["total"] = round(max(0, subtotal - club_discount), 2)
            totals["percent_discount_amount"] = 0
            totals["fixed_discount"] = 0
        if not items and redemption:
            await session.delete(redemption)
            await session.commit()
        unavailable_reason = (
            "Сначала убери обычные промокоды: скидка DCP с ними не суммируется."
            if codes else
            "Недостаточно DCP: нужно " + str(required_points) + " DCP."
            if club_offer["available"] and balance < required_points else
            club_offer["reason"]
        )
        return {
            "items": items, **totals,
            "promos": [{"code": p.code, "kind": p.kind, "amount": float(p.amount), "target_type": p.target_type, "target_id": p.target_id, "min_order": float(p.min_order or 0), "starts_at": _promo_datetime_to_local(p.starts_at), "expires_at": _promo_datetime_to_local(p.expires_at), "applicable": not p.used and int(p.used_count or 0) < int(p.max_uses or 1) and _promo_is_active(p) and subtotal + 0.001 >= float(p.min_order or 0) and any(_promo_matches_item(p, i) for i in items)} for p in promos],
            "club_balance": balance, "club_points": applied_points, "club_discount": club_discount,
            "club_max_points": required_points if can_redeem else 0,
            "club_required_points": required_points,
            "club_discount_offer": float(club_offer["discount"]),
            "club_eligible_subtotal": float(club_offer["eligible_subtotal"]),
            "club_can_redeem": can_redeem,
            "club_unavailable_reason": unavailable_reason,
            "club_min_order": int(club_config.get("economy", {}).get("min_order", CLUB_MIN_ORDER)), "club_min_product_price": int(club_config.get("economy", {}).get("min_product_price", CLUB_MIN_PRODUCT_PRICE)), "club_dcp_per_ruble": int(club_config.get("economy", {}).get("dcp_per_ruble", CLUB_DCP_PER_RUBLE)),
        }


@app.get("/api/categories")
async def categories(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
        return [{"id": c.id, "name": c.name} for c in rows]


@app.get("/api/products")
async def products(category_id: int | None = None, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        query = select(Product).where(Product.in_stock.is_(True), Product.stock_qty > 0)
        if category_id is not None:
            query = query.where(Product.category_id == category_id)
        rows = (await session.execute(query.order_by(Product.category_id, Product.number))).scalars().all()
        return [{"id": p.id, "category_id": p.category_id, "number": p.number, "brand": p.brand, "flavor": p.flavor, "price": float(p.price), "in_stock": bool(p.in_stock), "stock_qty": int(p.stock_qty or 0), "images": p.images} for p in rows]


@app.get("/api/favorites")
async def favorites(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Product).join(Favorite, Favorite.product_id == Product.id)
            .where(Favorite.user_id == user["id"]).order_by(Favorite.created_at.desc())
        )).scalars().all()
        return [{"id": p.id, "category_id": p.category_id, "number": p.number, "brand": p.brand,
                 "flavor": p.flavor, "price": float(p.price), "in_stock": bool(p.in_stock),
                 "stock_qty": int(p.stock_qty or 0), "images": p.images} for p in rows]


@app.post("/api/favorites/toggle")
async def toggle_favorite(data: FavoriteInput, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        if not await session.get(Product, data.product_id):
            raise HTTPException(404, "Товар не найден.")
        existing = (await session.execute(select(Favorite).where(
            Favorite.user_id == user["id"], Favorite.product_id == data.product_id
        ))).scalar_one_or_none()
        if existing:
            await session.delete(existing)
            is_favorite = False
        else:
            session.add(Favorite(user_id=user["id"], product_id=data.product_id))
            is_favorite = True
        await session.commit()
    return {"product_id": data.product_id, "is_favorite": is_favorite}


@app.get("/api/cart")
async def get_cart(user: dict = Depends(current_user)):
    return await _cart(user["id"])


@app.post("/api/cart/add")
async def cart_add(data: ProductId, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        p = await session.get(Product, data.product_id)
        if not p or not p.in_stock or int(p.stock_qty or 0) <= 0:
            raise HTTPException(409, "Товар больше недоступен.")
        existing = (await session.execute(select(CartItem).where(CartItem.user_id == user["id"], CartItem.product_id == p.id))).scalar_one_or_none()
        if existing:
            if int(existing.quantity or 1) >= int(p.stock_qty or 0):
                raise HTTPException(409, f"Максимум в наличии: {p.stock_qty} шт.")
            existing.quantity = int(existing.quantity or 1) + 1
        else:
            session.add(CartItem(user_id=user["id"], product_id=p.id, reserved=False, quantity=1))
        await session.commit()
    return await _cart(user["id"])


@app.post("/api/cart/quantity")
async def cart_set_quantity(data: CartQuantityInput, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        item = (await session.execute(select(CartItem).where(CartItem.user_id == user["id"], CartItem.product_id == data.product_id))).scalar_one_or_none()
        if not item:
            raise HTTPException(404, "Товар не найден в корзине.")
        product = await session.get(Product, data.product_id)
        if not product or not product.in_stock or data.quantity > int(product.stock_qty or 0):
            raise HTTPException(409, f"Доступно не больше {int(product.stock_qty or 0) if product else 0} шт.")
        item.quantity = data.quantity
        await session.commit()
    return await _cart(user["id"])


@app.post("/api/cart/remove")
async def cart_remove(data: ProductId, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        await session.execute(delete(CartItem).where(CartItem.user_id == user["id"], CartItem.product_id == data.product_id))
        await session.commit()
    return await _cart(user["id"])


@app.post("/api/cart/clear")
async def cart_clear(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        await session.execute(delete(CartItem).where(CartItem.user_id == user["id"]))
        await session.execute(delete(CartPromo).where(CartPromo.user_id == user["id"]))
        await session.commit()
    return await _cart(user["id"])


@app.post("/api/club/redeem")
async def club_redeem(data: ClubRedeemInput, user: dict = Depends(current_user)):
    uid = user["id"]
    async with SessionLocal() as session:
        if data.points == 0:
            await session.execute(delete(CartClubRedemption).where(CartClubRedemption.user_id == uid))
            await session.commit()
            return await _cart(uid)
        codes = await _promo_codes(session, uid)
        if codes:
            raise HTTPException(409, "DCP нельзя совмещать с обычными промокодами. Убери промокоды из корзины.")
        rows = (await session.execute(select(CartItem, Product).join(Product, CartItem.product_id == Product.id).where(CartItem.user_id == uid))).all()
        if not rows:
            raise HTTPException(400, "Сначала добавь товары в корзину.")
        subtotal = round(sum(float(p.price) * int(item.quantity or 1) for item, p in rows), 2)
        club_config = await _club_config(session)
        club_offer = _club_discount_for_cart([
            {"price": float(p.price), "cost_price": float(p.cost_price) if p.cost_price is not None else None, "quantity": int(item.quantity or 1)}
            for item, p in rows
        ], subtotal, club_config)
        if not club_offer["available"]:
            raise HTTPException(400, club_offer["reason"] or "Клубная скидка недоступна для этой корзины.")
        required_points = int(club_offer["required_points"])
        if data.points != required_points:
            raise HTTPException(400, f"Для этой корзины доступна фиксированная скидка {int(club_offer['discount'])} ₽ за {required_points} DCP.")
        account = await session.get(ClubAccount, uid)
        balance = int(account.balance or 0) if account else 0
        if balance < required_points:
            raise HTTPException(409, f"Недостаточно DCP. Нужно {required_points} DCP.")
        redemption = await session.get(CartClubRedemption, uid)
        if redemption is None:
            session.add(CartClubRedemption(user_id=uid, points=required_points))
        else:
            redemption.points = required_points
        await session.commit()
    return await _cart(uid)


@app.post("/api/club/redeem/remove")
async def club_redeem_remove(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        await session.execute(delete(CartClubRedemption).where(CartClubRedemption.user_id == user["id"]))
        await session.commit()
    return await _cart(user["id"])


@app.post("/api/promo/apply")
async def apply_promo(data: dict, user: dict = Depends(current_user)):
    code = str(data.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,16}", code):
        raise HTTPException(400, "Проверь написание промокода.")
    async with SessionLocal() as session:
        promo = await session.get(Promocode, code)
        if not promo or promo.used or promo.used_count >= promo.max_uses:
            raise HTTPException(404, "Промокод не найден или исчерпан.")
        cart_rows = (await session.execute(select(CartItem, Product).join(Product, CartItem.product_id == Product.id).where(CartItem.user_id == user["id"]))).all()
        category_names = {c.id: c.name for c in (await session.execute(select(Category))).scalars().all()}
        cart_items = [{"product_id": p.id, "category_id": p.category_id, "category_name": category_names.get(p.category_id, ""),
            "price": float(p.price), "quantity": int(item.quantity or 1)} for item, p in cart_rows]
        if not cart_items:
            raise HTTPException(400, "Сначала добавь товары в корзину.")
        _validate_promo_for_items(promo, cart_items)
        redemption = await session.get(CartClubRedemption, user["id"])
        if redemption and int(redemption.points or 0) > 0:
            raise HTTPException(409, "Сначала отключи списание DCP — клубная скидка не совмещается с промокодами.")
        if promo.owner_id not in (0, user["id"], ADMIN_ID):
            raise HTTPException(403, "Этот промокод выдан другому пользователю.")
        codes = await _promo_codes(session, user["id"])
        if code in codes:
            raise HTTPException(409, "Этот промокод уже применён.")
        applied_promos = list((await session.execute(select(Promocode).where(Promocode.code.in_(codes)))).scalars().all()) if codes else []
        if promo.created_by == "club" and codes:
            raise HTTPException(409, "Клубный промокод нельзя объединять с другими промокодами.")
        if any(p.created_by == "club" for p in applied_promos):
            raise HTTPException(409, "К клубному промокоду нельзя добавлять другие промокоды.")
        if len(codes) >= MAX_PROMOS_PER_ORDER:
            raise HTTPException(409, "Можно применить не больше двух промокодов.")
        session.add(CartPromo(user_id=user["id"], code=code))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "Промокод уже применён.")
    return {"code": promo.code, "kind": promo.kind, "amount": float(promo.amount), "target_type": promo.target_type, "target_id": promo.target_id, "min_order": float(promo.min_order or 0)}


@app.post("/api/promo/remove")
async def remove_applied_promo(data: dict, user: dict = Depends(current_user)):
    code = str(data.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,16}", code):
        raise HTTPException(400, "Проверь код промокода.")
    async with SessionLocal() as session:
        result = await session.execute(delete(CartPromo).where(CartPromo.user_id == user["id"], CartPromo.code == code))
        await session.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "Промокод не найден в корзине.")
    return await _cart(user["id"])


@app.get("/api/promo/applied")
async def get_applied_promos(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        return {"codes": await _promo_codes(session, user["id"])}


@app.post("/api/checkout")
async def checkout(data: CheckoutInput, user: dict = Depends(current_user)):
    if data.delivery_type not in ("pickup", "cdek"):
        raise HTTPException(400, "Неверный способ получения.")
    if data.delivery_type == "cdek" and not (data.delivery_name and data.delivery_phone and data.delivery_address and len(data.delivery_name.strip()) >= 3 and len(data.delivery_address.strip()) >= 5):
        raise HTTPException(400, "Заполни ФИО, телефон и адрес доставки.")
    async with SessionLocal() as session:
        rows = (await session.execute(select(CartItem, Product).join(Product, CartItem.product_id == Product.id).where(CartItem.user_id == user["id"]).order_by(CartItem.id))).all()
        if not rows:
            raise HTTPException(400, "Корзина пуста.")
        for cart_item, p in rows:
            requested = int(cart_item.quantity or 1)
            if not p.in_stock or int(p.stock_qty or 0) < requested:
                raise HTTPException(409, f"Для товара {p.brand} — {p.flavor} доступно {int(p.stock_qty or 0)} шт., в корзине {requested}.")
        category_names = {c.id: c.name for c in (await session.execute(select(Category))).scalars().all()}
        items = [{"product_id": p.id, "category_id": p.category_id, "category_name": category_names.get(p.category_id, ""),
            "brand": p.brand, "flavor": p.flavor, "price": float(p.price), "cost_price": float(p.cost_price) if p.cost_price is not None else None,
            "quantity": int(cart_item.quantity or 1)} for cart_item, p in rows]
        codes = await _promo_codes(session, user["id"])
        promos = list((await session.execute(select(Promocode).where(Promocode.code.in_(codes)))).scalars().all()) if codes else []
        if len(codes) != len(promos) or any(p.used or p.used_count >= p.max_uses for p in promos):
            raise HTTPException(409, "Один из промокодов больше недоступен.")
        if any(p.created_by == "club" for p in promos) and len(promos) != 1:
            raise HTTPException(409, "Клубный промокод нельзя объединять с другими промокодами.")
        for promo in promos:
            if promo.owner_id not in (0, user["id"], ADMIN_ID):
                raise HTTPException(403, "Один из промокодов выдан другому пользователю.")
            _validate_promo_for_items(promo, items)
        totals = _totals(items, promos)
        redemption = await session.get(CartClubRedemption, user["id"])
        dcp_spent = int(redemption.points or 0) if redemption else 0
        club_discount = 0.0
        account = None
        if dcp_spent:
            if codes:
                raise HTTPException(409, "DCP нельзя совмещать с обычными промокодами.")
            club_offer = _club_discount_for_cart(items, totals["subtotal"], await _club_config(session))
            required_points = int(club_offer["required_points"])
            if not club_offer["available"] or dcp_spent != required_points:
                raise HTTPException(409, "Условия фиксированной скидки DSS CLUB изменились. Обнови корзину.")
            account = await session.get(ClubAccount, user["id"])
            if account is None or int(account.balance or 0) < required_points:
                raise HTTPException(409, "Недостаточно DCP на балансе. Обнови корзину.")
            club_discount = float(club_offer["discount"])
            totals["total"] = round(max(0, totals["subtotal"] - club_discount), 2)
            totals["percent_discount_amount"] = 0
            totals["fixed_discount"] = 0
        delivery_price = DELIVERY_PRICE if data.delivery_type == "cdek" else 0
        final = round(totals["total"] + delivery_price, 2)
        order = Order(user_id=user["id"], total=final, status="accepted", promo_codes=",".join(codes) if codes else None,
            dcp_spent=dcp_spent, club_discount=club_discount,
            delivery_type=data.delivery_type, delivery_name=data.delivery_name.strip() if data.delivery_name else None,
            delivery_phone=data.delivery_phone.strip() if data.delivery_phone else None,
            delivery_address=data.delivery_address.strip() if data.delivery_address else None)
        session.add(order)
        await session.flush()
        session.add(OrderStatusHistory(order_id=order.id, status="accepted", changed_by=user["id"], note="Заказ оформлен"))
        for cart_item, p in rows:
            quantity = int(cart_item.quantity or 1)
            session.add(OrderItem(order_id=order.id, product_id=p.id, price=float(p.price), quantity=quantity,
                cost_price_snapshot=float(p.cost_price) if p.cost_price is not None else None,
                brand_snapshot=p.brand, flavor_snapshot=p.flavor))
            p.stock_qty = max(0, int(p.stock_qty or 0) - quantity)
            p.in_stock = p.stock_qty > 0
        await session.execute(delete(CartItem).where(CartItem.user_id == user["id"]))
        await session.execute(delete(CartPromo).where(CartPromo.user_id == user["id"]))
        await session.execute(delete(CartClubRedemption).where(CartClubRedemption.user_id == user["id"]))
        if dcp_spent:
            account.balance = int(account.balance or 0) - dcp_spent
            session.add(ClubTransaction(user_id=user["id"], event_key=f"order:{order.id}:club-spend", kind="spend", amount=-dcp_spent,
                balance_after=account.balance, reason=f"Скидка DSS CLUB по заказу DSS-{order.id:06d}"))
        await session.commit()
        await session.refresh(order)
        record = await session.get(User, user["id"])
        username = record.username if record else user.get("username")
    item_text = "\n".join(f"• {item['brand']} — {item['flavor']} × {item['quantity']} — {int(item['price'])} ₽/шт." for item in items)
    receive = "Самовывоз" if data.delivery_type == "pickup" else f"СДЭК: {data.delivery_name}, {data.delivery_phone}, {data.delivery_address} (+{DELIVERY_PRICE} ₽)"
    notice = f"🆕 Новый заказ DSS-{order.id:06d}\nПользователь: {username or user['id']} (ID {user['id']})\n\n{item_text}\n\nСумма товаров: {int(totals['subtotal'])} ₽\nСкидка: {totals['percent_discount']}% и {int(totals['fixed_discount'])} ₽\nСкидка DSS CLUB: {int(club_discount)} ₽ ({dcp_spent} DCP)\nПолучение: {receive}\nПромокоды: {', '.join(codes) if codes else 'нет'}\nК оплате: {int(final)} ₽\n\nОткрой /admin в боте, чтобы обработать заказ."
    await bot.notify_admin(notice, order.id)
    return {"id": order.id, "order_number": f"DSS-{order.id:06d}", "total": float(order.total), "status": order.status, "delivery_type": order.delivery_type,
        "delivery_name": order.delivery_name, "delivery_phone": order.delivery_phone, "delivery_address": order.delivery_address,
        "created_at": order.created_at.isoformat() if order.created_at else "", "items": items, "delivery_price": delivery_price,
        "dcp_spent": dcp_spent, "club_discount": club_discount}


async def _order_items(session, order_id: int) -> list[dict]:
    rows = (await session.execute(select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id).where(OrderItem.order_id == order_id).order_by(OrderItem.id))).all()
    return [{"product_id": p.id, "brand": item.brand_snapshot or p.brand, "flavor": item.flavor_snapshot or p.flavor, "price": float(item.price), "quantity": int(item.quantity or 1)} for item, p in rows]


@app.get("/api/orders")
async def orders(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Order).where(Order.user_id == user["id"]).order_by(Order.created_at.desc()).limit(50))).scalars().all()
        reviewed = set((await session.execute(select(Review.order_id).where(Review.user_id == user["id"]))).scalars().all())
        result = []
        for o in rows:
            history = (await session.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == o.id).order_by(OrderStatusHistory.created_at, OrderStatusHistory.id))).scalars().all()
            result.append({"id": o.id, "order_number": f"DSS-{o.id:06d}", "total": float(o.total), "status": o.status, "delivery_type": o.delivery_type,
                "delivery_name": o.delivery_name, "delivery_phone": o.delivery_phone, "delivery_address": o.delivery_address,
                "created_at": o.created_at.isoformat() if o.created_at else "", "items": await _order_items(session, o.id),
                "dcp_spent": int(o.dcp_spent or 0), "club_discount": float(o.club_discount or 0),
                "status_history": [{"status": h.status, "changed_at": h.created_at.isoformat() if h.created_at else "", "note": h.note or ""} for h in history],
                "has_review": o.id in reviewed})
        return result




CLUB_LEVELS = [
    {"code": "MEMBER", "title": "MEMBER", "orders": 0, "points": 0, "referrals": 0,
     "perks": ["Базовый клубный профиль", "История DCP и доступ к заданиям"]},
    {"code": "ACTIVE", "title": "ACTIVE", "orders": 3, "points": 300, "referrals": 0,
     "perks": ["Эксклюзивный значок ACTIVE", "Особый цвет уровня в профиле"]},
    {"code": "PREMIUM", "title": "PREMIUM", "orders": 10, "points": 1000, "referrals": 0,
     "perks": ["Уникальный титул PREMIUM", "Эксклюзивная карточка профиля"]},
    {"code": "VIP", "title": "VIP", "orders": 25, "points": 3000, "referrals": 0,
     "perks": ["Рамка VIP", "Закрытые предложения магазина", "Дополнительные клубные задания"]},
    {"code": "LEGEND", "title": "LEGEND", "orders": 75, "points": 10000, "referrals": 0,
     "perks": ["Легендарная рамка", "Уникальный эффект профиля", "Секретные задания", "Ранний доступ к отдельным новинкам"]},
    {"code": "AMBASSADOR", "title": "AMBASSADOR", "orders": 150, "points": 25000, "referrals": 10,
     "perks": ["Уникальная анимация профиля", "Эксклюзивный титул AMBASSADOR", "Особая рамка AMBASSADOR", "Ранние анонсы товаров", "Приватные предложения для амбассадоров"]},
]

ORDER_MILESTONES = [(5, 100), (10, 250), (25, 500), (50, 1000), (100, 2500)]
REFERRAL_MILESTONES = [(5, 300), (10, 700), (25, 1500), (50, 3500), (100, 8000)]

# UTC timestamps corresponding to 00:00 Moscow time on 1 December 2026.
SEASON_FIRST_START = datetime(2026, 11, 30, 21, 0)
SEASON_LENGTH_DAYS = 30
SEASON_DAILY_TASK_SP = 15
SEASON_WEEKLY_TASK_SP = 75
SEASON_PERSONAL_REWARDS = [
    {"points": 100, "key": "season_badge", "title": "Сезонный значок"},
    {"points": 250, "key": "season_title", "title": "Сезонный титул"},
    {"points": 500, "key": "season_card", "title": "Сезонная карточка"},
    {"points": 1000, "key": "season_frame", "title": "Эксклюзивная рамка"},
    {"points": 1500, "key": "season_effect", "title": "Секретный сезонный эффект"},
]
SEASON_TOP_REWARDS = [
    {"from": 1, "to": 1, "key": "top_1", "title": "Уникальная легендарная сезонная рамка"},
    {"from": 2, "to": 3, "key": "top_2_3", "title": "Эксклюзивный сезонный титул"},
    {"from": 4, "to": 10, "key": "top_4_10", "title": "Редкий сезонный значок"},
    {"from": 11, "to": 25, "key": "top_11_25", "title": "Сезонная карточка профиля"},
    {"from": 26, "to": 50, "key": "top_26_50", "title": "Особый значок участника сезона"},

]

async def _award_club_dcp(session, user_id: int, amount: int, event_key: str, reason: str) -> int:
    if amount <= 0:
        return 0
    exists = (await session.execute(select(ClubTransaction.id).where(ClubTransaction.event_key == event_key))).first()
    if exists:
        return 0
    account = await session.get(ClubAccount, user_id)
    if account is None:
        account = ClubAccount(user_id=user_id, balance=0, lifetime_earned=0)
        session.add(account)
        await session.flush()
    account.balance = int(account.balance or 0) + amount
    account.lifetime_earned = int(account.lifetime_earned or 0) + amount
    session.add(ClubTransaction(user_id=user_id, event_key=event_key, kind="earn", amount=amount,
                                balance_after=account.balance, reason=reason))
    return amount



CLUB_ACHIEVEMENTS = [
    # These are cosmetic unlocks; DCP is awarded by repeatable milestone rules below.
    {"key": "first_order", "title": "FIRST STEP", "description": "Заверши свой первый заказ", "icon": "🛍️", "metric": "orders", "target": 1, "reward": 0, "reward_type": "badge"},
    {"key": "five_orders", "title": "REGULAR", "description": "Заверши 5 заказов", "icon": "⭐", "metric": "orders", "target": 5, "reward": 0, "reward_type": "title"},
    {"key": "ten_orders", "title": "LOYAL", "description": "Заверши 10 заказов", "icon": "💜", "metric": "orders", "target": 10, "reward": 0, "reward_type": "badge"},
    {"key": "twenty_five_orders", "title": "VETERAN", "description": "Заверши 25 заказов", "icon": "🏆", "metric": "orders", "target": 25, "reward": 0, "reward_type": "title"},
    {"key": "first_referral", "title": "CONNECTOR", "description": "Пригласи друга, который завершит заказ", "icon": "🤝", "metric": "referrals", "target": 1, "reward": 0, "reward_type": "badge"},
    {"key": "five_referrals", "title": "AMBASSADOR", "description": "Получи 5 завершивших покупку рефералов", "icon": "👑", "metric": "referrals", "target": 5, "reward": 0, "reward_type": "title"},
    {"key": "first_review", "title": "YOUR VOICE", "description": "Опубликуй первый одобренный отзыв", "icon": "✍️", "metric": "reviews", "target": 1, "reward": 0, "reward_type": "badge"},
]
DEFAULT_CLUB_TASKS = [
    {"key": "daily_order", "title": "DAILY CHECK", "description": "Заверши 1 заказ за день", "period": "daily", "event_type": "orders", "target": 1, "reward": 30, "active": True},
    {"key": "weekly_orders", "title": "WEEKLY GRIND", "description": "Заверши 2 заказа за неделю", "period": "weekly", "event_type": "orders", "target": 2, "reward": 100, "active": True},
]


CLUB_DEFAULT_CONFIG = {
    "economy": {
        "order_dcp": 100, "first_order_dcp": 200, "referral_dcp": 150,
        "order_sp": 10, "referral_sp": 25, "daily_task_sp": 15, "weekly_task_sp": 75,
        "dcp_per_ruble": 100, "min_order": 500, "min_product_price": 500,
        "redemption_tiers": [[500, 100], [1000, 150], [1500, 250], [3000, 300], [5000, 400], [10000, 500]],
        "order_milestones": [[5, 100], [10, 250], [25, 500], [50, 1000], [100, 2500]],
        "referral_milestones": [[5, 300], [10, 700], [25, 1500], [50, 3500], [100, 8000]]
    },
    "levels": CLUB_LEVELS,
    "achievements": CLUB_ACHIEVEMENTS,
    "season": {
        "title": "Season 01 · Новый год", "title_prefix": "Season",
        "first_start": "2026-11-30T21:00:00", "length_days": 30,
        "personal_rewards": SEASON_PERSONAL_REWARDS, "top_rewards": SEASON_TOP_REWARDS
    }
}


async def _club_config(session) -> dict:
    row = await session.get(ClubSetting, "main")
    if row is None:
        value = json.loads(json.dumps(CLUB_DEFAULT_CONFIG, ensure_ascii=False))
        session.add(ClubSetting(key="main", value=json.dumps(value, ensure_ascii=False)))
        await session.flush()
        return value
    try:
        stored = json.loads(row.value or "{}")
    except (TypeError, ValueError):
        stored = {}
    result = json.loads(json.dumps(CLUB_DEFAULT_CONFIG, ensure_ascii=False))
    if isinstance(stored, dict):
        for key in result:
            if key in stored and isinstance(stored[key], dict):
                result[key].update(stored[key])
            elif key in stored:
                result[key] = stored[key]
    return result


async def _save_club_config(session, value: dict) -> None:
    row = await session.get(ClubSetting, "main")
    payload = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(ClubSetting(key="main", value=payload))
    else:
        row.value = payload

async def _ensure_club_tasks(session):
    for item in DEFAULT_CLUB_TASKS:
        if await session.get(ClubTaskConfig, item["key"]) is None:
            session.add(ClubTaskConfig(**item))
    await session.flush()


CLUB_MONTHLY_DISCOUNTS = {
    "MEMBER": (50, 300), "ACTIVE": (100, 400), "PREMIUM": (150, 450),
    "VIP": (250, 500), "LEGEND": (300, 800), "AMBASSADOR": (400, 1500),
}


async def _club_current_level_code(session, user_id: int) -> str:
    config = await _club_config(session)
    metrics = await _club_metrics(session, user_id)
    account = await session.get(ClubAccount, user_id)
    lifetime = int(account.lifetime_earned or 0) if account else 0
    current = "MEMBER"
    for level in config.get("levels", CLUB_LEVELS):
        if (metrics["orders"] >= int(level.get("orders", 0))
                and lifetime >= int(level.get("points", 0))
                and metrics["referrals"] >= int(level.get("referrals", 0))):
            current = str(level.get("code", "MEMBER")).upper()
    return current


async def _ensure_monthly_club_coupon(session, user_id: int, level_code: str | None = None) -> dict:
    now_utc = datetime.utcnow()
    moscow_now = now_utc + timedelta(hours=3)
    month_key = f"{moscow_now.year:04d}-{moscow_now.month:02d}"
    if level_code is None:
        level_code = await _club_current_level_code(session, user_id)
    discount, min_order = CLUB_MONTHLY_DISCOUNTS.get(level_code, CLUB_MONTHLY_DISCOUNTS["MEMBER"])
    existing = (await session.execute(select(ClubMonthlyCoupon).where(
        ClubMonthlyCoupon.user_id == user_id, ClubMonthlyCoupon.month_key == month_key
    ))).scalar_one_or_none()
    if existing:
        promo = await session.get(Promocode, existing.code)
        if promo and not promo.used and int(promo.used_count or 0) < int(promo.max_uses or 1) and _promo_is_active(promo, now_utc):
            return {"code": promo.code, "discount": int(existing.discount), "min_order": int(existing.min_order),
                    "expires_at": _promo_datetime_to_local(promo.expires_at), "level": existing.level_code, "new": False}
    old_codes = list((await session.execute(select(Promocode.code).where(
        Promocode.owner_id == user_id, Promocode.created_by == "club"
    ))).scalars().all())
    if old_codes:
        await session.execute(delete(CartPromo).where(CartPromo.user_id == user_id, CartPromo.code.in_(old_codes)))
        await session.execute(delete(Promocode).where(Promocode.code.in_(old_codes)))
    local_start = moscow_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if moscow_now.month == 12:
        local_next = local_start.replace(year=moscow_now.year + 1, month=1)
    else:
        local_next = local_start.replace(month=moscow_now.month + 1)
    starts_at = local_start - timedelta(hours=3)
    expires_at = local_next - timedelta(hours=3, seconds=1)
    code = "DSS" + secrets.token_hex(6).upper()
    while await session.get(Promocode, code):
        code = "DSS" + secrets.token_hex(6).upper()
    promo = Promocode(code=code, owner_id=user_id, kind="fixed", amount=discount, used=False,
        created_by="club", max_uses=1, used_count=0, target_type="all", min_order=min_order,
        starts_at=starts_at, expires_at=expires_at)
    session.add(promo)
    session.add(ClubMonthlyCoupon(user_id=user_id, month_key=month_key, code=code,
        level_code=level_code, discount=discount, min_order=min_order))
    await session.commit()
    try:
        await bot.send_message_safe(user_id,
            f"💜 Твоя ежемесячная привилегия DSS CLUB\n\n"
            f"Промокод: {code}\nСкидка: {discount} ₽\n"
            f"Минимальная корзина: {min_order} ₽\n"
            f"Действует до конца месяца. Один заказ, без объединения с другими промокодами. "
            f"Категория «Наборы» не участвует.")
    except Exception:
        log.exception("Не удалось отправить клубный промокод user_id=%s", user_id)
    return {"code": code, "discount": discount, "min_order": min_order,
            "expires_at": _promo_datetime_to_local(expires_at), "level": level_code, "new": True}


async def _club_metrics(session, user_id: int) -> dict:
    actual_orders = int((await session.execute(select(func.count(Order.id)).where(Order.user_id == user_id, Order.status == "completed"))).scalar() or 0)
    account = await session.get(ClubAccount, user_id)
    orders = max(0, actual_orders + int(account.orders_adjustment or 0)) if account else actual_orders
    referrals = int((await session.execute(select(func.count(User.id)).where(User.referrer_id == user_id, User.first_purchase_done.is_(True)))).scalar() or 0)
    reviews = int((await session.execute(select(func.count(Review.id)).where(Review.user_id == user_id, Review.status == "published"))).scalar() or 0)
    return {"orders": orders, "referrals": referrals, "reviews": reviews}


async def _sync_club_achievements(session, user_id: int) -> list[str]:
    metrics = await _club_metrics(session, user_id)
    config = await _club_config(session)
    messages = []
    for item in config.get("achievements", CLUB_ACHIEVEMENTS):
        if not item.get("active", True):
            continue
        if metrics[item["metric"]] < item["target"]:
            continue
        exists = (await session.execute(select(ClubAchievementUnlock.id).where(
            ClubAchievementUnlock.user_id == user_id, ClubAchievementUnlock.achievement_key == item["key"]
        ))).first()
        if exists:
            continue
        session.add(ClubAchievementUnlock(user_id=user_id, achievement_key=item["key"]))
        await session.flush()
        amount = await _award_club_dcp(session, user_id, item["reward"], f"achievement:{item['key']}:{user_id}", f"Достижение: {item['title']}")
        reward_text = f" · +{amount} DCP" if amount else ""
        messages.append(f"🏆 Достижение открыто: {item['title']}{reward_text}")
    return messages


def _club_period(task, now):
    # Database timestamps are stored as naive UTC. Club periods, however, are
    # defined by Moscow local time (UTC+3), so convert the local boundary back
    # to naive UTC before querying OrderStatusHistory.created_at.
    moscow_now = now + timedelta(hours=3)
    if task.period == "daily":
        local_start = moscow_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_start - timedelta(hours=3)
        return start_utc, local_start.strftime("%Y-%m-%d")
    local_start = (moscow_now - timedelta(days=moscow_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = local_start - timedelta(hours=3)
    return start_utc, local_start.strftime("%G-W%V")


async def _club_task_count(session, user_id: int, task, start) -> int:
    if task.event_type == "reviews":
        value = (await session.execute(
            select(func.count(Review.id)).where(
                Review.user_id == user_id, Review.status == "published", Review.published_at >= start
            )
        )).scalar() or 0
    else:
        value = (await session.execute(
            select(func.count(func.distinct(OrderStatusHistory.order_id)))
            .join(Order, Order.id == OrderStatusHistory.order_id)
            .where(Order.user_id == user_id, OrderStatusHistory.status == "completed", OrderStatusHistory.created_at >= start)
        )).scalar() or 0
    return int(value)


async def _sync_club_tasks(session, user_id: int) -> list[str]:
    await _ensure_club_tasks(session)
    now = datetime.utcnow()
    messages = []
    tasks = (await session.execute(select(ClubTaskConfig).where(ClubTaskConfig.active.is_(True)))).scalars().all()
    for task in tasks:
        start, period_key = _club_period(task, now)
        count = await _club_task_count(session, user_id, task, start)
        progress = (await session.execute(select(ClubTaskProgress).where(
            ClubTaskProgress.user_id == user_id, ClubTaskProgress.task_key == task.key, ClubTaskProgress.period_key == period_key
        ))).scalar_one_or_none()
        if progress is None:
            progress = ClubTaskProgress(user_id=user_id, task_key=task.key, period_key=period_key, progress=0, claimed=False)
            session.add(progress)
            await session.flush()
        progress.progress = min(count, int(task.target))
        if progress.progress >= int(task.target) and not progress.claimed:
            progress.claimed = True
            amount = await _award_club_dcp(session, user_id, int(task.reward),
                f"task:{task.key}:{period_key}:{user_id}", f"Задание: {task.title}")
            economy = (await _club_config(session)).get("economy", {})
            season_points = int(economy.get("daily_task_sp", SEASON_DAILY_TASK_SP) if task.period == "daily"
                                else economy.get("weekly_task_sp", SEASON_WEEKLY_TASK_SP))
            sp_earned = await _award_season_points(session, user_id, season_points,
                f"season:task:{task.key}:{period_key}:{user_id}", f"Сезонное задание: {task.title}")
            messages.append(f"🎯 Задание выполнено: {task.title} · +{amount} DCP" + (f" · +{sp_earned} SP" if sp_earned else ""))
    return messages


async def _sync_repeatable_milestones(session, user_id: int) -> list[str]:
    metrics = await _club_metrics(session, user_id)
    config = await _club_config(session)
    economy = config.get("economy", {})
    messages = []
    for milestone_type, milestones, metric_name in (
        ("orders", economy.get("order_milestones", ORDER_MILESTONES), "orders"),
        ("referrals", economy.get("referral_milestones", REFERRAL_MILESTONES), "referrals"),
    ):
        total = int(metrics[metric_name])
        for threshold, reward in milestones:
            cycles = total // threshold
            for cycle in range(1, cycles + 1):
                unit = "заказов" if milestone_type == "orders" else "купивших рефералов"
                reason = f"Рубеж: каждые {threshold} {unit} · цикл {cycle}"
                event_key = f"milestone:{milestone_type}:{threshold}:{cycle}:{user_id}"
                legacy_keys = {
                    ("orders", 5, 1): f"achievement:five_orders:{user_id}",
                    ("orders", 10, 1): f"achievement:ten_orders:{user_id}",
                    ("orders", 25, 1): f"achievement:twenty_five_orders:{user_id}",
                    ("referrals", 5, 1): f"achievement:five_referrals:{user_id}",
                }
                legacy_key = legacy_keys.get((milestone_type, threshold, cycle))
                legacy_paid = False
                if legacy_key:
                    legacy_paid = bool((await session.execute(
                        select(ClubTransaction.id).where(ClubTransaction.event_key == legacy_key)
                    )).first())
                earned = 0 if legacy_paid else await _award_club_dcp(session, user_id, reward, event_key, reason)
                if earned:
                    messages.append(f"🏆 {reason}: +{earned} DCP")
    return messages


async def _ensure_season(session, now: datetime | None = None):
    now = now or datetime.utcnow()
    season_config = (await _club_config(session)).get("season", {})
    first_start = datetime.fromisoformat(season_config.get("first_start", SEASON_FIRST_START.isoformat()))
    season_length = max(1, int(season_config.get("length_days", SEASON_LENGTH_DAYS)))
    first_end = first_start + timedelta(days=season_length)
    first = await session.get(ClubSeason, 1)
    if first is None:
        first = ClubSeason(season_number=1, title=str(season_config.get("title", "Season 01 · Новый год"))[:96],
                           starts_at=first_start, ends_at=first_end)
        session.add(first)
        await session.flush()
    else:
        # Once a season record exists, its persisted start/end dates are authoritative.
        # This prevents later settings edits from unexpectedly resetting the season index.
        first_start = first.starts_at
        season_length = max(1, int((first.ends_at - first.starts_at).total_seconds() // 86400))
    if now < first_start:
        return first, False
    season_number = int((now - first_start).total_seconds() // (season_length * 86400)) + 1
    current = first
    for number in range(2, season_number + 1):
        current = await session.get(ClubSeason, number)
        if current is None:
            starts_at = first_start + timedelta(days=season_length * (number - 1))
            current = ClubSeason(season_number=number, title=f"{str(season_config.get('title_prefix', 'Season'))[:40]} {number:02d}",
                                 starts_at=starts_at, ends_at=starts_at + timedelta(days=season_length))
            session.add(current)
            await session.flush()
    return current, current.starts_at <= now < current.ends_at


async def _award_season_points(session, user_id: int, amount: int, event_key: str, reason: str) -> int:
    if amount <= 0:
        return 0
    season, active = await _ensure_season(session)
    if not active:
        return 0
    exists = (await session.execute(select(ClubSeasonPoint.id).where(ClubSeasonPoint.event_key == event_key))).first()
    if exists:
        return 0
    session.add(ClubSeasonPoint(user_id=user_id, season_number=season.season_number,
                                event_key=event_key, amount=amount, reason=reason))
    await session.flush()
    return amount


async def _finalize_expired_seasons(session, now: datetime):
    season_config = (await _club_config(session)).get("season", {})
    personal_rewards = season_config.get("personal_rewards", SEASON_PERSONAL_REWARDS)
    top_rewards = season_config.get("top_rewards", SEASON_TOP_REWARDS)
    seasons = (await session.execute(select(ClubSeason).where(ClubSeason.ends_at <= now))).scalars().all()
    for season in seasons:
        rows = (await session.execute(
            select(ClubSeasonPoint.user_id, func.sum(ClubSeasonPoint.amount).label("points"),
                   func.max(ClubSeasonPoint.created_at).label("reached_at"))
            .where(ClubSeasonPoint.season_number == season.season_number)
            .group_by(ClubSeasonPoint.user_id)
        )).all()
        ranking = sorted(rows, key=lambda row: (-int(row.points or 0), row.reached_at or datetime.max, int(row.user_id)))
        for row in rows:
            points = int(row.points or 0)
            for reward in personal_rewards:
                if points >= reward["points"]:
                    exists = (await session.execute(select(ClubSeasonReward.id).where(
                        ClubSeasonReward.user_id == row.user_id,
                        ClubSeasonReward.season_number == season.season_number,
                        ClubSeasonReward.reward_key == reward["key"]
                    ))).first()
                    if not exists:
                        session.add(ClubSeasonReward(user_id=row.user_id, season_number=season.season_number,
                            reward_key=reward["key"], title=reward["title"]))
        for rank, row in enumerate(ranking, 1):
            if rank > 50:
                break
            reward = next((item for item in top_rewards if item["from"] <= rank <= item["to"]), None)
            if reward:
                exists = (await session.execute(select(ClubSeasonReward.id).where(
                    ClubSeasonReward.user_id == row.user_id,
                    ClubSeasonReward.season_number == season.season_number,
                    ClubSeasonReward.reward_key == reward["key"]
                ))).first()
                if not exists:
                    session.add(ClubSeasonReward(user_id=row.user_id, season_number=season.season_number,
                        reward_key=reward["key"], title=reward["title"], rank=rank))
    await session.flush()


async def _sync_club_activity(session, user_id: int) -> list[str]:
    messages = await _sync_club_achievements(session, user_id)
    messages.extend(await _sync_repeatable_milestones(session, user_id))
    messages.extend(await _sync_club_tasks(session, user_id))
    return messages


@app.get("/api/club/activities")
async def club_activities(user: dict = Depends(current_user)):
    uid = user["id"]
    async with SessionLocal() as session:
        await _ensure_club_tasks(session)
        await _sync_club_achievements(session, uid)
        await _sync_club_tasks(session, uid)
        selected = await session.get(ClubTitleSelection, uid)
        unlocked = set((await session.execute(select(ClubAchievementUnlock.achievement_key).where(ClubAchievementUnlock.user_id == uid))).scalars().all())
        tasks = (await session.execute(select(ClubTaskConfig).where(ClubTaskConfig.active.is_(True)))).scalars().all()
        task_data = []
        for task in tasks:
            start, _period_key = _club_period(task, datetime.utcnow())
            count = await _club_task_count(session, uid, task, start)
            task_data.append({"key": task.key, "title": task.title, "description": task.description, "period": task.period,
                "target": int(task.target), "reward": int(task.reward), "progress": min(count, int(task.target)), "completed": count >= int(task.target)})
        config = await _club_config(session)
        metrics = await _club_metrics(session, uid)
        account = await session.get(ClubAccount, uid)
        lifetime = int(account.lifetime_earned or 0) if account else 0
        card_items = (await session.execute(select(ClubCosmetic).where(
            ClubCosmetic.kind == "card", ClubCosmetic.active.is_(True)
        ))).scalars().all()
        for card in card_items:
            req_type = card.requirement_type or "none"
            req_value = int(card.requirement_value or 0)
            metric_value = {"orders": metrics["orders"], "lifetime_dcp": lifetime, "referrals": metrics["referrals"]}.get(req_type, req_value)
            if req_type == "none" or metric_value >= req_value:
                owned_card = (await session.execute(select(ClubUserCosmetic.id).where(
                    ClubUserCosmetic.user_id == uid, ClubUserCosmetic.cosmetic_key == card.key
                ))).first()
                if not owned_card:
                    minted = await session.execute(update(ClubCosmetic).where(
                        ClubCosmetic.key == card.key,
                        ((ClubCosmetic.max_supply == 0) | (ClubCosmetic.issued_count < ClubCosmetic.max_supply))
                    ).values(issued_count=ClubCosmetic.issued_count + 1))
                    if minted.rowcount:
                        session.add(ClubUserCosmetic(user_id=uid, cosmetic_key=card.key, granted_by=None))
        await session.flush()
        cosmetics = (await session.execute(
            select(ClubCosmetic)
            .join(ClubUserCosmetic, ClubUserCosmetic.cosmetic_key == ClubCosmetic.key)
            .where(ClubUserCosmetic.user_id == uid, ClubCosmetic.active.is_(True))
            .order_by(ClubCosmetic.kind, ClubCosmetic.title)
        )).scalars().all()
        loadout = (await session.execute(select(ClubCosmeticLoadout).where(
            ClubCosmeticLoadout.user_id == uid
        ))).scalars().all()
        equipped_by_kind = {x.kind: x.cosmetic_key for x in loadout}
        await session.commit()
        return {"achievements": [{**item, "unlocked": item["key"] in unlocked}
                    for item in config.get("achievements", CLUB_ACHIEVEMENTS) if item.get("active", True)],
                "tasks": task_data, "selected_title": selected.title_key if selected else None,
                "cosmetics": [{"key": c.key, "kind": c.kind, "title": c.title, "description": c.description,
                    "icon": c.icon, "rarity": c.rarity, "equipped": equipped_by_kind.get(c.kind) == c.key,
                    "card_primary": c.card_primary, "card_secondary": c.card_secondary, "card_accent": c.card_accent,
                    "card_animation": c.card_animation, "requirement_type": c.requirement_type,
                    "requirement_value": int(c.requirement_value or 0), "card_font": c.card_font,
                    "max_supply": int(c.max_supply or 0), "issued_count": int(c.issued_count or 0)}
                    for c in cosmetics]}


@app.post("/api/club/title")
async def club_select_title(data: dict, user: dict = Depends(current_user)):
    key = str(data.get("title_key", "")).strip()
    async with SessionLocal() as session:
        config = await _club_config(session)
        allowed = {item["key"] for item in config.get("achievements", CLUB_ACHIEVEMENTS)
                   if item.get("reward_type") == "title" and item.get("active", True)}
        cosmetic = await session.get(ClubCosmetic, key)
        owned_cosmetic = (await session.execute(select(ClubUserCosmetic.id).where(
            ClubUserCosmetic.user_id == user["id"], ClubUserCosmetic.cosmetic_key == key
        ))).first()
        if key not in allowed and not (cosmetic and cosmetic.kind == "title" and owned_cosmetic):
            raise HTTPException(400, "Титул не открыт или не существует.")
        unlocked = (await session.execute(select(ClubAchievementUnlock.id).where(
            ClubAchievementUnlock.user_id == user["id"], ClubAchievementUnlock.achievement_key == key
        ))).first()
        if key in allowed and not unlocked:
            raise HTTPException(403, "Сначала открой это достижение.")
        selection = await session.get(ClubTitleSelection, user["id"])
        if selection is None:
            session.add(ClubTitleSelection(user_id=user["id"], title_key=key))
        else:
            selection.title_key = key
        await session.commit()
    return {"ok": True, "selected_title": key}


@app.post("/api/club/cosmetic/equip")
async def club_equip_cosmetic(data: dict, user: dict = Depends(current_user)):
    key = str(data.get("key", "")).strip()
    async with SessionLocal() as session:
        cosmetic = await session.get(ClubCosmetic, key)
        if not cosmetic or not cosmetic.active:
            raise HTTPException(404, "Косметический предмет не найден.")
        owned = (await session.execute(select(ClubUserCosmetic.id).where(
            ClubUserCosmetic.user_id == user["id"], ClubUserCosmetic.cosmetic_key == key
        ))).first()
        if not owned:
            raise HTTPException(403, "Этот предмет ещё не получен.")
        loadout = (await session.execute(select(ClubCosmeticLoadout).where(
            ClubCosmeticLoadout.user_id == user["id"], ClubCosmeticLoadout.kind == cosmetic.kind
        ))).scalar_one_or_none()
        if loadout is None:
            session.add(ClubCosmeticLoadout(user_id=user["id"], kind=cosmetic.kind, cosmetic_key=key))
        else:
            loadout.cosmetic_key = key
        if cosmetic.kind == "title":
            selection = await session.get(ClubTitleSelection, user["id"])
            if selection is None:
                session.add(ClubTitleSelection(user_id=user["id"], title_key=key))
            else:
                selection.title_key = key
        await session.commit()
    return {"ok": True, "equipped": key, "kind": cosmetic.kind}


@app.get("/api/admin/club/tasks")
async def admin_club_tasks(user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        await _ensure_club_tasks(session)
        rows = (await session.execute(select(ClubTaskConfig).order_by(ClubTaskConfig.period, ClubTaskConfig.key))).scalars().all()
        result = [{"key": t.key, "title": t.title, "description": t.description, "period": t.period, "event_type": t.event_type, "target": int(t.target), "reward": int(t.reward), "active": bool(t.active)} for t in rows]
        await session.commit()
        return result


@app.put("/api/admin/club/tasks/{task_key}")
async def admin_club_task_update(task_key: str, data: dict, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        task = await session.get(ClubTaskConfig, task_key)
        if not task:
            raise HTTPException(404, "Задание не найдено.")
        if "title" in data:
            title = str(data["title"]).strip()
            if not title or len(title) > 96:
                raise HTTPException(400, "Название задания должно быть от 1 до 96 символов.")
            task.title = title
        if "description" in data:
            task.description = str(data["description"]).strip()[:256]
        if "period" in data:
            if data["period"] not in ("daily", "weekly"):
                raise HTTPException(400, "Период должен быть daily или weekly.")
            task.period = data["period"]
        if "event_type" in data:
            if data["event_type"] not in ("orders", "reviews"):
                raise HTTPException(400, "Тип события должен быть orders или reviews.")
            task.event_type = data["event_type"]
        if "reward" in data:
            reward = int(data["reward"])
            if reward < 0 or reward > 100000:
                raise HTTPException(400, "Награда должна быть от 0 до 100000 DCP.")
            task.reward = reward
        if "target" in data:
            target = int(data["target"])
            if target < 1 or target > 100000:
                raise HTTPException(400, "Цель должна быть от 1 до 100000.")
            task.target = target
        if "active" in data:
            task.active = bool(data["active"])
        await session.commit()
        return {"ok": True}


@app.post("/api/admin/club/tasks")
async def admin_club_task_create(data: dict, user: dict = Depends(admin_user)):
    key = re.sub(r"[^a-z0-9_]", "_", str(data.get("key", "")).lower()).strip("_")
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    period = str(data.get("period", "daily"))
    event_type = str(data.get("event_type", "orders"))
    if not key or len(key) > 48 or not title or len(title) > 96:
        raise HTTPException(400, "Укажи уникальный ключ и название задания.")
    if period not in ("daily", "weekly"):
        raise HTTPException(400, "Период должен быть daily или weekly.")
    if event_type not in ("orders", "reviews"):
        raise HTTPException(400, "Пока доступны задания по завершённым заказам и опубликованным отзывам.")
    target = int(data.get("target", 1))
    reward = int(data.get("reward", 0))
    if not 1 <= target <= 100000 or not 0 <= reward <= 100000:
        raise HTTPException(400, "Цель: 1–100000, награда: 0–100000 DCP.")
    async with SessionLocal() as session:
        if await session.get(ClubTaskConfig, key):
            raise HTTPException(409, "Задание с таким ключом уже существует.")
        session.add(ClubTaskConfig(key=key, title=title, description=description[:256],
            period=period, event_type=event_type, target=target, reward=reward,
            active=bool(data.get("active", True))))
        await session.commit()
    return {"ok": True, "key": key}


@app.delete("/api/admin/club/tasks/{task_key}")
async def admin_club_task_delete(task_key: str, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        task = await session.get(ClubTaskConfig, task_key)
        if not task:
            raise HTTPException(404, "Задание не найдено.")
        await session.delete(task)
        await session.execute(delete(ClubTaskProgress).where(ClubTaskProgress.task_key == task_key))
        await session.commit()
    return {"ok": True}


@app.get("/api/admin/club/config")
async def admin_club_config(user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        config = await _club_config(session)
        await session.commit()
        return config


@app.put("/api/admin/club/config")
async def admin_club_config_update(data: dict, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        current = await _club_config(session)
        for section in ("economy", "levels", "achievements", "season"):
            if section in data:
                expected = dict if section in ("economy", "season") else list
                if not isinstance(data[section], expected):
                    raise HTTPException(400, f"Раздел {section} имеет неверный формат.")
                current[section] = data[section]
        economy = current.get("economy", {})
        for field, low, high in (
            ("order_dcp", 0, 1000000), ("first_order_dcp", 0, 1000000),
            ("referral_dcp", 0, 1000000), ("order_sp", 0, 1000000),
            ("referral_sp", 0, 1000000), ("daily_task_sp", 0, 1000000),
            ("weekly_task_sp", 0, 1000000), ("dcp_per_ruble", 1, 100000),
            ("min_order", 0, 10000000), ("min_product_price", 0, 10000000),
        ):
            value = int(economy.get(field, CLUB_DEFAULT_CONFIG["economy"][field]))
            if not low <= value <= high:
                raise HTTPException(400, f"Некорректное значение {field}.")
            economy[field] = value
        tiers = economy.get("redemption_tiers", [])
        if not isinstance(tiers, list) or len(tiers) > 20:
            raise HTTPException(400, "Некорректная таблица обмена DCP.")
        normalized_tiers = []
        previous_min = previous_discount = 0
        for tier in tiers:
            if not isinstance(tier, (list, tuple)) or len(tier) != 2:
                raise HTTPException(400, "Каждый уровень обмена должен содержать сумму корзины и скидку.")
            minimum, discount = int(tier[0]), int(tier[1])
            if minimum <= previous_min or discount <= previous_discount or minimum < 0 or discount < 1:
                raise HTTPException(400, "Уровни обмена должны возрастать по сумме и скидке.")
            normalized_tiers.append([minimum, discount])
            previous_min, previous_discount = minimum, discount
        economy["redemption_tiers"] = normalized_tiers
        for field in ("order_milestones", "referral_milestones"):
            values = economy.get(field, [])
            if not isinstance(values, list) or len(values) > 30:
                raise HTTPException(400, f"Некорректные рубежи {field}.")
            cleaned = []
            for item in values:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise HTTPException(400, "Рубеж должен содержать количество и награду.")
                threshold, reward = int(item[0]), int(item[1])
                if threshold < 1 or reward < 0 or reward > 1000000:
                    raise HTTPException(400, "Некорректный рубеж или награда.")
                cleaned.append([threshold, reward])
            economy[field] = cleaned
        levels = current.get("levels", [])
        if not isinstance(levels, list) or not levels or len(levels) > 20:
            raise HTTPException(400, "Должен быть хотя бы один уровень клуба.")
        seen = set()
        previous_thresholds = (-1, -1, -1)
        for level in levels:
            if not isinstance(level, dict):
                raise HTTPException(400, "Каждый уровень должен быть объектом.")
            code = str(level.get("code", "")).strip().upper()
            if not code or code in seen:
                raise HTTPException(400, "Ключи уровней должны быть уникальными.")
            seen.add(code)
            level["code"] = code
            level["title"] = str(level.get("title", code))[:48]
            level["orders"] = max(0, min(1000000, int(level.get("orders", 0))))
            level["points"] = max(0, min(100000000, int(level.get("points", 0))))
            level["referrals"] = max(0, min(1000000, int(level.get("referrals", 0))))
            thresholds = (level["orders"], level["points"], level["referrals"])
            if any(thresholds[i] < previous_thresholds[i] for i in range(3)):
                raise HTTPException(400, "Требования уровней должны идти по возрастанию.")
            previous_thresholds = thresholds
            perks = level.get("perks", [])
            if not isinstance(perks, list):
                raise HTTPException(400, "Привилегии уровня должны быть списком.")
            level["perks"] = [str(x)[:120] for x in perks[:10]]
        achievements = current.get("achievements", [])
        if not isinstance(achievements, list) or len(achievements) > 100:
            raise HTTPException(400, "Слишком много достижений.")
        seen = set()
        for item in achievements:
            if not isinstance(item, dict):
                raise HTTPException(400, "Каждое достижение должно быть объектом.")
            key = str(item.get("key", "")).strip().lower()
            if not key or key in seen or len(key) > 48:
                raise HTTPException(400, "Ключи достижений должны быть уникальными.")
            seen.add(key)
            if item.get("metric") not in ("orders", "referrals", "reviews"):
                raise HTTPException(400, "Для достижений доступны метрики orders, referrals и reviews.")
            if item.get("reward_type", "badge") not in ("badge", "title"):
                raise HTTPException(400, "Тип награды достижения должен быть badge или title.")
            item["key"] = key
            item["title"] = str(item.get("title", key))[:96]
            item["description"] = str(item.get("description", ""))[:256]
            item["icon"] = str(item.get("icon", "✦"))[:16]
            item["target"] = max(1, min(1000000, int(item.get("target", 1))))
            item["reward"] = max(0, min(1000000, int(item.get("reward", 0))))
        season = current.get("season", {})
        season["length_days"] = max(1, min(365, int(season.get("length_days", 30))))
        season["title"] = str(season.get("title", "Season 01 · Новый год"))[:96]
        season["title_prefix"] = str(season.get("title_prefix", "Season"))[:40]
        try:
            datetime.fromisoformat(str(season.get("first_start", SEASON_FIRST_START.isoformat())))
        except ValueError:
            raise HTTPException(400, "Дата старта сезона должна быть в формате YYYY-MM-DDTHH:MM:SS.")
        for field in ("personal_rewards", "top_rewards"):
            if not isinstance(season.get(field, []), list) or len(season.get(field, [])) > 100:
                raise HTTPException(400, "Некорректные сезонные награды.")
        for reward in season.get("personal_rewards", []):
            reward["points"] = max(0, min(100000000, int(reward.get("points", 0))))
            reward["key"] = str(reward.get("key", ""))[:64]
            reward["title"] = str(reward.get("title", ""))[:160]
            if not reward["key"] or not reward["title"]:
                raise HTTPException(400, "У каждой личной сезонной награды нужны ключ и название.")
        for reward in season.get("top_rewards", []):
            reward["from"] = max(1, min(100000, int(reward.get("from", 1))))
            reward["to"] = max(reward["from"], min(100000, int(reward.get("to", reward["from"]))))
            reward["key"] = str(reward.get("key", ""))[:64]
            reward["title"] = str(reward.get("title", ""))[:160]
            if not reward["key"] or not reward["title"]:
                raise HTTPException(400, "У каждой награды рейтинга нужны ключ и название.")
        season_row = await session.get(ClubSeason, 1)
        if season_row is not None:
            season_row.title = str(season.get("title", season_row.title))[:96]
            has_points = (await session.execute(select(ClubSeasonPoint.id).where(
                ClubSeasonPoint.season_number == 1
            ).limit(1))).first()
            first_start = datetime.fromisoformat(str(season.get("first_start", SEASON_FIRST_START.isoformat())))
            if not has_points:
                season_row.starts_at = first_start
                season_row.ends_at = first_start + timedelta(days=season["length_days"])
            else:
                persisted_length = max(1, int((season_row.ends_at - season_row.starts_at).total_seconds() // 86400))
                if first_start != season_row.starts_at or season["length_days"] != persisted_length:
                    raise HTTPException(409, "Дату и длительность сезона нельзя менять после начисления первых SP.")
        await _save_club_config(session, current)
        await session.commit()
        return {"ok": True, "config": current}


@app.post("/api/admin/club/users/{user_id}/balance")
async def admin_club_adjust_balance(user_id: int, data: dict, user: dict = Depends(admin_user)):
    delta = int(data.get("amount", 0))
    reason = str(data.get("reason", "")).strip()[:256]
    if not delta or abs(delta) > 10000000 or not reason:
        raise HTTPException(400, "Укажи сумму изменения (не 0, до 10 млн DCP) и причину.")
    async with SessionLocal() as session:
        target = await session.get(User, user_id)
        if not target:
            raise HTTPException(404, "Пользователь не найден.")
        account = await session.get(ClubAccount, user_id)
        if account is None:
            account = ClubAccount(user_id=user_id, balance=0, lifetime_earned=0)
            session.add(account)
            await session.flush()
        new_balance = int(account.balance or 0) + delta
        if new_balance < 0:
            raise HTTPException(409, "На балансе недостаточно DCP для списания.")
        account.balance = new_balance
        if delta > 0:
            account.lifetime_earned = int(account.lifetime_earned or 0) + delta
        session.add(ClubTransaction(user_id=user_id, event_key=f"admin-adjust:{user_id}:{secrets.token_hex(8)}",
            kind="admin_adjust", amount=delta, balance_after=new_balance, reason=reason))
        await session.commit()
    return {"ok": True, "balance": new_balance}


@app.delete("/api/admin/club/users/{user_id}/cosmetics/{cosmetic_key}")
async def admin_club_revoke_cosmetic(user_id: int, cosmetic_key: str, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        await session.execute(delete(ClubUserCosmetic).where(
            ClubUserCosmetic.user_id == user_id, ClubUserCosmetic.cosmetic_key == cosmetic_key
        ))
        await session.execute(delete(ClubCosmeticLoadout).where(
            ClubCosmeticLoadout.user_id == user_id, ClubCosmeticLoadout.cosmetic_key == cosmetic_key
        ))
        selection = await session.get(ClubTitleSelection, user_id)
        if selection and selection.title_key == cosmetic_key:
            await session.delete(selection)
        await session.commit()
    return {"ok": True}


@app.post("/api/admin/club/users/{user_id}/season-points")
async def admin_club_adjust_season_points(user_id: int, data: dict, user: dict = Depends(admin_user)):
    delta = int(data.get("amount", 0))
    reason = str(data.get("reason", "")).strip()[:160]
    if not delta or abs(delta) > 10000000 or not reason:
        raise HTTPException(400, "Укажи изменение сезонных очков и причину.")
    async with SessionLocal() as session:
        if not await session.get(User, user_id):
            raise HTTPException(404, "Пользователь не найден.")
        season, active = await _ensure_season(session)
        if not active:
            raise HTTPException(409, "Сейчас нет активного сезона.")
        current = int((await session.execute(select(func.coalesce(func.sum(ClubSeasonPoint.amount), 0)).where(
            ClubSeasonPoint.user_id == user_id, ClubSeasonPoint.season_number == season.season_number
        ))).scalar() or 0)
        if current + delta < 0:
            raise HTTPException(409, "Нельзя списать больше сезонных очков, чем есть у пользователя.")
        session.add(ClubSeasonPoint(user_id=user_id, season_number=season.season_number,
            event_key=f"admin-season-adjust:{user_id}:{secrets.token_hex(8)}",
            amount=delta, reason=reason))
        await session.commit()
    return {"ok": True, "season_number": season.season_number, "points": current + delta}


@app.get("/api/admin/club/cosmetics")
async def admin_club_cosmetics(user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(ClubCosmetic).order_by(ClubCosmetic.kind, ClubCosmetic.key))).scalars().all()
        return [{"key": x.key, "kind": x.kind, "title": x.title, "description": x.description,
                 "icon": x.icon, "rarity": x.rarity, "active": bool(x.active),
                 "card_primary": x.card_primary, "card_secondary": x.card_secondary, "card_accent": x.card_accent,
                 "card_animation": x.card_animation, "requirement_type": x.requirement_type,
                 "requirement_value": int(x.requirement_value or 0), "card_font": x.card_font,
                 "max_supply": int(x.max_supply or 0), "issued_count": int(x.issued_count or 0)} for x in rows]


@app.post("/api/admin/club/cosmetics")
async def admin_club_cosmetic_create(data: dict, user: dict = Depends(admin_user)):
    key = re.sub(r"[^a-z0-9_]", "_", str(data.get("key", "")).lower()).strip("_")
    kind = str(data.get("kind", "badge"))
    title = str(data.get("title", "")).strip()
    if not key or len(key) > 48 or not title or kind not in ("title", "badge", "frame", "effect", "card"):
        raise HTTPException(400, "Укажи ключ, название и корректный тип косметики.")
    async with SessionLocal() as session:
        if await session.get(ClubCosmetic, key):
            raise HTTPException(409, "Косметика с таким ключом уже существует.")
        requirement_type = str(data.get("requirement_type", "none"))
        animation = str(data.get("card_animation", "pulse"))
        card_font = str(data.get("card_font", "orbitron"))
        allowed_animations = ("none", "pulse", "sheen", "aurora", "scan", "shimmer", "orbit", "ember")
        allowed_fonts = ("orbitron", "rajdhani", "exo2", "montserrat", "georgia", "mono")
        if requirement_type not in ("none", "orders", "lifetime_dcp", "referrals") or animation not in allowed_animations or card_font not in allowed_fonts:
            raise HTTPException(400, "Некорректное условие, анимация или шрифт карточки.")
        def valid_hex(value, fallback):
            value = str(value or fallback).strip()
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                raise HTTPException(400, "Цвет карточки должен быть в формате #RRGGBB.")
            return value
        session.add(ClubCosmetic(key=key, kind=kind, title=title[:96],
            description=str(data.get("description", ""))[:256], icon=str(data.get("icon", "✦"))[:16],
            rarity=str(data.get("rarity", "common"))[:16], active=bool(data.get("active", True)),
            card_primary=valid_hex(data.get("card_primary"), "#26133b"),
            card_secondary=valid_hex(data.get("card_secondary"), "#0b0812"),
            card_accent=valid_hex(data.get("card_accent"), "#c49aff"),
            card_animation=animation, requirement_type=requirement_type,
            requirement_value=max(0, min(100000000, int(data.get("requirement_value", 0)))),
            card_font=card_font, max_supply=max(0, min(100000, int(data.get("max_supply", 0)))), issued_count=0)
        await session.commit()
    return {"ok": True, "key": key}


@app.put("/api/admin/club/cosmetics/{cosmetic_key}")
async def admin_club_cosmetic_update(cosmetic_key: str, data: dict, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        item = await session.get(ClubCosmetic, cosmetic_key)
        if not item:
            raise HTTPException(404, "Косметика не найдена.")
        if "title" in data:
            title = str(data["title"]).strip()
            if not title or len(title) > 96:
                raise HTTPException(400, "Название должно быть от 1 до 96 символов.")
            item.title = title
        if "description" in data:
            item.description = str(data["description"])[:256]
        if "icon" in data:
            item.icon = str(data["icon"])[:16]
        if "rarity" in data:
            item.rarity = str(data["rarity"])[:16]
        if "active" in data:
            item.active = bool(data["active"])
        for field in ("card_primary", "card_secondary", "card_accent"):
            if field in data:
                value = str(data[field]).strip()
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                    raise HTTPException(400, "Цвет карточки должен быть в формате #RRGGBB.")
                setattr(item, field, value)
        if "card_animation" in data:
            if data["card_animation"] not in ("none", "pulse", "sheen", "aurora", "scan", "shimmer", "orbit", "ember"):
                raise HTTPException(400, "Неизвестная анимация карточки.")
            item.card_animation = data["card_animation"]
        if "card_font" in data:
            if data["card_font"] not in ("orbitron", "rajdhani", "exo2", "montserrat", "georgia", "mono"):
                raise HTTPException(400, "Неизвестный шрифт карточки.")
            item.card_font = data["card_font"]
        if "max_supply" in data:
            item.max_supply = max(0, min(100000, int(data["max_supply"]))
        if "requirement_type" in data:
            if data["requirement_type"] not in ("none", "orders", "lifetime_dcp", "referrals"):
                raise HTTPException(400, "Неизвестное условие получения карточки.")
            item.requirement_type = data["requirement_type"]
        if "requirement_value" in data:
            item.requirement_value = max(0, min(100000000, int(data["requirement_value"])))
        await session.commit()
    return {"ok": True}


@app.delete("/api/admin/club/cosmetics/{cosmetic_key}")
async def admin_club_cosmetic_delete(cosmetic_key: str, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        item = await session.get(ClubCosmetic, cosmetic_key)
        if not item:
            raise HTTPException(404, "Косметика не найдена.")
        await session.execute(delete(ClubUserCosmetic).where(ClubUserCosmetic.cosmetic_key == cosmetic_key))
        await session.execute(delete(ClubCosmeticLoadout).where(ClubCosmeticLoadout.cosmetic_key == cosmetic_key))
        await session.delete(item)
        await session.commit()
    return {"ok": True}


@app.post("/api/admin/club/users/{user_id}/cosmetics/{cosmetic_key}")
async def admin_club_grant_cosmetic(user_id: int, cosmetic_key: str, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        if not await session.get(User, user_id):
            raise HTTPException(404, "Пользователь не найден.")
        cosmetic = await session.get(ClubCosmetic, cosmetic_key)
        if not cosmetic or not cosmetic.active:
            raise HTTPException(404, "Активная косметика не найдена.")
        exists = (await session.execute(select(ClubUserCosmetic.id).where(
            ClubUserCosmetic.user_id == user_id, ClubUserCosmetic.cosmetic_key == cosmetic_key
        ))).first()
        if not exists:
            minted = await session.execute(update(ClubCosmetic).where(
                ClubCosmetic.key == cosmetic_key,
                ((ClubCosmetic.max_supply == 0) | (ClubCosmetic.issued_count < ClubCosmetic.max_supply))
            ).values(issued_count=ClubCosmetic.issued_count + 1))
            if not minted.rowcount:
                raise HTTPException(409, "Лимит выпуска этой клубной карточки исчерпан.")
            session.add(ClubUserCosmetic(user_id=user_id, cosmetic_key=cosmetic_key, granted_by=user["id"]))
        await session.commit()
    return {"ok": True}


@app.get("/api/club")
async def club_profile(user: dict = Depends(current_user)):
    uid = user["id"]
    async with SessionLocal() as session:
        account = await session.get(ClubAccount, uid)
        balance = int(account.balance or 0) if account else 0
        lifetime = int(account.lifetime_earned or 0) if account else 0
        orders = (await _club_metrics(session, uid))["orders"]
        referrals = int((await session.execute(select(func.count(User.id)).where(
            User.referrer_id == uid, User.first_purchase_done.is_(True)
        ))).scalar() or 0)
        rows = (await session.execute(select(ClubTransaction).where(
            ClubTransaction.user_id == uid
        ).order_by(ClubTransaction.created_at.desc(), ClubTransaction.id.desc()).limit(20))).scalars().all()
        config = await _club_config(session)
    levels = config.get("levels", CLUB_LEVELS)
    level_index = 0
    for index, level in enumerate(levels):
        if orders >= level["orders"] and lifetime >= level["points"] and referrals >= level.get("referrals", 0):
            level_index = index
    current = levels[level_index]
    nxt = levels[level_index + 1] if level_index + 1 < len(levels) else None
    if nxt:
        ratios = [orders / max(nxt["orders"], 1), lifetime / max(nxt["points"], 1)]
        if nxt.get("referrals", 0):
            ratios.append(referrals / nxt["referrals"])
        progress = round(min(ratios) * 100)
    else:
        progress = 100
    monthly_coupon = None
    async with SessionLocal() as session:
        monthly_coupon = await _ensure_monthly_club_coupon(session, uid, current["code"])
    return {
        "monthly_coupon": monthly_coupon,
        "balance": balance, "lifetime_earned": lifetime, "completed_orders": orders,
        "completed_referrals": referrals,
        "level": current["code"], "level_title": current["title"], "level_perks": current["perks"],
        "levels": [{**level, "unlocked": level_index >= i} for i, level in enumerate(levels)],
        "next_level": nxt["code"] if nxt else None,
        "next_level_orders": nxt["orders"] if nxt else None,
        "next_level_points": nxt["points"] if nxt else None,
        "next_level_referrals": nxt.get("referrals", 0) if nxt else 0,
        "progress_percent": max(0, min(100, progress)),
        "transactions": [{"id": row.id, "kind": row.kind, "amount": int(row.amount),
            "balance_after": int(row.balance_after), "reason": row.reason,
            "created_at": row.created_at.isoformat() if row.created_at else ""} for row in rows]
    }


@app.get("/api/club/season")
async def club_season(user: dict = Depends(current_user)):
    uid = user["id"]
    now = datetime.utcnow()
    async with SessionLocal() as session:
        season, active = await _ensure_season(session, now)
        await _finalize_expired_seasons(session, now)
        rows = (await session.execute(
            select(User.id, User.username,
                   func.sum(ClubSeasonPoint.amount).label("points"),
                   func.max(ClubSeasonPoint.created_at).label("reached_at"))
            .join(ClubSeasonPoint, ClubSeasonPoint.user_id == User.id)
            .where(ClubSeasonPoint.season_number == season.season_number)
            .group_by(User.id, User.username)
        )).all()
        rewards = (await session.execute(select(ClubSeasonReward).where(
            ClubSeasonReward.user_id == uid
        ).order_by(ClubSeasonReward.season_number.desc(), ClubSeasonReward.id.desc()))).scalars().all()
        season_config = (await _club_config(session)).get("season", {})
        await session.commit()
    personal_rewards = season_config.get("personal_rewards", SEASON_PERSONAL_REWARDS)
    top_rewards = season_config.get("top_rewards", SEASON_TOP_REWARDS)
    ranking = sorted(
        [{"user_id": int(row.id), "username": row.username or "",
          "points": int(row.points or 0), "reached_at": row.reached_at}
         for row in rows],
        key=lambda row: (-row["points"], row["reached_at"] or datetime.max, row["user_id"])
    )
    for index, row in enumerate(ranking, 1):
        row["position"] = index
    mine = next((row for row in ranking if row["user_id"] == uid), None)
    my_points = mine["points"] if mine else 0
    my_rank = mine["position"] if mine else None
    return {
        "season_number": season.season_number, "title": season.title, "active": active,
        "starts_at": season.starts_at.isoformat(), "ends_at": season.ends_at.isoformat(),
        "days_left": max(0, (season.ends_at - now).days),
        "points": my_points, "rank": my_rank, "participants": len(ranking),
        "leaderboard": [{"position": row["position"], "user_id": row["user_id"],
                         "username": row["username"], "points": row["points"]} for row in ranking[:50]],
        "personal_rewards": [{**reward, "unlocked": my_points >= reward["points"]}
                             for reward in personal_rewards],
        "top_rewards": top_rewards,
        "earned_rewards": [{"season": r.season_number, "key": r.reward_key, "title": r.title, "rank": r.rank}
                           for r in rewards],
    }


@app.get("/api/profile")
async def profile(user: dict = Depends(current_user)):
    uid = user["id"]
    async with SessionLocal() as session:
        record = await session.get(User, uid)
        spent = (await _club_metrics(session, uid))["orders"]
        promos = (await session.execute(select(func.count(Promocode.code)).where(Promocode.owner_id == uid, Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses))).scalar() or 0
    ref_link = f"https://t.me/{bot.BOT_USERNAME}?start=ref_{uid}" if bot.BOT_USERNAME else ""
    return {"user_id": uid, "username": record.username if record else user.get("username"), "telegram_username": user.get("username") or "", "first_name": user.get("first_name") or "", "last_name": user.get("last_name") or "", "photo_url": user.get("photo_url") or "",
        "balance": float(record.balance or 0) if record else 0, "orders_count": int(spent),
        "referrals_count": int(record.referrals_count or 0) if record else 0,
        "next_bonus_at": int(record.next_bonus_at or REFERRAL_MILESTONE) if record else REFERRAL_MILESTONE,
        "ref_link": ref_link, "active_promos": int(promos), "is_admin": uid == ADMIN_ID}


@app.get("/api/promos")
async def my_promos(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        level_code = await _club_current_level_code(session, user["id"])
        await _ensure_monthly_club_coupon(session, user["id"], level_code)
        rows = (await session.execute(select(Promocode).where(Promocode.owner_id == user["id"], Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses).order_by(Promocode.code))).scalars().all()
        return [{"code": p.code, "kind": p.kind, "amount": float(p.amount), "used_count": p.used_count, "max_uses": p.max_uses,
                 "is_club": p.created_by == "club", "min_order": float(p.min_order or 0),
                 "expires_at": _promo_datetime_to_local(p.expires_at)} for p in rows]


@app.get("/api/top")
async def top_buyers(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(User).where(User.balance > 0).order_by(User.balance.desc()).limit(10))).scalars().all()
        return [{"username": u.username, "user_id": u.id, "value": float(u.balance or 0)} for u in rows]


@app.get("/api/topref")
async def top_referrers(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(User).where(User.referrals_count > 0).order_by(User.referrals_count.desc()).limit(10))).scalars().all()
        return [{"username": u.username, "user_id": u.id, "value": int(u.referrals_count or 0)} for u in rows]


@app.post("/api/review")
async def add_review(
    order_id: int = Form(..., gt=0),
    rating: int = Form(..., ge=1, le=5),
    text: str = Form(..., min_length=3, max_length=2048),
    photo: UploadFile | None = File(default=None),
    user: dict = Depends(current_user),
):
    uid = user["id"]
    review_text = text.strip()
    if len(review_text) < 3:
        raise HTTPException(422, "Напиши отзыв хотя бы в 3 символа.")
    saved_path: Path | None = None
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != uid or order.status != "completed":
            raise HTTPException(400, "Отзыв можно оставить только к своему подтверждённому заказу.")
        exists = (await session.execute(select(Review.id).where(Review.order_id == order_id))).first()
        if exists:
            raise HTTPException(409, "Для этого заказа отзыв уже оставлен.")
        account = await session.get(User, uid)
        username = account.username if account else user.get("username")
        photo_url = None
        try:
            if photo and photo.filename:
                if photo.content_type not in ALLOWED_PRODUCT_IMAGE_TYPES:
                    raise HTTPException(415, "Поддерживаются только JPG, PNG и WEBP.")
                content = await photo.read(MAX_PRODUCT_IMAGE_SIZE + 1)
                if not content or len(content) > MAX_PRODUCT_IMAGE_SIZE:
                    raise HTTPException(413, "Размер фотографии — не более 5 МБ.")
                try:
                    optimized = await asyncio.to_thread(optimize_product_image, content)
                except ValueError as exc:
                    raise HTTPException(415, str(exc)) from exc
                filename = f"{secrets.token_hex(12)}.webp"
                saved_path = REVIEW_UPLOAD_DIR / filename
                saved_path.write_bytes(optimized)
                photo_url = f"/uploads/reviews/{filename}"
            review = Review(user_id=uid, order_id=order_id, rating=rating, text=review_text,
                            photo_id=photo_url, has_photo=bool(photo_url), status="pending")
            session.add(review)
            await session.commit()
            await session.refresh(review)
            review_id = review.id
        except Exception:
            if saved_path:
                saved_path.unlink(missing_ok=True)
            raise
    notified = await bot.send_review_moderation(
        review_id=review_id, order_id=order_id, username=username, user_id=uid,
        rating=rating, review_text=review_text, photo_path=str(saved_path) if saved_path else None,
    )
    return {"ok": True, "status": "pending", "notified_admin": notified}

async def _create_promo(session, owner_id: int, kind: str, amount: float, max_uses: int = 1, created_by: str = "ref") -> str:
    for _ in range(20):
        code = secrets.token_hex(4).upper()
        if not await session.get(Promocode, code):
            session.add(Promocode(code=code, owner_id=owner_id, kind=kind, amount=round(amount, 2), used=False, created_by=created_by, max_uses=max_uses, used_count=0))
            await session.flush()
            return code
    raise RuntimeError("Не удалось сгенерировать уникальный промокод.")


ORDER_STATUS_LABELS = {"accepted": "Принят", "assembling": "В сборке", "assembled": "Собран", "in_transit": "В пути", "completed": "Выполнен", "rejected": "Отклонён"}


async def _set_order_status(order_id: int, new_status: str, actor_id: int | None = None) -> dict:
    if new_status not in ORDER_STATUS_LABELS:
        raise HTTPException(400, "Неизвестный статус заказа.")
    notifications: list[tuple[int, str]] = []
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order:
            raise HTTPException(404, "Заказ не найден.")
        current = order.status or "accepted"
        if current in ("completed", "rejected"):
            raise HTTPException(409, "Заказ уже завершён.")
        allowed = {"accepted": {"assembling", "rejected"}, "assembling": {"assembled", "rejected"},
                   "assembled": {"in_transit", "completed", "rejected"}, "in_transit": {"completed"}}
        if new_status not in allowed.get(current, set()):
            raise HTTPException(409, f"Нельзя изменить статус «{ORDER_STATUS_LABELS.get(current, current)}» на «{ORDER_STATUS_LABELS[new_status]}».")
        if new_status == "in_transit" and order.delivery_type != "cdek":
            raise HTTPException(400, "Статус «В пути» доступен только для доставки.")
        if new_status == "completed" and order.delivery_type == "cdek" and current != "in_transit":
            raise HTTPException(409, "Для доставки сначала установи статус «В пути».")
        items = (await session.execute(select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id).where(OrderItem.order_id == order_id))).all()
        buyer = await session.get(User, order.user_id)
        order.status = new_status
        session.add(OrderStatusHistory(order_id=order.id, status=new_status, changed_by=actor_id, note="Изменение статуса заказа"))
        if new_status == "rejected":
            for order_item, product in items:
                product.stock_qty = int(product.stock_qty or 0) + int(order_item.quantity or 1)
                product.in_stock = product.stock_qty > 0
            if int(order.dcp_spent or 0) > 0 and buyer:
                refund_key = f"order:{order.id}:club-refund"
                already_refunded = (await session.execute(select(ClubTransaction.id).where(ClubTransaction.event_key == refund_key))).first()
                if not already_refunded:
                    account = await session.get(ClubAccount, buyer.id)
                    if account is None:
                        account = ClubAccount(user_id=buyer.id, balance=0, lifetime_earned=0)
                        session.add(account)
                        await session.flush()
                    refund_points = int(order.dcp_spent)
                    account.balance = int(account.balance or 0) + refund_points
                    session.add(ClubTransaction(user_id=buyer.id, event_key=refund_key, kind="refund", amount=refund_points,
                        balance_after=account.balance, reason=f"Возврат DCP за отклонённый заказ DSS-{order.id:06d}"))
                    notifications.append((buyer.id, f"↩️ DSS CLUB: возвращено {refund_points} DCP за отклонённый заказ."))
        if new_status == "completed":
            if buyer:
                buyer.balance = float(buyer.balance or 0) + float(order.total)
                club_economy = (await _club_config(session)).get("economy", {})
                order_dcp = int(club_economy.get("order_dcp", 100))
                order_sp = int(club_economy.get("order_sp", 10))
                earned = await _award_club_dcp(session, buyer.id, order_dcp, f"order:{order.id}:completed", "Завершённый заказ")
                season_earned = await _award_season_points(session, buyer.id, order_sp, f"season:order:{order.id}", "Завершённый заказ")
                if earned:
                    notifications.append((buyer.id, f"💜 DSS CLUB: +{earned} DCP за завершённый заказ."))
                if season_earned:
                    notifications.append((buyer.id, f"❄️ DSS CLUB: +{season_earned} сезонных очков SP за заказ."))
                if not buyer.first_purchase_done:
                    first_bonus = await _award_club_dcp(session, buyer.id, int(club_economy.get("first_order_dcp", 200)), f"user:{buyer.id}:first-order", "Бонус за первую покупку")
                    if first_bonus:
                        notifications.append((buyer.id, "🎉 DSS CLUB: +200 DCP за первую покупку!"))
                    if buyer.referrer_id:
                        ref_id = buyer.referrer_id
                        referral_fixed = round(float(order.total) * REFERRAL_BONUS_PERCENT / 100, 2)
                        bonus_code = await _create_promo(session, ref_id, "fixed", referral_fixed)
                        referrer = await session.get(User, ref_id)
                        milestone_code = None
                        if referrer:
                            referrer.referrals_count = int(referrer.referrals_count or 0) + 1
                            referral_dcp = await _award_club_dcp(session, ref_id, int(club_economy.get("referral_dcp", 150)), f"referral:first-order:{buyer.id}", "Приглашённый завершил первую покупку")
                            referral_sp = await _award_season_points(session, ref_id, int(club_economy.get("referral_sp", 25)), f"season:referral:{buyer.id}", "Новый приглашённый завершил покупку")
                            if referral_dcp:
                                notifications.append((ref_id, "👥 DSS CLUB: +150 DCP — приглашённый совершил первую покупку."))
                            if referral_sp:
                                notifications.append((ref_id, "❄️ DSS CLUB: +25 сезонных очков SP за нового покупателя по твоей ссылке."))
                            next_at = int(referrer.next_bonus_at or REFERRAL_MILESTONE)
                            if referrer.referrals_count >= next_at:
                                milestone_code = await _create_promo(session, ref_id, "percent", REFERRAL_MILESTONE_DISCOUNT)
                                referrer.next_bonus_at = next_at + REFERRAL_MILESTONE
                            notifications.append((ref_id, f"🎁 Твой приглашённый завершил первую покупку. Промокод на {int(referral_fixed)} ₽: {bonus_code}"))
                            if milestone_code:
                                notifications.append((ref_id, f"🏆 Уже {referrer.referrals_count} купивших рефералов! Промокод на −{REFERRAL_MILESTONE_DISCOUNT}%: {milestone_code}"))
                    buyer.first_purchase_done = True
                await session.flush()
                notifications.extend((buyer.id, msg) for msg in await _sync_club_activity(session, buyer.id))
                if buyer.referrer_id:
                    notifications.extend((buyer.referrer_id, msg) for msg in await _sync_club_activity(session, buyer.referrer_id))
            if order.promo_codes:
                for code in filter(None, order.promo_codes.split(",")):
                    promo = await session.get(Promocode, code)
                    if promo and promo.used_count < promo.max_uses:
                        promo.used_count += 1
                        promo.used = promo.used_count >= promo.max_uses
        if buyer:
            msg = f"❌ Заказ DSS-{order_id:06d} отклонён. Зарезервированный товар возвращён на склад." if new_status == "rejected" else f"📦 Заказ DSS-{order_id:06d}: статус изменён — {ORDER_STATUS_LABELS[new_status]}."
            notifications.append((buyer.id, msg))
        await session.commit()
    for user_id, message in notifications:
        await bot.send_message_safe(user_id, message)
    return {"status": new_status, "order_id": order_id, "order_number": f"DSS-{order_id:06d}"}


async def _process_order(order_id: int, approve: bool) -> dict:
    return await _set_order_status(order_id, "assembling" if approve else "rejected", ADMIN_ID)


@app.get("/api/admin/products")
async def admin_products(user: dict = Depends(admin_user)):
    active_statuses = ("accepted", "assembling", "assembled", "in_transit")
    async with SessionLocal() as session:
        rows = (await session.execute(select(Product).order_by(Product.category_id, Product.number, Product.id))).scalars().all()
        reserved_rows = (await session.execute(
            select(OrderItem.product_id, func.sum(OrderItem.quantity))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.status.in_(active_statuses))
            .group_by(OrderItem.product_id)
        )).all()
        reserved = {int(product_id): int(quantity or 0) for product_id, quantity in reserved_rows}
        return [{"id": p.id, "category_id": p.category_id, "number": p.number, "brand": p.brand, "flavor": p.flavor, "price": float(p.price), "cost_price": float(p.cost_price) if p.cost_price is not None else None, "in_stock": bool(p.in_stock), "stock_qty": int(p.stock_qty or 0), "reserved_qty": reserved.get(p.id, 0), "images": p.images} for p in rows]


@app.post("/api/admin/products")
async def admin_product_create(data: ProductInput, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        if not await session.get(Category, data.category_id):
            raise HTTPException(404, "Категория не найдена.")
        n = (await session.execute(select(func.max(Product.number)).where(Product.category_id == data.category_id))).scalar() or 0
        product = Product(category_id=data.category_id, number=n + 1, brand=data.brand.strip(), flavor=data.flavor.strip(), price=data.price, cost_price=data.cost_price, stock_qty=data.stock_qty, in_stock=data.stock_qty > 0)
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return {"id": product.id, "number": product.number}



@app.post("/api/admin/products/{product_id}/images")
async def admin_product_upload_images(product_id: int, files: list[UploadFile] = File(...), user: dict = Depends(admin_user)):
    if not files or len(files) > 5:
        raise HTTPException(400, "Можно загрузить от 1 до 5 фотографий за раз.")
    saved: list[str] = []
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            raise HTTPException(404, "Товар не найден.")
        existing = product.images
        if len(existing) + len(files) > 5:
            raise HTTPException(400, "У товара может быть не больше 5 фотографий.")
        try:
            for upload in files:
                if (upload.content_type not in ALLOWED_PRODUCT_IMAGE_TYPES):
                    raise HTTPException(415, "Поддерживаются только JPG, PNG и WEBP.")
                content = await upload.read(MAX_PRODUCT_IMAGE_SIZE + 1)
                if not content or len(content) > MAX_PRODUCT_IMAGE_SIZE:
                    raise HTTPException(413, "Размер одной фотографии — не более 5 МБ.")
                try:
                    optimized = await asyncio.to_thread(optimize_product_image, content)
                except ValueError as exc:
                    raise HTTPException(415, str(exc)) from exc
                filename = f"{product_id}_{secrets.token_hex(12)}.webp"
                (PRODUCT_UPLOAD_DIR / filename).write_bytes(optimized)
                saved.append(f"/uploads/products/{filename}")
                log.info("Product image optimized: %s -> %s bytes", len(content), len(optimized))
            product.image_urls = json.dumps(existing + saved, ensure_ascii=False)
            await session.commit()
        except Exception:
            for url in saved:
                (PRODUCT_UPLOAD_DIR / Path(url).name).unlink(missing_ok=True)
            raise
    return {"images": existing + saved}


@app.delete("/api/admin/products/{product_id}/images/{image_index}")
async def admin_product_delete_image(product_id: int, image_index: int, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            raise HTTPException(404, "Товар не найден.")
        images = product.images
        if image_index < 0 or image_index >= len(images):
            raise HTTPException(404, "Фотография не найдена.")
        removed = images.pop(image_index)
        product.image_urls = json.dumps(images, ensure_ascii=False)
        await session.commit()
    filename = Path(removed).name
    if filename and Path(filename).name == filename:
        (PRODUCT_UPLOAD_DIR / filename).unlink(missing_ok=True)
    return {"images": images}


@app.get("/uploads/products/{filename}")
async def product_image(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not re.fullmatch(r"[0-9]+_[a-f0-9]{24}\.(jpg|png|webp)", safe_name):
        raise HTTPException(404, "Фотография не найдена.")
    target = PRODUCT_UPLOAD_DIR / safe_name
    if not target.is_file():
        raise HTTPException(404, "Фотография не найдена.")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.put("/api/admin/products/{product_id}")
async def admin_product_update(product_id: int, data: ProductPatch, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        p = await session.get(Product, product_id)
        if not p:
            raise HTTPException(404, "Товар не найден.")
        values = data.model_dump(exclude_unset=True)
        if values.get("category_id") is not None and not await session.get(Category, values["category_id"]):
            raise HTTPException(404, "Категория не найдена.")
        for key, value in values.items():
            if key == "cost_price":
                p.cost_price = value
            elif value is not None:
                setattr(p, key, value.strip() if key in ("brand", "flavor") else value)
        if "stock_qty" in values and values["stock_qty"] is not None:
            p.in_stock = values["stock_qty"] > 0
        elif values.get("in_stock") is True and int(p.stock_qty or 0) <= 0:
            p.stock_qty = 1
        elif values.get("in_stock") is False:
            p.stock_qty = 0
        await session.commit()
        return {"ok": True}


@app.delete("/api/admin/products/{product_id}")
async def admin_product_delete(product_id: int, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        p = await session.get(Product, product_id)
        if not p:
            raise HTTPException(404, "Товар не найден.")
        used = (await session.execute(select(OrderItem.id).where(OrderItem.product_id == product_id).limit(1))).first()
        if used:
            p.in_stock = False
            p.stock_qty = 0
            await session.commit()
            return {"ok": True, "archived": True}
        await session.execute(delete(CartItem).where(CartItem.product_id == product_id))
        await session.delete(p)
        await session.commit()
        return {"ok": True, "archived": False}


@app.get("/api/admin/orders")
async def admin_orders(status: str | None = None, user: dict = Depends(admin_user)):
    if status and status not in ("accepted", "assembling", "assembled", "in_transit", "completed", "rejected"):
        raise HTTPException(400, "Неизвестный статус заказа.")
    async with SessionLocal() as session:
        query = select(Order).order_by(Order.created_at.desc()).limit(150)
        if status:
            query = query.where(Order.status == status)
        rows = (await session.execute(query)).scalars().all()
        result = []
        for o in rows:
            buyer = await session.get(User, o.user_id)
            history = (await session.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == o.id).order_by(OrderStatusHistory.created_at, OrderStatusHistory.id))).scalars().all()
            result.append({"id": o.id, "order_number": f"DSS-{o.id:06d}", "user_id": o.user_id, "username": buyer.username if buyer else None, "total": float(o.total), "status": o.status, "status_history": [{"status": h.status, "changed_at": h.created_at.isoformat() if h.created_at else "", "note": h.note or ""} for h in history],
                "delivery_type": o.delivery_type, "delivery_name": o.delivery_name, "delivery_phone": o.delivery_phone, "delivery_address": o.delivery_address,
                "promo_codes": o.promo_codes, "created_at": o.created_at.isoformat() if o.created_at else "", "items": [
                    {"product_id": item.product_id, "brand": item.brand_snapshot or product.brand, "flavor": item.flavor_snapshot or product.flavor,
                     "price": float(item.price), "quantity": int(item.quantity or 1),
                     "cost_price": float(item.cost_price_snapshot) if item.cost_price_snapshot is not None else None}
                    for item, product in (await session.execute(select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id).where(OrderItem.order_id == o.id).order_by(OrderItem.id))).all()
                ]})
        return result


@app.post("/api/admin/orders/{order_id}/status")
async def admin_order_status(order_id: int, data: OrderStatusInput, user: dict = Depends(admin_user)):
    return await _set_order_status(order_id, data.status, user["id"])


@app.post("/api/admin/orders/{order_id}/approve")
async def admin_order_approve(order_id: int, user: dict = Depends(admin_user)):
    return await _process_order(order_id, True)


@app.post("/api/admin/orders/{order_id}/reject")
async def admin_order_reject(order_id: int, user: dict = Depends(admin_user)):
    return await _process_order(order_id, False)


@app.get("/api/admin/users")
async def admin_users(q: str | None = None, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        query = select(User).order_by(User.id.desc()).limit(200)
        if q:
            value = q.strip()
            if value.startswith("@"):
                query = query.where(User.username == value[1:])
            elif value.isdigit():
                query = query.where(User.id == int(value))
            else:
                query = query.where(User.username.ilike(f"%{value}%"))
        rows = (await session.execute(query)).scalars().all()
        result = []
        for u in rows:
            count = (await _club_metrics(session, u.id))["orders"]
            promos = (await session.execute(select(func.count(Promocode.code)).where(Promocode.owner_id == u.id, Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses))).scalar() or 0
            result.append({"id": u.id, "username": u.username, "balance": float(u.balance or 0), "orders_count": int(count), "orders_adjustment": int((await session.get(ClubAccount, u.id)).orders_adjustment or 0) if await session.get(ClubAccount, u.id) else 0, "referrals_count": int(u.referrals_count or 0), "active_promos": int(promos), "is_banned": bool(u.is_banned)})
        return result


@app.post("/api/admin/users/{user_id}/orders-adjustment")
async def admin_adjust_club_orders(user_id: int, data: dict, user: dict = Depends(admin_user)):
    delta = int(data.get("amount", 0))
    if not delta or abs(delta) > 100000:
        raise HTTPException(400, "Укажи изменение количества заказов от -100000 до 100000, кроме нуля.")
    async with SessionLocal() as session:
        if not await session.get(User, user_id):
            raise HTTPException(404, "Пользователь не найден.")
        account = await session.get(ClubAccount, user_id)
        if account is None:
            account = ClubAccount(user_id=user_id, balance=0, lifetime_earned=0, orders_adjustment=0)
            session.add(account)
            await session.flush()
        actual = int((await session.execute(select(func.count(Order.id)).where(Order.user_id == user_id, Order.status == "completed"))).scalar() or 0)
        next_adjustment = int(account.orders_adjustment or 0) + delta
        if actual + next_adjustment < 0:
            raise HTTPException(409, "Итоговое количество клубных заказов не может быть меньше нуля.")
        account.orders_adjustment = next_adjustment
        await session.commit()
    return {"ok": True, "orders_count": actual + next_adjustment, "orders_adjustment": next_adjustment}


@app.post("/api/admin/users/{user_id}/ban")
async def admin_user_ban(user_id: int, user: dict = Depends(admin_user)):
    if user_id == ADMIN_ID:
        raise HTTPException(400, "Нельзя заблокировать администратора.")
    async with SessionLocal() as session:
        u = await session.get(User, user_id)
        if not u:
            raise HTTPException(404, "Пользователь не найден.")
        u.is_banned = True
        await session.commit()
    await bot.send_message_safe(user_id, "❌ Доступ к магазину заблокирован администратором.")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unban")
async def admin_user_unban(user_id: int, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        u = await session.get(User, user_id)
        if not u:
            raise HTTPException(404, "Пользователь не найден.")
        u.is_banned = False
        await session.commit()
    await bot.send_message_safe(user_id, "✅ Доступ к магазину восстановлен.")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset")
async def admin_user_reset(user_id: int, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        u = await session.get(User, user_id)
        if not u:
            raise HTTPException(404, "Пользователь не найден.")
        u.balance = 0
        u.referrals_count = 0
        u.next_bonus_at = REFERRAL_MILESTONE
        u.first_purchase_done = False
        await session.execute(delete(Promocode).where(Promocode.owner_id == user_id))
        await session.execute(delete(CartPromo).where(CartPromo.user_id == user_id))
        await session.commit()
    return {"ok": True, "history_preserved": True}


@app.get("/api/admin/promos")
async def admin_promos(user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Promocode).order_by(Promocode.created_by, Promocode.code).limit(500))).scalars().all()
        return [{"code": p.code, "kind": p.kind, "amount": float(p.amount), "owner_id": p.owner_id, "used": bool(p.used), "used_count": p.used_count, "max_uses": p.max_uses, "created_by": p.created_by, "target_type": p.target_type or "all", "target_id": p.target_id, "min_order": float(p.min_order or 0), "starts_at": _promo_datetime_to_local(p.starts_at), "expires_at": _promo_datetime_to_local(p.expires_at), "active": _promo_is_active(p)} for p in rows]


@app.post("/api/admin/promos")
async def admin_promo_create(data: PromoInput, user: dict = Depends(admin_user)):
    if data.kind not in ("fixed", "percent") or (data.kind == "percent" and data.amount > 100):
        raise HTTPException(400, "Проверь тип и размер скидки.")
    if data.target_type not in ("all", "product", "category"):
        raise HTTPException(400, "Неизвестный тип ограничения промокода.")
    if data.target_type == "all" and data.target_id is not None:
        raise HTTPException(400, "Для промокода на весь каталог ID указывать не нужно.")
    if data.target_type != "all" and data.target_id is None:
        raise HTTPException(400, "Укажи ID товара или категории.")
    starts_at = _promo_datetime(data.starts_at)
    expires_at = _promo_datetime(data.expires_at)
    if starts_at and expires_at and expires_at <= starts_at:
        raise HTTPException(400, "Дата окончания должна быть позже даты начала.")
    code = (data.code or "").strip().upper()
    if code and not re.fullmatch(r"[A-Z0-9_-]{1,16}", code):
        raise HTTPException(400, "Код: латиница, цифры, дефис или подчёркивание, до 16 символов.")
    async with SessionLocal() as session:
        if data.target_type == "product" and not await session.get(Product, data.target_id):
            raise HTTPException(404, "Товар для ограничения не найден.")
        if data.target_type == "category" and not await session.get(Category, data.target_id):
            raise HTTPException(404, "Категория для ограничения не найдена.")
        if code and await session.get(Promocode, code):
            raise HTTPException(409, "Такой промокод уже существует.")
        if not code:
            code = await _create_promo(session, data.owner_id, data.kind, data.amount, data.max_uses, "admin")
            promo = await session.get(Promocode, code)
        else:
            promo = Promocode(code=code, owner_id=data.owner_id, kind=data.kind, amount=round(data.amount, 2), used=False, created_by="admin", max_uses=data.max_uses, used_count=0)
            session.add(promo)
        promo.target_type = data.target_type
        promo.target_id = data.target_id
        promo.min_order = round(data.min_order, 2)
        promo.starts_at = starts_at
        promo.expires_at = expires_at
        await session.commit()
    return {"ok": True, "code": code}


@app.delete("/api/admin/promos/{code}")
async def admin_promo_delete(code: str, user: dict = Depends(admin_user)):
    code = code.strip().upper()
    async with SessionLocal() as session:
        promo = await session.get(Promocode, code)
        if not promo:
            raise HTTPException(404, "Промокод не найден.")
        pending = (await session.execute(select(Order.id).where(Order.status.in_(("accepted", "assembling", "assembled", "in_transit")), Order.promo_codes.contains(code)).limit(1))).first()
        if pending:
            raise HTTPException(409, "Промокод привязан к ожидающему заказу.")
        await session.execute(delete(CartPromo).where(CartPromo.code == code))
        await session.delete(promo)
        await session.commit()
    return {"ok": True}


@app.get("/api/admin/stats")
async def admin_stats(period: str = "30d", user: dict = Depends(admin_user)):
    if period not in ("today", "yesterday", "7d", "30d", "all"):
        raise HTTPException(400, "Неизвестный период статистики.")
    now = datetime.utcnow()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "yesterday":
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
    elif period == "7d":
        start, end = now - timedelta(days=7), None
    elif period == "30d":
        start, end = now - timedelta(days=30), None
    else:
        start, end = None, None
    async with SessionLocal() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        pending = (await session.execute(select(func.count(Order.id)).where(Order.status.in_(("accepted", "assembling", "assembled", "in_transit"))))).scalar() or 0
        order_query = select(Order).where(Order.status == "completed")
        if start is not None: order_query = order_query.where(Order.created_at >= start)
        if end is not None: order_query = order_query.where(Order.created_at < end)
        orders_rows = (await session.execute(order_query.order_by(Order.created_at.desc()))).scalars().all()
        order_count = len(orders_rows)
        revenue = sum(max(0.0, float(o.total) - (DELIVERY_PRICE if o.delivery_type == "cdek" else 0.0)) for o in orders_rows)
        item_query = select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id).where(Order.status == "completed")
        if start is not None: item_query = item_query.where(Order.created_at >= start)
        if end is not None: item_query = item_query.where(Order.created_at < end)
        item_rows = (await session.execute(item_query)).all()
        cogs = 0.0
        tracked_units = untracked_units = 0
        products = {}
        order_item_gross = {}
        for item, order in item_rows:
            order_item_gross[order.id] = order_item_gross.get(order.id, 0.0) + float(item.price) * int(item.quantity or 1)
        for item, order in item_rows:
            qty = int(item.quantity or 1)
            if item.cost_price_snapshot is None: untracked_units += qty
            else:
                cogs += float(item.cost_price_snapshot) * qty
                tracked_units += qty
            gross_order_items = order_item_gross.get(order.id, 0.0)
            paid_goods = max(0.0, float(order.total) - (DELIVERY_PRICE if order.delivery_type == "cdek" else 0.0))
            allocated_revenue = (float(item.price) * qty * paid_goods / gross_order_items) if gross_order_items > 0 else 0.0
            entry = products.setdefault(item.product_id, {"brand": item.brand_snapshot or f"Товар #{item.product_id}", "flavor": item.flavor_snapshot or "", "quantity": 0, "revenue": 0.0, "cost": 0.0, "cost_tracked": True})
            entry["quantity"] += qty
            entry["revenue"] += allocated_revenue
            if item.cost_price_snapshot is None: entry["cost_tracked"] = False
            else: entry["cost"] += float(item.cost_price_snapshot) * qty
        top_products = sorted(products.values(), key=lambda x: x["quantity"], reverse=True)[:5]
        top_profit_products = sorted(({"brand": p["brand"], "flavor": p["flavor"], "profit": round(p["revenue"] - p["cost"], 2), "cost_tracked": p["cost_tracked"]} for p in products.values()), key=lambda x: x["profit"], reverse=True)[:5]
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        day_orders = (await session.execute(select(Order).where(Order.status == "completed", Order.created_at >= day_start))).scalars().all()
        week_orders = (await session.execute(select(Order).where(Order.status == "completed", Order.created_at >= week_start))).scalars().all()
        revenue_day = sum(max(0.0, float(o.total) - (DELIVERY_PRICE if o.delivery_type == "cdek" else 0.0)) for o in day_orders)
        revenue_week = sum(max(0.0, float(o.total) - (DELIVERY_PRICE if o.delivery_type == "cdek" else 0.0)) for o in week_orders)
    return {
        "period": period, "total_users": int(total_users), "total_orders": order_count,
        "pending_orders": int(pending), "total_revenue": round(revenue, 2),
        "total_cost": round(cogs, 2), "gross_profit": round(revenue - cogs, 2) if untracked_units == 0 else None,
        "revenue_day": round(revenue_day, 2), "revenue_week": round(revenue_week, 2),
        "avg_check": round(revenue / order_count, 2) if order_count else 0,
        "tracked_units": tracked_units, "untracked_units": untracked_units,
        "cost_coverage": round(tracked_units / (tracked_units + untracked_units) * 100, 1) if tracked_units + untracked_units else 100.0,
        "top_products": top_products, "top_profit_products": top_profit_products,
    }


async def _broadcast(text_to_send: str):
    async with SessionLocal() as session:
        ids = list((await session.execute(select(User.id).where(User.is_unreachable.is_(False)))).scalars().all())
    sent = failed = 0
    for uid in ids:
        if await bot.send_message_safe(uid, text_to_send):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.07)
    log.info("Broadcast finished sent=%s failed=%s total=%s", sent, failed, len(ids))


@app.post("/api/admin/broadcast")
async def admin_broadcast(data: BroadcastInput, tasks: BackgroundTasks, user: dict = Depends(admin_user)):
    if not data.text.strip():
        raise HTTPException(400, "Сообщение пустое.")
    tasks.add_task(_broadcast, data.text.strip())
    return {"ok": True, "message": "Рассылка поставлена в очередь."}


@app.get("/api/admin/reviews")
async def admin_reviews(user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Review).order_by(Review.published_at.desc()).limit(100))).scalars().all()
        return [{"id": r.id, "order_id": r.order_id, "user_id": r.user_id, "rating": r.rating, "text": r.text or "", "photo_url": r.photo_id if r.has_photo else None, "status": r.status or "pending", "published_at": r.published_at.isoformat() if r.published_at else ""} for r in rows]


@app.get("/uploads/reviews/{filename}")
async def review_image(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not re.fullmatch(r"[a-f0-9]{24}\.webp", safe_name):
        raise HTTPException(404, "Фотография не найдена.")
    target = REVIEW_UPLOAD_DIR / safe_name
    if not target.is_file():
        raise HTTPException(404, "Фотография не найдена.")
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    icon = STATIC_DIR / "favicon.svg"
    if not icon.is_file():
        raise HTTPException(404, "Favicon not found.")
    return FileResponse(icon, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
async def homepage():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        log.error("Frontend entrypoint is missing: %s", index)
        raise HTTPException(503, "Frontend files are missing from the deployed app.")
    return FileResponse(index, media_type="text/html")


@app.get("/{full_path:path}")
async def static_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404, "API route not found.")
    root = STATIC_DIR.resolve()
    file_path = (STATIC_DIR / full_path).resolve()
    if file_path != root and root in file_path.parents and file_path.is_file():
        return FileResponse(file_path)
    if Path(full_path).suffix:
        raise HTTPException(404, "Static asset not found.")
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    raise HTTPException(503, "Frontend build not found.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.getenv("PORT", "3000")), reload=False)
