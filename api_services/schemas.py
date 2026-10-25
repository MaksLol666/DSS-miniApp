from pydantic import BaseModel
from typing import Optional, List


class AuthResponse(BaseModel):
    user_id: int
    username: Optional[str] = None
    is_admin: bool
    is_banned: bool


class ProductOut(BaseModel):
    id: int
    category_id: int
    number: int
    brand: str
    flavor: str
    price: float
    in_stock: bool

    class Config:
        from_attributes = True


class CategoryOut(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


class CartItemOut(BaseModel):
    product_id: int
    brand: str
    flavor: str
    price: float


class CartOut(BaseModel):
    items: List[CartItemOut]
    subtotal: float
    percent_discount: float
    fixed_discount: float
    total: float


class CartAddRequest(BaseModel):
    product_id: int


class PromoApplyRequest(BaseModel):
    code: str


class PromoOut(BaseModel):
    code: str
    kind: str
    amount: float


class CheckoutRequest(BaseModel):
    delivery_type: str  # "pickup" | "cdek"
    delivery_name: Optional[str] = None
    delivery_phone: Optional[str] = None
    delivery_address: Optional[str] = None


class OrderOut(BaseModel):
    id: int
    total: float
    status: str
    delivery_type: str
    delivery_name: Optional[str] = None
    delivery_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    created_at: str
    items: List[CartItemOut] = []


class ProfileOut(BaseModel):
    user_id: int
    username: Optional[str] = None
    balance: float
    orders_count: int
    referrals_count: int
    ref_link: str
    active_promos: int


class TopUserOut(BaseModel):
    username: Optional[str] = None
    user_id: int
    value: float


class ReviewRequest(BaseModel):
    order_id: int
    rating: int
    text: str
    photo_url: Optional[str] = None


# ============ АДМИН ============

class AdminProductCreate(BaseModel):
    category_id: int
    brand: str
    flavor: str
    price: float


class AdminProductUpdate(BaseModel):
    brand: Optional[str] = None
    flavor: Optional[str] = None
    price: Optional[float] = None


class AdminOrderActionResponse(BaseModel):
    status: str
    order_id: int


class AdminUserOut(BaseModel):
    id: int
    username: Optional[str] = None
    balance: float
    referrals_count: int
    is_banned: bool


class AdminStats(BaseModel):
    total_users: int
    total_orders: int
    pending_orders: int
    total_revenue: float
    revenue_day: float
    revenue_week: float
    avg_check: float
    top_products: List[dict]
