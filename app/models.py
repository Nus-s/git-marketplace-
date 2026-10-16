from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, Boolean,
    ForeignKey, DateTime, Index, CheckConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class User(Base):
    """Сущность Покупатель (User) — связь 1:N с Заказами (Order)."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    phone = Column(String(32), nullable=False, unique=True, index=True)
    email = Column(String(150), nullable=False, unique=True)
    default_address = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    orders = relationship("Order", back_populates="buyer")


class Seller(Base):
    """Сущность Продавец / Селлер (Seller) — связь 1:N с Товарами (Product)."""
    __tablename__ = "sellers"

    id = Column(Integer, primary_key=True, index=True)
    shop_name = Column(String(200), nullable=False)
    inn = Column(String(20), nullable=False, unique=True)
    rating = Column(Numeric(3, 2), nullable=False, default=5.00)
    commission_pct = Column(Numeric(5, 2), nullable=False, default=12.00)
    payout_account = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    products = relationship("Product", back_populates="seller")


class Warehouse(Base):
    """Сущность Склад фулфилмента (Warehouse) — связи 1:N с Order и 1:N с WarehouseStock."""
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    region_code = Column(String(32), nullable=False, index=True)
    lat = Column(Numeric(9, 6), nullable=False)
    lon = Column(Numeric(9, 6), nullable=False)
    warehouse_type = Column(String(32), nullable=False, default="FULFILLMENT")
    is_active = Column(Boolean, nullable=False, default=True)

    stocks = relationship("WarehouseStock", back_populates="warehouse")
    orders = relationship("Order", back_populates="warehouse")


class Courier(Base):
    """Сущность Курьер доставки (Courier) — связь 1:N с Заказами (Order)."""
    __tablename__ = "couriers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    phone = Column(String(32), nullable=False, unique=True)
    vehicle_type = Column(String(32), nullable=False, default="CAR")
    rating = Column(Numeric(3, 2), nullable=False, default=4.95)
    current_zone = Column(String(64), nullable=False)
    shift_status = Column(String(32), nullable=False, default="ONLINE")
    current_lat = Column(Numeric(9, 6), default=55.755800)
    current_lon = Column(Numeric(9, 6), default=37.617300)
    updated_at = Column(DateTime(timezone=True), default=utc_now)

    orders = relationship("Order", back_populates="courier")


class Product(Base):
    """Сущность Товар / SKU (Product) — принадлежит Продавцу (Seller)."""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    seller_id = Column(Integer, ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(100), nullable=False, index=True)
    brand = Column(String(100), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    base_price = Column(Numeric(12, 2), nullable=False)
    promo_price = Column(Numeric(12, 2), nullable=True)
    is_flash_sale = Column(Boolean, nullable=False, default=False)
    flash_sale_limit = Column(Integer, nullable=False, default=0)
    weight_g = Column(Integer, nullable=False, default=500)
    rating = Column(Numeric(3, 2), nullable=False, default=4.80)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    seller = relationship("Seller", back_populates="products")
    stocks = relationship("WarehouseStock", back_populates="product", cascade="all, delete-orphan")


class WarehouseStock(Base):
    """
    Промежуточная сущность Складской остаток (WarehouseStock).
    Развязывает связь N:M между Складом (Warehouse) и Товаром (Product).
    Составной первичный ключ: (warehouse_id, product_id).
    """
    __tablename__ = "warehouse_stocks"

    warehouse_id = Column(Integer, ForeignKey("warehouses.id", ondelete="CASCADE"), primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True, index=True)
    bin_location = Column(String(64), nullable=False, default="A-01-01")
    available_qty = Column(Integer, nullable=False, default=0)
    reserved_qty = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("available_qty >= 0", name="ck_available_qty_non_negative"),
        CheckConstraint("reserved_qty >= 0", name="ck_reserved_qty_non_negative"),
    )

    warehouse = relationship("Warehouse", back_populates="stocks")
    product = relationship("Product", back_populates="stocks")


class Order(Base):
    """
    ЦЕНТРАЛЬНАЯ СУЩНОСТЬ: Заказ (Order).
    Связывает Покупателя (buyer_id), Склад фулфилмента (warehouse_id),
    Курьера (courier_id), Позиции заказа (OrderItem) и Платежи (Payment).
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    courier_id = Column(Integer, ForeignKey("couriers.id"), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="RESERVED")
    total_amount = Column(Numeric(12, 2), nullable=False)
    delivery_address = Column(Text, nullable=False)
    delivery_slot = Column(String(64), nullable=False, default="18:00-20:00")
    verification_code = Column(String(8), nullable=False, default="4821")
    reserve_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    buyer = relationship("User", back_populates="orders")
    warehouse = relationship("Warehouse", back_populates="orders")
    courier = relationship("Courier", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="order", cascade="all, delete-orphan")


Index("idx_orders_buyer_created", Order.buyer_id, Order.created_at)
Index("idx_orders_warehouse_status", Order.warehouse_id, Order.status)


class OrderItem(Base):
    """
    Промежуточная сущность Позиция заказа (OrderItem).
    Развязывает связь N:M между Заказом (Order) и Товаром (Product).
    Фиксирует историческую цену покупки (price_at_purchase).
    """
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    seller_id = Column(Integer, ForeignKey("sellers.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    price_at_purchase = Column(Numeric(12, 2), nullable=False)
    discount_amount = Column(Numeric(12, 2), nullable=False, default=0.00)
    item_status = Column(String(32), nullable=False, default="RESERVED")

    order = relationship("Order", back_populates="items")
    product = relationship("Product")


class Payment(Base):
    """
    Сущность Платёж (Payment) — связь N:1 к Заказу (Order).
    Содержит уникальный ключ идемпотентности (idempotency_key) для защиты от двойных списаний.
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    provider = Column(String(64), nullable=False, default="MOCK_ACQUIRING_BANK")
    status = Column(String(32), nullable=False, default="SUCCEEDED")
    receipt_url = Column(String(255), nullable=True)
    paid_at = Column(DateTime(timezone=True), default=utc_now)

    order = relationship("Order", back_populates="payments")


class CartItem(Base):
    """Корзина покупателя (ФТ-3: Управление корзиной)."""
    __tablename__ = "cart_items"

    id = Column(Integer, primary_key=True, index=True)
    buyer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    promo_code = Column(String(50), nullable=True)


class Review(Base):
    """Отзыв покупателя о доставленном товаре (ФТ-6)."""
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class OutboxEvent(Base):
    """
    Таблица Transactional Outbox (Этап 4 и Этап 7 первой части).
    Обеспечивает гарантированную доставку событий (order.created, stock.reserved, payment.succeeded)
    в асинхронную шину событий без потери сообщений при сбое сети.
    """
    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True, index=True)
    topic = Column(String(100), nullable=False, index=True)
    aggregate_id = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    processed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)
