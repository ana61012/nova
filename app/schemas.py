from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProductOut(BaseModel):
    id: int
    name: str
    price_cents: int
    image_emoji: str
    total: int
    available: int


class DropOut(BaseModel):
    id: int
    name: str
    starts_at: datetime
    products: list[ProductOut]


class ReserveIn(BaseModel):
    product_id: int
    qty: int = Field(1, ge=1)


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: int
    user_id: str
    qty: int
    status: str
    expires_at: datetime


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    token: str
    user_id: str
    email: str


class OrderIn(BaseModel):
    reservation_id: str


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reservation_id: str
    product_id: int
    qty: int
    amount_cents: int
    status: str
    payment_intent_id: str
