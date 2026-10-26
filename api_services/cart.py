from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete
from database.db import SessionLocal
from database.models import CartItem, Product, Promocode
from api_services.auth import get_current_user
from api_services.schemas import (
    CartOut, CartItemOut, CartAddRequest, PromoApplyRequest, PromoOut
)
from config import ADMIN_ID, MAX_PROMOS_PER_ORDER

router = APIRouter(prefix="/api", tags=["cart"])

# Простое in-memory хранилище применённых промо (переживёт между запросами, но не рестарт)
_applied_promos: dict[int, list[str]] = {}


def _get_applied(uid: int) -> list[str]:
    return _applied_promos.get(uid, [])


def _add_applied(uid: int, code: str) -> bool:
    lst = _applied_promos.setdefault(uid, [])
    if code in lst or len(lst) >= MAX_PROMOS_PER_ORDER:
        return False
    lst.append(code)
    return True


def _clear_applied(uid: int):
    _applied_promos.pop(uid, None)


async def _calc_discount(codes: list[str]):
    if not codes:
        return 0.0, 0.0
    async with SessionLocal() as s:
        promos = (await s.execute(
            select(Promocode).where(Promocode.code.in_(codes))
        )).scalars().all()
    percent, fixed = 0.0, 0.0
    for p in promos:
        if p.kind == "percent":
            percent += p.amount
        else:
            fixed += p.amount
    return min(percent, 100.0), fixed


async def _render_cart(uid: int) -> CartOut:
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(CartItem, Product).join(Product, CartItem.product_id == Product.id)
            .where(CartItem.user_id == uid)
        )).all()

    items = [
        CartItemOut(
            product_id=p.id, brand=p.brand, flavor=p.flavor, price=p.price
        ) for _, p in rows
    ]
    subtotal = sum(i.price for i in items)

    percent, fixed = await _calc_discount(_get_applied(uid))
    after_percent = subtotal * (1 - percent / 100)
    total = max(after_percent - fixed, 0)

    return CartOut(
        items=items, subtotal=subtotal,
        percent_discount=percent, fixed_discount=fixed,
        total=total,
    )


@router.get("/cart", response_model=CartOut)
async def get_cart(user=Depends(get_current_user)):
    return await _render_cart(user["id"])


@router.post("/cart/add", response_model=CartOut)
async def add_to_cart(req: CartAddRequest, user=Depends(get_current_user)):
    uid = user["id"]
    async with SessionLocal() as s:
        product = await s.get(Product, req.product_id)
        if not product or not product.in_stock:
            raise HTTPException(400, "Товар недоступен")

        existing = (await s.execute(
            select(CartItem).where(
                CartItem.user_id == uid,
                CartItem.product_id == req.product_id,
            )
        )).scalar_one_or_none()

        if existing:
            # Idempotent behavior: repeated taps do not create duplicates or
            # punish a normal user for a double-click.
            return await _render_cart(uid)

        s.add(CartItem(user_id=uid, product_id=req.product_id, reserved=False))
        await s.commit()

    return await _render_cart(uid)


@router.post("/cart/remove", response_model=CartOut)
async def remove_from_cart(req: CartAddRequest, user=Depends(get_current_user)):
    uid = user["id"]
    async with SessionLocal() as s:
        await s.execute(
            delete(CartItem).where(
                CartItem.user_id == uid,
                CartItem.product_id == req.product_id,
            )
        )
        await s.commit()
    return await _render_cart(uid)


@router.post("/cart/clear", response_model=CartOut)
async def clear_cart(user=Depends(get_current_user)):
    uid = user["id"]
    async with SessionLocal() as s:
        await s.execute(delete(CartItem).where(CartItem.user_id == uid))
        await s.commit()
    _clear_applied(uid)
    return await _render_cart(uid)


@router.post("/promo/apply", response_model=PromoOut)
async def apply_promo(req: PromoApplyRequest, user=Depends(get_current_user)):
    uid = user["id"]
    code = req.code.strip().upper()

    async with SessionLocal() as s:
        promo = await s.get(Promocode, code)
    if not promo:
        raise HTTPException(404, "Промокод не найден")
    if promo.used_count >= promo.max_uses:
        raise HTTPException(400, "Промокод исчерпан")
    if promo.owner_id not in (0, uid, ADMIN_ID):
        raise HTTPException(403, "Промокод не твой")

    if not _add_applied(uid, code):
        raise HTTPException(400, "Уже применён или лимит 2")

    return PromoOut(code=promo.code, kind=promo.kind, amount=promo.amount)


@router.get("/promo/applied")
async def get_applied_promos(user=Depends(get_current_user)):
    return {"codes": _get_applied(user["id"])}


def pop_applied(uid: int) -> list[str]:
    """Вызывается при checkout — забирает применённые промокоды."""
    return _applied_promos.pop(uid, [])


def reset_applied(uid: int):
    _applied_promos.pop(uid, None)
