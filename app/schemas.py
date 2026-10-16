from typing import List, Optional, Dict
from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    seller_id: int = Field(..., description="ID продавца (FK -> sellers.id)")
    category: str = Field(..., examples=["Смартфоны"])
    brand: str = Field(..., examples=["Apple"])
    title: str = Field(..., examples=["iPhone 16 Pro 256GB Titanium"])
    description: Optional[str] = Field("Флагманский смартфон с поддержкой 5G")
    base_price: float = Field(..., gt=0, examples=[119990.0])
    promo_price: Optional[float] = Field(None, gt=0, examples=[89990.0])
    is_flash_sale: bool = Field(False)
    flash_sale_limit: int = Field(0, ge=0)
    weight_g: int = Field(220, gt=0)


class FlashSaleConfigUpdate(BaseModel):
    base_price: float = Field(..., gt=0)
    promo_price: float = Field(..., gt=0)
    is_flash_sale: bool = True
    flash_sale_limit: int = Field(..., gt=0, description="Выделенный пул штук на распродажу (делится на 10 бакетов)")
    warehouse_id: int = Field(1, description="Склад фулфилмента, где резервируется сток")


class InboundSupplyRequest(BaseModel):
    seller_id: int
    warehouse_id: int
    product_id: int
    quantity: int = Field(..., gt=0)
    bin_location: str = Field("A-12-04", description="Адресная ячейка размещения на складе")


class CartAddRequest(BaseModel):
    buyer_id: int
    product_id: int
    quantity: int = Field(1, gt=0)
    promo_code: Optional[str] = None


class OrderItemRequest(BaseModel):
    product_id: int
    quantity: int = Field(1, gt=0)


class OrderCreateRequest(BaseModel):
    buyer_id: int
    warehouse_id: int = 1
    delivery_address: str = Field(..., examples=["г. Москва, ул. Тверская, д. 15, кв. 42"])
    delivery_slot: str = Field("18:00-20:00")
    items: List[OrderItemRequest]


class PaymentRequest(BaseModel):
    idempotency_key: str = Field(..., description="Уникальный UUID для защиты от двойного списания (NFR)")
    payment_method: str = Field("CARD", examples=["CARD", "SBP"])


class WmsReceiveRequest(BaseModel):
    warehouse_id: int
    product_id: int
    quantity: int = Field(..., gt=0)
    bin_location: str = Field(..., examples=["B-04-19"])


class CourierAssignRequest(BaseModel):
    courier_id: int


class CourierLocationUpdate(BaseModel):
    lat: float = Field(..., ge=-90, le=90, examples=[55.7584])
    lon: float = Field(..., ge=-180, le=180, examples=[37.6198])


class DeliveryCompleteRequest(BaseModel):
    courier_id: int
    verification_code: str = Field(..., description="Проверочный код вручения из приложения покупателя")


class ReviewCreateRequest(BaseModel):
    buyer_id: int
    product_id: int
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(..., examples=["Доставили за 2 часа, телефон оригинал!"])
