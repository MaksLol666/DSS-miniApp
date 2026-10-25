from fastapi import APIRouter, Depends
from sqlalchemy import select
from database.db import SessionLocal
from database.models import Category, Product
from api_services.auth import get_current_user
from api_services.schemas import CategoryOut, ProductOut

router = APIRouter(prefix="/api", tags=["products"])


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(user=Depends(get_current_user)):
    async with SessionLocal() as s:
        cats = (await s.execute(select(Category).order_by(Category.id))).scalars().all()
        return [CategoryOut.model_validate(c) for c in cats]


@router.get("/products", response_model=list[ProductOut])
async def list_products(category_id: int | None = None, user=Depends(get_current_user)):
    async with SessionLocal() as s:
        q = select(Product).where(Product.in_stock == True)
        if category_id is not None:
            q = q.where(Product.category_id == category_id)
        q = q.order_by(Product.number)
        products = (await s.execute(q)).scalars().all()
        return [ProductOut.model_validate(p) for p in products]
