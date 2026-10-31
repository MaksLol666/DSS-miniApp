from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Legacy name: cumulative spending on approved orders.
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    first_purchase_done: Mapped[bool] = mapped_column(Boolean, default=False)
    referrals_count: Mapped[int] = mapped_column(Integer, default=0)
    next_bonus_at: Mapped[int] = mapped_column(Integer, default=10)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_unreachable: Mapped[bool] = mapped_column(Boolean, default=False)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    number: Mapped[int] = mapped_column(Integer)
    brand: Mapped[str] = mapped_column(String(64))
    flavor: Mapped[str] = mapped_column(String(64))
    price: Mapped[float] = mapped_column(Float)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    stock_qty: Mapped[int] = mapped_column(Integer, default=1)


class CartItem(Base):
    __tablename__ = "cart_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    promo_codes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    delivery_type: Mapped[str] = mapped_column(String(16), default="pickup")
    delivery_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivery_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(String(512), nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class Promocode(Base):
    __tablename__ = "promocodes"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8), default="fixed")
    amount: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(8), default="system")
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)


class CartPromo(Base):
    __tablename__ = "cart_promos"
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_cart_promos_user_code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(ForeignKey("promocodes.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    photo_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    published_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
