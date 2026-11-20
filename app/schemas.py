from datetime import datetime

from pydantic import BaseModel, Field


class ProductOut(BaseModel):
    id: int
    name: str
    price_cents: int
    image_emoji: str
    total: int
    available: int


class LimitsOut(BaseModel):
    max_qty_per_product: int
    max_cart_lines: int


class DropOut(BaseModel):
    id: int
    name: str
    starts_at: datetime
    limits: LimitsOut
    products: list[ProductOut]


class CartLine(BaseModel):
    product_id: int
    qty: int = Field(ge=1, le=1000)  # the real per-customer cap is enforced server-side


class ReserveIn(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=50)


class LineOut(BaseModel):
    product_id: int
    qty: int


class ReservationOut(BaseModel):
    id: str
    user_id: str
    status: str
    expires_at: datetime
    items: list[LineOut]


class EmailIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class VerifyIn(EmailIn):
    code: str = Field(pattern=r"^\d{6}$")


class CodeSentOut(BaseModel):
    detail: str
    expires_in: int
    resend_after: int


class TokenOut(BaseModel):
    token: str
    user_id: str
    email: str


class OrderIn(BaseModel):
    reservation_id: str


class OrderItemOut(BaseModel):
    product_id: int
    qty: int
    unit_price_cents: int


class OrderOut(BaseModel):
    id: str
    reservation_id: str
    amount_cents: int
    status: str
    payment_intent_id: str
    items: list[OrderItemOut]