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
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

import bot
from config import (
    ADMIN_ID, BOT_TOKEN, CATEGORIES, DELIVERY_PRICE, MAX_PROMOS_PER_ORDER,
    REFERRAL_BONUS_PERCENT, REFERRAL_MILESTONE, REFERRAL_MILESTONE_DISCOUNT,
)
from database.db import SessionLocal, engine, init_db
from database.models import CartItem, CartPromo, Category, Order, OrderItem, Product, Promocode, Review, User

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("dss.api")
STATIC_DIR = Path(__file__).resolve().parent / "static"
SITE_KEY = os.getenv("DDOS_GUARD_SITE_KEY", "").strip()
PRIVATE_KEY = os.getenv("DDOS_GUARD_SECRET_KEY", "").strip()
VERIFY_URL = os.getenv("DDOS_GUARD_VERIFY_URL", "https://captcha.ddos-guard.net/siteverify").strip()
SESSION_SECRET = os.getenv("DDOS_GUARD_SESSION_SECRET", "").strip()
SESSION_TTL = max(3600, int(os.getenv("DDOS_GUARD_SESSION_TTL_SECONDS", "2592000")))
COOKIE_NAME = "ddg_captcha_session"
INIT_DATA_MAX_AGE = max(60, int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "86400")))
RATE_WINDOW = max(5, int(os.getenv("SECURITY_RATE_WINDOW_SECONDS", "30")))
RATE_MAX = max(10, int(os.getenv("SECURITY_RATE_MAX_REQUESTS", "45")))
SUSPICIOUS_ADDS = max(5, int(os.getenv("DDOS_GUARD_SUSPICIOUS_CART_ADDS", "8")))
SUSPICIOUS_WINDOW = max(5, int(os.getenv("DDOS_GUARD_SUSPICIOUS_WINDOW_SECONDS", "20")))
_requests: dict[str, deque] = defaultdict(deque)
_cart_adds: dict[str, deque] = defaultdict(deque)
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


class ProductPatch(BaseModel):
    category_id: int | None = Field(default=None, gt=0)
    brand: str | None = Field(default=None, min_length=1, max_length=64)
    flavor: str | None = Field(default=None, min_length=1, max_length=64)
    price: float | None = Field(default=None, gt=0, le=10000000)
    in_stock: bool | None = None


class ProductId(BaseModel):
    product_id: int = Field(gt=0)


class PromoInput(BaseModel):
    code: str | None = Field(default=None, max_length=16)
    kind: str
    amount: float = Field(gt=0)
    owner_id: int = Field(default=0, ge=0)
    max_uses: int = Field(default=1, ge=1, le=1000000)


class CheckoutInput(BaseModel):
    delivery_type: str
    delivery_name: str | None = Field(default=None, max_length=128)
    delivery_phone: str | None = Field(default=None, max_length=32)
    delivery_address: str | None = Field(default=None, max_length=512)


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
        ("users", "is_banned", "INTEGER DEFAULT 0"),
        ("users", "is_unreachable", "INTEGER DEFAULT 0"),
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


def _totals(items: list[dict], promos: list[Promocode]) -> dict:
    subtotal = round(sum(float(i["price"]) for i in items), 2)
    percent = min(100.0, sum(float(p.amount) for p in promos if p.kind == "percent"))
    fixed = sum(float(p.amount) for p in promos if p.kind == "fixed")
    return {"subtotal": subtotal, "percent_discount": round(percent, 2), "fixed_discount": round(fixed, 2), "total": round(max(subtotal * (1 - percent / 100) - fixed, 0), 2)}


async def _cart(uid: int) -> dict:
    async with SessionLocal() as session:
        rows = (await session.execute(select(CartItem, Product).join(Product, CartItem.product_id == Product.id).where(CartItem.user_id == uid).order_by(CartItem.id))).all()
        items = [{"product_id": p.id, "brand": p.brand, "flavor": p.flavor, "price": float(p.price)} for _, p in rows]
        codes = await _promo_codes(session, uid)
        promos = list((await session.execute(select(Promocode).where(Promocode.code.in_(codes)))).scalars().all()) if codes else []
        return {"items": items, **_totals(items, promos), "promos": [{"code": p.code, "kind": p.kind, "amount": float(p.amount)} for p in promos]}


@app.get("/api/categories")
async def categories(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
        return [{"id": c.id, "name": c.name} for c in rows]


@app.get("/api/products")
async def products(category_id: int | None = None, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        query = select(Product).where(Product.in_stock.is_(True))
        if category_id is not None:
            query = query.where(Product.category_id == category_id)
        rows = (await session.execute(query.order_by(Product.category_id, Product.number))).scalars().all()
        return [{"id": p.id, "category_id": p.category_id, "number": p.number, "brand": p.brand, "flavor": p.flavor, "price": float(p.price), "in_stock": bool(p.in_stock)} for p in rows]


@app.get("/api/cart")
async def get_cart(user: dict = Depends(current_user)):
    return await _cart(user["id"])


@app.post("/api/cart/add")
async def cart_add(data: ProductId, user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        p = await session.get(Product, data.product_id)
        if not p or not p.in_stock:
            raise HTTPException(409, "Товар больше недоступен.")
        existing = (await session.execute(select(CartItem.id).where(CartItem.user_id == user["id"], CartItem.product_id == p.id))).first()
        if not existing:
            session.add(CartItem(user_id=user["id"], product_id=p.id, reserved=False))
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


@app.post("/api/promo/apply")
async def apply_promo(data: dict, user: dict = Depends(current_user)):
    code = str(data.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,16}", code):
        raise HTTPException(400, "Проверь написание промокода.")
    async with SessionLocal() as session:
        promo = await session.get(Promocode, code)
        if not promo or promo.used or promo.used_count >= promo.max_uses:
            raise HTTPException(404, "Промокод не найден или исчерпан.")
        if promo.owner_id not in (0, user["id"], ADMIN_ID):
            raise HTTPException(403, "Этот промокод выдан другому пользователю.")
        codes = await _promo_codes(session, user["id"])
        if code in codes:
            raise HTTPException(409, "Этот промокод уже применён.")
        if len(codes) >= MAX_PROMOS_PER_ORDER:
            raise HTTPException(409, "Можно применить не больше двух промокодов.")
        session.add(CartPromo(user_id=user["id"], code=code))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "Промокод уже применён.")
    return {"code": promo.code, "kind": promo.kind, "amount": float(promo.amount)}


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
        for _, p in rows:
            if not p.in_stock:
                raise HTTPException(409, f"Товар {p.brand} — {p.flavor} больше недоступен.")
        items = [{"product_id": p.id, "brand": p.brand, "flavor": p.flavor, "price": float(p.price)} for _, p in rows]
        codes = await _promo_codes(session, user["id"])
        promos = list((await session.execute(select(Promocode).where(Promocode.code.in_(codes)))).scalars().all()) if codes else []
        if len(codes) != len(promos) or any(p.used or p.used_count >= p.max_uses for p in promos):
            raise HTTPException(409, "Один из промокодов больше недоступен.")
        totals = _totals(items, promos)
        delivery_price = DELIVERY_PRICE if data.delivery_type == "cdek" else 0
        final = round(totals["total"] + delivery_price, 2)
        order = Order(user_id=user["id"], total=final, status="pending", promo_codes=",".join(codes) if codes else None,
            delivery_type=data.delivery_type, delivery_name=data.delivery_name.strip() if data.delivery_name else None,
            delivery_phone=data.delivery_phone.strip() if data.delivery_phone else None,
            delivery_address=data.delivery_address.strip() if data.delivery_address else None)
        session.add(order)
        await session.flush()
        for _, p in rows:
            session.add(OrderItem(order_id=order.id, product_id=p.id, price=float(p.price)))
            p.in_stock = False
        await session.execute(delete(CartItem).where(CartItem.user_id == user["id"]))
        await session.execute(delete(CartPromo).where(CartPromo.user_id == user["id"]))
        await session.commit()
        await session.refresh(order)
        record = await session.get(User, user["id"])
        username = record.username if record else user.get("username")
    item_text = "\n".join(f"• {item['brand']} — {item['flavor']} — {int(item['price'])} ₽" for item in items)
    receive = "Самовывоз" if data.delivery_type == "pickup" else f"СДЭК: {data.delivery_name}, {data.delivery_phone}, {data.delivery_address} (+{DELIVERY_PRICE} ₽)"
    notice = f"🆕 Новый заказ №{order.id}\nПользователь: {username or user['id']} (ID {user['id']})\n\n{item_text}\n\nСумма товаров: {int(totals['subtotal'])} ₽\nСкидка: {totals['percent_discount']}% и {int(totals['fixed_discount'])} ₽\nПолучение: {receive}\nПромокоды: {', '.join(codes) if codes else 'нет'}\nК оплате: {int(final)} ₽\n\nОткрой /admin в боте, чтобы обработать заказ."
    await bot.notify_admin(notice)
    return {"id": order.id, "total": float(order.total), "status": order.status, "delivery_type": order.delivery_type,
        "delivery_name": order.delivery_name, "delivery_phone": order.delivery_phone, "delivery_address": order.delivery_address,
        "created_at": order.created_at.isoformat() if order.created_at else "", "items": items, "delivery_price": delivery_price}


async def _order_items(session, order_id: int) -> list[dict]:
    rows = (await session.execute(select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id).where(OrderItem.order_id == order_id).order_by(OrderItem.id))).all()
    return [{"product_id": p.id, "brand": p.brand, "flavor": p.flavor, "price": float(item.price)} for item, p in rows]


@app.get("/api/orders")
async def orders(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Order).where(Order.user_id == user["id"]).order_by(Order.created_at.desc()).limit(50))).scalars().all()
        reviewed = set((await session.execute(select(Review.order_id).where(Review.user_id == user["id"]))).scalars().all())
        result = []
        for o in rows:
            result.append({"id": o.id, "total": float(o.total), "status": o.status, "delivery_type": o.delivery_type,
                "delivery_name": o.delivery_name, "delivery_phone": o.delivery_phone, "delivery_address": o.delivery_address,
                "created_at": o.created_at.isoformat() if o.created_at else "", "items": await _order_items(session, o.id),
                "has_review": o.id in reviewed})
        return result


@app.get("/api/profile")
async def profile(user: dict = Depends(current_user)):
    uid = user["id"]
    async with SessionLocal() as session:
        record = await session.get(User, uid)
        spent = (await session.execute(select(func.count(Order.id)).where(Order.user_id == uid, Order.status == "approved"))).scalar() or 0
        promos = (await session.execute(select(func.count(Promocode.code)).where(Promocode.owner_id == uid, Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses))).scalar() or 0
    ref_link = f"https://t.me/{bot.BOT_USERNAME}?start=ref_{uid}" if bot.BOT_USERNAME else ""
    return {"user_id": uid, "username": record.username if record else user.get("username"),
        "balance": float(record.balance or 0) if record else 0, "orders_count": int(spent),
        "referrals_count": int(record.referrals_count or 0) if record else 0,
        "next_bonus_at": int(record.next_bonus_at or REFERRAL_MILESTONE) if record else REFERRAL_MILESTONE,
        "ref_link": ref_link, "active_promos": int(promos), "is_admin": uid == ADMIN_ID}


@app.get("/api/promos")
async def my_promos(user: dict = Depends(current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Promocode).where(Promocode.owner_id == user["id"], Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses).order_by(Promocode.code))).scalars().all()
        return [{"code": p.code, "kind": p.kind, "amount": float(p.amount), "used_count": p.used_count, "max_uses": p.max_uses} for p in rows]


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
async def add_review(data: ReviewInput, user: dict = Depends(current_user)):
    uid = user["id"]
    async with SessionLocal() as session:
        order = await session.get(Order, data.order_id)
        if not order or order.user_id != uid or order.status != "approved":
            raise HTTPException(400, "Отзыв можно оставить только к своему подтверждённому заказу.")
        exists = (await session.execute(select(Review.id).where(Review.order_id == data.order_id))).first()
        if exists:
            raise HTTPException(409, "Для этого заказа отзыв уже оставлен.")
        account = await session.get(User, uid)
        username = account.username if account else user.get("username")
        session.add(Review(user_id=uid, order_id=data.order_id, rating=data.rating, text=data.text.strip(), has_photo=False))
        await session.commit()
    published = await bot.publish_review(f"⭐ Отзыв на заказ №{data.order_id}\nПользователь: {username or uid} (ID {uid})\nОценка: {'★' * data.rating}{'☆' * (5-data.rating)}\n\n{data.text.strip()}")
    return {"ok": True, "published": published}


async def _create_promo(session, owner_id: int, kind: str, amount: float, max_uses: int = 1, created_by: str = "ref") -> str:
    for _ in range(20):
        code = secrets.token_hex(4).upper()
        if not await session.get(Promocode, code):
            session.add(Promocode(code=code, owner_id=owner_id, kind=kind, amount=round(amount, 2), used=False, created_by=created_by, max_uses=max_uses, used_count=0))
            await session.flush()
            return code
    raise RuntimeError("Не удалось сгенерировать уникальный промокод.")


async def _process_order(order_id: int, approve: bool) -> dict:
    notifications = []
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order:
            raise HTTPException(404, "Заказ не найден.")
        if order.status != "pending":
            raise HTTPException(409, "Этот заказ уже обработан.")
        items = (await session.execute(select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id).where(OrderItem.order_id == order_id))).all()
        buyer = await session.get(User, order.user_id)
        if approve:
            order.status = "approved"
            if buyer:
                buyer.balance = float(buyer.balance or 0) + float(order.total)
                if buyer.referrer_id and not buyer.first_purchase_done:
                    ref_id = buyer.referrer_id
                    bonus = round(float(order.total) * REFERRAL_BONUS_PERCENT / 100, 2)
                    bonus_code = await _create_promo(session, ref_id, "fixed", bonus)
                    referrer = await session.get(User, ref_id)
                    milestone_code = None
                    if referrer:
                        referrer.referrals_count = int(referrer.referrals_count or 0) + 1
                        next_at = int(referrer.next_bonus_at or REFERRAL_MILESTONE)
                        if referrer.referrals_count >= next_at:
                            milestone_code = await _create_promo(session, ref_id, "percent", REFERRAL_MILESTONE_DISCOUNT)
                            referrer.next_bonus_at = next_at + REFERRAL_MILESTONE
                        notifications.append((ref_id, f"🎁 Твой приглашённый совершил первую покупку. Промокод на {int(bonus)} ₽: {bonus_code}"))
                        if milestone_code:
                            notifications.append((ref_id, f"🏆 Уже {referrer.referrals_count} купивших рефералов! Промокод на −{REFERRAL_MILESTONE_DISCOUNT}%: {milestone_code}"))
                buyer.first_purchase_done = True
            if order.promo_codes:
                for code in filter(None, order.promo_codes.split(",")):
                    promo = await session.get(Promocode, code)
                    if promo and promo.used_count < promo.max_uses:
                        promo.used_count += 1
                        promo.used = promo.used_count >= promo.max_uses
            if buyer:
                notifications.append((buyer.id, f"✅ Заказ №{order_id} подтверждён! Спасибо за покупку."))
        else:
            order.status = "rejected"
            for _, product in items:
                product.in_stock = True
            if buyer:
                notifications.append((buyer.id, f"❌ Заказ №{order_id} отклонён администратором."))
        await session.commit()
        status = order.status
    for user_id, message in notifications:
        await bot.send_message_safe(user_id, message)
    return {"status": status, "order_id": order_id}


@app.get("/api/admin/products")
async def admin_products(user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Product).order_by(Product.category_id, Product.number, Product.id))).scalars().all()
        return [{"id": p.id, "category_id": p.category_id, "number": p.number, "brand": p.brand, "flavor": p.flavor, "price": float(p.price), "in_stock": bool(p.in_stock)} for p in rows]


@app.post("/api/admin/products")
async def admin_product_create(data: ProductInput, user: dict = Depends(admin_user)):
    async with SessionLocal() as session:
        if not await session.get(Category, data.category_id):
            raise HTTPException(404, "Категория не найдена.")
        n = (await session.execute(select(func.max(Product.number)).where(Product.category_id == data.category_id))).scalar() or 0
        product = Product(category_id=data.category_id, number=n + 1, brand=data.brand.strip(), flavor=data.flavor.strip(), price=data.price, in_stock=True)
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return {"id": product.id, "number": product.number}


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
            if value is not None:
                setattr(p, key, value.strip() if key in ("brand", "flavor") else value)
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
            await session.commit()
            return {"ok": True, "archived": True}
        await session.execute(delete(CartItem).where(CartItem.product_id == product_id))
        await session.delete(p)
        await session.commit()
        return {"ok": True, "archived": False}


@app.get("/api/admin/orders")
async def admin_orders(status: str | None = None, user: dict = Depends(admin_user)):
    if status and status not in ("pending", "approved", "rejected"):
        raise HTTPException(400, "Неизвестный статус заказа.")
    async with SessionLocal() as session:
        query = select(Order).order_by(Order.created_at.desc()).limit(150)
        if status:
            query = query.where(Order.status == status)
        rows = (await session.execute(query)).scalars().all()
        result = []
        for o in rows:
            buyer = await session.get(User, o.user_id)
            result.append({"id": o.id, "user_id": o.user_id, "username": buyer.username if buyer else None, "total": float(o.total), "status": o.status,
                "delivery_type": o.delivery_type, "delivery_name": o.delivery_name, "delivery_phone": o.delivery_phone, "delivery_address": o.delivery_address,
                "promo_codes": o.promo_codes, "created_at": o.created_at.isoformat() if o.created_at else "", "items": await _order_items(session, o.id)})
        return result


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
            count = (await session.execute(select(func.count(Order.id)).where(Order.user_id == u.id, Order.status == "approved"))).scalar() or 0
            promos = (await session.execute(select(func.count(Promocode.code)).where(Promocode.owner_id == u.id, Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses))).scalar() or 0
            result.append({"id": u.id, "username": u.username, "balance": float(u.balance or 0), "orders_count": int(count), "referrals_count": int(u.referrals_count or 0), "active_promos": int(promos), "is_banned": bool(u.is_banned)})
        return result


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
        return [{"code": p.code, "kind": p.kind, "amount": float(p.amount), "owner_id": p.owner_id, "used": bool(p.used), "used_count": p.used_count, "max_uses": p.max_uses, "created_by": p.created_by} for p in rows]


@app.post("/api/admin/promos")
async def admin_promo_create(data: PromoInput, user: dict = Depends(admin_user)):
    if data.kind not in ("fixed", "percent") or (data.kind == "percent" and data.amount > 100):
        raise HTTPException(400, "Проверь тип и размер скидки.")
    code = (data.code or "").strip().upper()
    if code and not re.fullmatch(r"[A-Z0-9_-]{1,16}", code):
        raise HTTPException(400, "Код: латиница, цифры, дефис или подчёркивание, до 16 символов.")
    async with SessionLocal() as session:
        if code and await session.get(Promocode, code):
            raise HTTPException(409, "Такой промокод уже существует.")
        if not code:
            code = await _create_promo(session, data.owner_id, data.kind, data.amount, data.max_uses, "admin")
        else:
            session.add(Promocode(code=code, owner_id=data.owner_id, kind=data.kind, amount=round(data.amount, 2), used=False, created_by="admin", max_uses=data.max_uses, used_count=0))
        await session.commit()
    return {"ok": True, "code": code}


@app.delete("/api/admin/promos/{code}")
async def admin_promo_delete(code: str, user: dict = Depends(admin_user)):
    code = code.strip().upper()
    async with SessionLocal() as session:
        promo = await session.get(Promocode, code)
        if not promo:
            raise HTTPException(404, "Промокод не найден.")
        pending = (await session.execute(select(Order.id).where(Order.status == "pending", Order.promo_codes.contains(code)).limit(1))).first()
        if pending:
            raise HTTPException(409, "Промокод привязан к ожидающему заказу.")
        await session.execute(delete(CartPromo).where(CartPromo.code == code))
        await session.delete(promo)
        await session.commit()
    return {"ok": True}


@app.get("/api/admin/stats")
async def admin_stats(user: dict = Depends(admin_user)):
    now = datetime.utcnow()
    async with SessionLocal() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        total_orders = (await session.execute(select(func.count(Order.id)).where(Order.status == "approved"))).scalar() or 0
        pending = (await session.execute(select(func.count(Order.id)).where(Order.status == "pending"))).scalar() or 0
        revenue = (await session.execute(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == "approved"))).scalar() or 0
        day = (await session.execute(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == "approved", Order.created_at >= now - timedelta(days=1)))).scalar() or 0
        week = (await session.execute(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == "approved", Order.created_at >= now - timedelta(days=7)))).scalar() or 0
        counts = (await session.execute(select(OrderItem.product_id, func.count(OrderItem.id).label("n")).group_by(OrderItem.product_id).order_by(func.count(OrderItem.id).desc()).limit(5))).all()
        top = []
        for product_id, count in counts:
            p = await session.get(Product, product_id)
            if p:
                top.append({"brand": p.brand, "flavor": p.flavor, "count": int(count)})
    return {"total_users": int(total_users), "total_orders": int(total_orders), "pending_orders": int(pending),
        "total_revenue": float(revenue), "revenue_day": float(day), "revenue_week": float(week),
        "avg_check": float(revenue) / total_orders if total_orders else 0, "top_products": top}


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
        return [{"id": r.id, "order_id": r.order_id, "user_id": r.user_id, "rating": r.rating, "text": r.text or "", "published_at": r.published_at.isoformat() if r.published_at else ""} for r in rows]


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
