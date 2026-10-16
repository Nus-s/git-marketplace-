import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.flash_sale_engine import flash_engine
from app.models import (
    CartItem, Courier, Order, OrderItem, OutboxEvent,
    Payment, Product, Review, Seller, User, Warehouse, WarehouseStock
)
from app.schemas import (
    CartAddRequest, CourierAssignRequest, CourierLocationUpdate,
    DeliveryCompleteRequest, FlashSaleConfigUpdate, InboundSupplyRequest,
    OrderCreateRequest, PaymentRequest, ProductCreate, ReviewCreateRequest,
    WmsReceiveRequest
)
from app.seed import seed_initial_data

# Настройка структурированного логирования (NFR: Логирование запросов и ошибок)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("marketplace.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаём таблицы ER-диаграммы и заполняем БД стартовыми данными (seed)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_result = seed_initial_data(db)
        logger.info(f"Database initialized & seeded: {seed_result}")
    finally:
        db.close()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Рабочий backend-сервис маркетплейса с фулфилментом и механикой Flash Sales "
        "(реализация второй части проекта по системному дизайну: 4 роли пользователей, "
        "ФТ-1..ФТ-15, ER-диаграмма из 8 основных сущностей, шардированные бакеты резерва стока "
        "с TTL, идемпотентные платежи и паттерн Transactional Outbox / Saga)."
    ),
    lifespan=lifespan,
)

# Простой счётчик запросов для Rate Limiting (NFR: Защита от всплесков)
_rate_window = {"minute": int(time.time() // 60), "count": 0}


@app.middleware("http")
async def observability_and_rate_limit_middleware(request: Request, call_next):
    start_ts = time.perf_counter()
    current_min = int(time.time() // 60)
    if _rate_window["minute"] != current_min:
        _rate_window["minute"] = current_min
        _rate_window["count"] = 0
    _rate_window["count"] += 1

    if _rate_window["count"] > settings.RATE_LIMIT_PER_MINUTE:
        logger.warning(f"Rate limit exceeded for {request.url.path}")
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded (API Gateway protection)"})

    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
    response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
    logger.info(f"{request.method} {request.url.path} -> {response.status_code} ({elapsed_ms} ms)")
    return response


def emit_outbox_event(db: Session, topic: str, aggregate_id: str, payload: dict):
    """Записывает событие в таблицу Transactional Outbox в рамках текущей БД-транзакции."""
    evt = OutboxEvent(
        topic=topic,
        aggregate_id=str(aggregate_id),
        payload_json=json.dumps(payload, ensure_ascii=False),
        processed=False,
    )
    db.add(evt)


# =============================================================================
# СЛУЖЕБНЫЕ ЭНДПОИНТЫ (HEALTHCHECK, SEED, OUTBOX EVENTS)
# =============================================================================

@app.get("/health", tags=["0. System & Health"])
def healthcheck(db: Session = Depends(get_db)):
    expired_cleaned = flash_engine.cleanup_expired_reservations()
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "env": settings.APP_ENV,
        "expired_reservations_returned": expired_cleaned,
    }


@app.post("/api/v1/system/seed", tags=["0. System & Health"])
def trigger_seed(db: Session = Depends(get_db)):
    """Принудительный запуск скрипта наполнения тестовыми данными (seed)."""
    return seed_initial_data(db)


@app.get("/api/v1/system/outbox", tags=["0. System & Health"])
def list_outbox_events(limit: int = 25, db: Session = Depends(get_db)):
    """Просмотр очереди событий Transactional Outbox (асинхронная шина событий Kafka / Saga)."""
    events = db.query(OutboxEvent).order_by(OutboxEvent.id.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "topic": e.topic,
            "aggregate_id": e.aggregate_id,
            "payload": json.loads(e.payload_json),
            "created_at": e.created_at,
        }
        for e in events
    ]


# =============================================================================
# РОЛЬ 1: ПОКУПАТЕЛЬ / КЛИЕНТ (ФТ-1 .. ФТ-6)
# =============================================================================

@app.get("/api/v1/catalog/products", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def search_catalog(
    q: Optional[str] = Query(None, description="Поисковый запрос по названию/описанию"),
    category: Optional[str] = Query(None, description="Фильтр по категории"),
    brand: Optional[str] = Query(None, description="Фильтр по бренду"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    only_flash_sale: bool = Query(False, description="Показать только товары горячей распродажи"),
    db: Session = Depends(get_db),
):
    """
    ФТ-1: Покупатель ищет товары по ключевым словам, категории, бренду и диапазону цен.
    """
    query = db.query(Product)
    if q:
        pattern = f"%{q}%"
        query = query.filter((Product.title.ilike(pattern)) | (Product.description.ilike(pattern)))
    if category:
        query = query.filter(Product.category == category)
    if brand:
        query = query.filter(Product.brand == brand)
    if only_flash_sale:
        query = query.filter(Product.is_flash_sale == True)

    products = query.order_by(Product.id.asc()).all()
    result = []
    for p in products:
        effective_price = float(p.promo_price if p.promo_price is not None else p.base_price)
        if min_price is not None and effective_price < min_price:
            continue
        if max_price is not None and effective_price > max_price:
            continue
        total_available = sum(s.available_qty for s in p.stocks)
        result.append({
            "id": p.id,
            "seller_id": p.seller_id,
            "category": p.category,
            "brand": p.brand,
            "title": p.title,
            "base_price": float(p.base_price),
            "promo_price": float(p.promo_price) if p.promo_price is not None else None,
            "effective_price": effective_price,
            "is_flash_sale": p.is_flash_sale,
            "rating": float(p.rating),
            "total_available_qty": total_available,
        })
    return {"count": len(result), "items": result}


@app.get("/api/v1/catalog/products/{product_id}", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def get_product_details(product_id: int, db: Session = Depends(get_db)):
    """
    ФТ-2: Покупатель просматривает карточку товара с актуальной ценой, остатками по складам и отзывами.
    """
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    reviews = db.query(Review).filter(Review.product_id == product_id).order_by(Review.id.desc()).all()
    warehouses_stock = [
        {
            "warehouse_id": s.warehouse_id,
            "warehouse_name": s.warehouse.name if s.warehouse else None,
            "bin_location": s.bin_location,
            "available_qty": s.available_qty,
            "reserved_qty": s.reserved_qty,
        }
        for s in p.stocks
    ]
    buckets = flash_engine.get_buckets_snapshot(product_id) if p.is_flash_sale else {}

    return {
        "id": p.id,
        "seller_id": p.seller_id,
        "seller_name": p.seller.shop_name if p.seller else None,
        "category": p.category,
        "brand": p.brand,
        "title": p.title,
        "description": p.description,
        "base_price": float(p.base_price),
        "promo_price": float(p.promo_price) if p.promo_price is not None else None,
        "effective_price": float(p.promo_price if p.promo_price is not None else p.base_price),
        "is_flash_sale": p.is_flash_sale,
        "flash_sale_buckets": buckets,
        "weight_g": p.weight_g,
        "rating": float(p.rating),
        "warehouses_stock": warehouses_stock,
        "reviews": [
            {"id": r.id, "buyer_id": r.buyer_id, "rating": r.rating, "comment": r.comment}
            for r in reviews
        ],
    }


@app.post("/api/v1/cart/items", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def add_to_cart(req: CartAddRequest, db: Session = Depends(get_db)):
    """
    ФТ-3: Покупатель добавляет товары в корзину и применяет промокод.
    """
    product = db.query(Product).filter(Product.id == req.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    existing = (
        db.query(CartItem)
        .filter(CartItem.buyer_id == req.buyer_id, CartItem.product_id == req.product_id)
        .first()
    )
    if existing:
        existing.quantity += req.quantity
        existing.promo_code = req.promo_code or existing.promo_code
    else:
        existing = CartItem(
            buyer_id=req.buyer_id,
            product_id=req.product_id,
            quantity=req.quantity,
            promo_code=req.promo_code,
        )
        db.add(existing)
    db.commit()
    return get_cart(req.buyer_id, db)


@app.get("/api/v1/cart/{buyer_id}", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def get_cart(buyer_id: int, db: Session = Depends(get_db)):
    """
    ФТ-3: Просмотр корзины с расчётом итоговой стоимости и доступных слотов доставки.
    """
    items = db.query(CartItem).filter(CartItem.buyer_id == buyer_id).all()
    detailed = []
    subtotal = 0.0
    for ci in items:
        prod = db.query(Product).filter(Product.id == ci.product_id).first()
        if not prod:
            continue
        unit_price = float(prod.promo_price if prod.promo_price is not None else prod.base_price)
        discount = round(unit_price * 0.05, 2) if ci.promo_code == "SALE5" else 0.0
        line_total = round((unit_price - discount) * ci.quantity, 2)
        subtotal += line_total
        detailed.append({
            "product_id": prod.id,
            "title": prod.title,
            "quantity": ci.quantity,
            "unit_price": unit_price,
            "discount_per_unit": discount,
            "line_total": line_total,
            "promo_code": ci.promo_code,
        })
    return {
        "buyer_id": buyer_id,
        "items": detailed,
        "total_amount": round(subtotal, 2),
        "available_delivery_slots": ["10:00-14:00", "14:00-18:00", "18:00-22:00"],
    }


@app.post("/api/v1/orders", status_code=201, tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def create_order_and_reserve_stock(req: OrderCreateRequest, db: Session = Depends(get_db)):
    """
    ФТ-4 + Узел Этапа 5: Покупатель оформляет заказ.
    • Для акционных товаров (Flash Sale) выполняется атомарный декремент в одном из 10 шардированных
      бакетов (sku:{id}:bucket_{0..9}) с мягким резервом на TTL = 10 минут.
    • Для всех товаров увеличивается reserved_qty и уменьшается available_qty на складе фулфилмента
      со строгой защитой от оверселлинга (0% overselling).
    • Создаётся центральная сущность Order, строки OrderItem (с фиксацией price_at_purchase) и событие Outbox.
    """
    buyer = db.query(User).filter(User.id == req.buyer_id).first()
    if not buyer:
        raise HTTPException(status_code=404, detail="Buyer not found")
    if not req.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    flash_engine.cleanup_expired_reservations()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.RESERVE_TTL_SECONDS)

    order = Order(
        buyer_id=req.buyer_id,
        warehouse_id=req.warehouse_id,
        status="RESERVED",
        total_amount=0,
        delivery_address=req.delivery_address,
        delivery_slot=req.delivery_slot,
        verification_code=str(1000 + (req.buyer_id * 379) % 8999),
        reserve_expires_at=expires_at,
    )
    db.add(order)
    db.flush()  # Получаем order.id

    total_amount = 0.0
    reserved_buckets_info = []

    for item_req in req.items:
        product = db.query(Product).filter(Product.id == item_req.product_id).first()
        if not product:
            db.rollback()
            raise HTTPException(status_code=404, detail=f"Product {item_req.product_id} not found")

        stock = (
            db.query(WarehouseStock)
            .filter(
                WarehouseStock.warehouse_id == req.warehouse_id,
                WarehouseStock.product_id == item_req.product_id,
            )
            .first()
        )
        if not stock or stock.available_qty < item_req.quantity:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Out of stock for product #{item_req.product_id} on warehouse #{req.warehouse_id} (Anti-Overselling CP check)",
            )

        # Если товар участвует в Flash Sale — атомарно списываем из шардированного бакета Redis/In-Memory (Этап 5)
        if product.is_flash_sale:
            res_key = f"order:{order.id}:sku:{product.id}"
            ok, bucket_idx, msg = flash_engine.try_reserve(
                reservation_id=res_key,
                user_id=req.buyer_id,
                product_id=product.id,
                quantity=item_req.quantity,
            )
            if not ok:
                db.rollback()
                raise HTTPException(status_code=409, detail=f"Flash Sale Sold Out: {msg}")
            reserved_buckets_info.append({"product_id": product.id, "bucket": bucket_idx, "info": msg})

        # Обновляем складской остаток (WarehouseStock) с версионированием
        stock.available_qty -= item_req.quantity
        stock.reserved_qty += item_req.quantity
        stock.version += 1

        purchase_price = float(product.promo_price if product.promo_price is not None else product.base_price)
        discount = float(product.base_price) - purchase_price if product.promo_price is not None else 0.0
        total_amount += purchase_price * item_req.quantity

        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            seller_id=product.seller_id,
            quantity=item_req.quantity,
            price_at_purchase=purchase_price,
            discount_amount=max(0.0, discount),
            item_status="RESERVED",
        )
        db.add(order_item)

    order.total_amount = round(total_amount, 2)

    # Transactional Outbox: публикуем события order.created и stock.reserved
    emit_outbox_event(
        db,
        topic="stock.reserved",
        aggregate_id=str(order.id),
        payload={
            "order_id": order.id,
            "buyer_id": order.buyer_id,
            "warehouse_id": order.warehouse_id,
            "total_amount": float(order.total_amount),
            "reserve_expires_at": expires_at.isoformat(),
            "flash_sale_buckets": reserved_buckets_info,
        },
    )
    db.commit()
    db.refresh(order)

    return {
        "order_id": order.id,
        "status": order.status,
        "buyer_id": order.buyer_id,
        "warehouse_id": order.warehouse_id,
        "total_amount": float(order.total_amount),
        "reserve_ttl_seconds": settings.RESERVE_TTL_SECONDS,
        "reserve_expires_at": order.reserve_expires_at,
        "verification_code": order.verification_code,
        "flash_sale_reservation": reserved_buckets_info,
        "items": [
            {
                "product_id": i.product_id,
                "seller_id": i.seller_id,
                "quantity": i.quantity,
                "price_at_purchase": float(i.price_at_purchase),
            }
            for i in order.items
        ],
    }


@app.post("/api/v1/orders/{order_id}/pay", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def pay_order(order_id: int, req: PaymentRequest, db: Session = Depends(get_db)):
    """
    ФТ-5: Онлайн-оплата заказа с 100% идемпотентностью по ключу `idempotency_key` (NFR).
    Вызывает внешний Mock-сервис банка-эквайера (или локальный fallback при запуске вне Docker),
    переводит заказ в статус PAID и отправляет задание на сборку на склад фулфилмента (WMS).
    """
    # 1. Проверка идемпотентности: если платёж с таким idempotency_key уже проведён, возвращаем его без повторного списания!
    existing_payment = db.query(Payment).filter(Payment.idempotency_key == req.idempotency_key).first()
    if existing_payment:
        return {
            "idempotent_replay": True,
            "payment_id": existing_payment.id,
            "order_id": existing_payment.order_id,
            "amount": float(existing_payment.amount),
            "status": existing_payment.status,
            "receipt_url": existing_payment.receipt_url,
            "paid_at": existing_payment.paid_at,
        }

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status == "PAID":
        raise HTTPException(status_code=409, detail="Order is already paid")
    if order.status == "CANCELLED":
        raise HTTPException(status_code=409, detail="Order was cancelled or expired")

    # 2. Обращение к внешнему Mock-сервису платёжного шлюза (с Circuit Breaker / Fallback)
    receipt_url = f"https://ofd-receipts.mock.local/check/{order.id}/{req.idempotency_key}"
    provider_name = "MOCK_ACQUIRING_BANK"
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.post(
                f"{settings.PAYMENT_MOCK_URL}/api/v1/acquiring/charge",
                json={
                    "order_id": order.id,
                    "amount": float(order.total_amount),
                    "idempotency_key": req.idempotency_key,
                    "method": req.payment_method,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                receipt_url = data.get("receipt_url", receipt_url)
                provider_name = data.get("provider", provider_name)
    except Exception as exc:
        logger.info(f"External payment-mock unreachable ({exc}), using embedded acquiring simulator fallback")

    # 3. Запись платежа и смена статуса заказа в одной транзакции (ACID)
    payment = Payment(
        order_id=order.id,
        idempotency_key=req.idempotency_key,
        amount=order.total_amount,
        provider=provider_name,
        status="SUCCEEDED",
        receipt_url=receipt_url,
    )
    db.add(payment)
    order.status = "PAID"
    for item in order.items:
        item.item_status = "PAID"
        flash_engine.confirm_payment(f"order:{order.id}:sku:{item.product_id}")

    emit_outbox_event(
        db,
        topic="payment.succeeded",
        aggregate_id=str(order.id),
        payload={
            "order_id": order.id,
            "amount": float(order.total_amount),
            "idempotency_key": req.idempotency_key,
            "receipt_url": receipt_url,
        },
    )
    db.commit()
    db.refresh(payment)

    return {
        "idempotent_replay": False,
        "payment_id": payment.id,
        "order_id": order.id,
        "order_status": order.status,
        "amount": float(payment.amount),
        "status": payment.status,
        "provider": payment.provider,
        "receipt_url": payment.receipt_url,
        "paid_at": payment.paid_at,
    }


@app.post("/api/v1/orders/{order_id}/cancel", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def cancel_unpaid_order(order_id: int, db: Session = Depends(get_db)):
    """
    Компенсирующий шаг Saga (Этап 5 и Этап 7): отмена неоплаченного заказа или истечение TTL резерва.
    Возвращает зарезервированные единицы товара обратно в доступный сток склада и в бакеты Redis.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "RESERVED":
        raise HTTPException(status_code=409, detail=f"Cannot cancel order in status {order.status}")

    for item in order.items:
        stock = (
            db.query(WarehouseStock)
            .filter(
                WarehouseStock.warehouse_id == order.warehouse_id,
                WarehouseStock.product_id == item.product_id,
            )
            .first()
        )
        if stock:
            stock.available_qty += item.quantity
            stock.reserved_qty = max(0, stock.reserved_qty - item.quantity)
            stock.version += 1
        item.item_status = "CANCELLED"
        flash_engine.release_reservation(f"order:{order.id}:sku:{item.product_id}")

    order.status = "CANCELLED"
    emit_outbox_event(
        db,
        topic="order.cancelled_stock_released",
        aggregate_id=str(order.id),
        payload={"order_id": order.id, "status": "CANCELLED"},
    )
    db.commit()
    return {"order_id": order.id, "status": order.status, "stock_returned": True}


@app.get("/api/v1/orders/{order_id}/tracking", tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def track_order(order_id: int, db: Session = Depends(get_db)):
    """
    ФТ-6: Покупатель отслеживает текущий статус сборки/доставки заказа и координаты курьера на карте.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    courier_info = None
    if order.courier:
        courier_info = {
            "courier_id": order.courier.id,
            "name": order.courier.name,
            "phone": order.courier.phone,
            "vehicle_type": order.courier.vehicle_type,
            "rating": float(order.courier.rating),
            "current_lat": float(order.courier.current_lat),
            "current_lon": float(order.courier.current_lon),
            "updated_at": order.courier.updated_at,
        }

    return {
        "order_id": order.id,
        "status": order.status,
        "warehouse": order.warehouse.name if order.warehouse else None,
        "delivery_address": order.delivery_address,
        "delivery_slot": order.delivery_slot,
        "courier": courier_info,
    }


@app.post("/api/v1/orders/{order_id}/reviews", status_code=201, tags=["1. Покупатель (Клиент) — ФТ-1..ФТ-6"])
def create_review(order_id: int, req: ReviewCreateRequest, db: Session = Depends(get_db)):
    """
    ФТ-6: Покупатель оставляет отзыв и оценку товару после получения заказа.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    review = Review(
        order_id=order_id,
        product_id=req.product_id,
        buyer_id=req.buyer_id,
        rating=req.rating,
        comment=req.comment,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return {"review_id": review.id, "order_id": order_id, "product_id": req.product_id, "rating": req.rating}


# =============================================================================
# РОЛЬ 2: ПРОДАВЕЦ / СЕЛЛЕР (ФТ-7 .. ФТ-10)
# =============================================================================

@app.post("/api/v1/seller/products", status_code=201, tags=["2. Продавец (Селлер) — ФТ-7..ФТ-10"])
def seller_create_product(req: ProductCreate, db: Session = Depends(get_db)):
    """
    ФТ-7: Продавец создаёт новую карточку товара (SKU) в каталоге маркетплейса.
    """
    seller = db.query(Seller).filter(Seller.id == req.seller_id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    product = Product(
        seller_id=req.seller_id,
        category=req.category,
        brand=req.brand,
        title=req.title,
        description=req.description,
        base_price=req.base_price,
        promo_price=req.promo_price,
        is_flash_sale=req.is_flash_sale,
        flash_sale_limit=req.flash_sale_limit,
        weight_g=req.weight_g,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"product_id": product.id, "title": product.title, "seller_id": product.seller_id}


@app.put("/api/v1/seller/products/{product_id}/flash-sale", tags=["2. Продавец (Селлер) — ФТ-7..ФТ-10"])
def seller_configure_flash_sale(product_id: int, req: FlashSaleConfigUpdate, db: Session = Depends(get_db)):
    """
    ФТ-8: Продавец управляет ценой товара и подключает его к горячей распродаже (Flash Sale).
    При этом автоматически создаются 10 шардированных бакетов остатков в памяти/Redis.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    product.base_price = req.base_price
    product.promo_price = req.promo_price
    product.is_flash_sale = req.is_flash_sale
    product.flash_sale_limit = req.flash_sale_limit

    stock = (
        db.query(WarehouseStock)
        .filter(WarehouseStock.warehouse_id == req.warehouse_id, WarehouseStock.product_id == product_id)
        .first()
    )
    if not stock:
        stock = WarehouseStock(
            warehouse_id=req.warehouse_id,
            product_id=product_id,
            bin_location="FLASH-A-01",
            available_qty=req.flash_sale_limit,
            reserved_qty=0,
        )
        db.add(stock)
    else:
        stock.available_qty = req.flash_sale_limit

    buckets = flash_engine.init_sku_buckets(product_id=product_id, total_qty=req.flash_sale_limit)
    emit_outbox_event(
        db,
        topic="catalog.price_changed",
        aggregate_id=str(product_id),
        payload={"product_id": product_id, "promo_price": req.promo_price, "flash_sale_limit": req.flash_sale_limit},
    )
    db.commit()
    return {
        "product_id": product_id,
        "is_flash_sale": product.is_flash_sale,
        "promo_price": float(product.promo_price),
        "flash_sale_limit": product.flash_sale_limit,
        "sharded_buckets": buckets,
    }


@app.post("/api/v1/seller/inbound-supplies", tags=["2. Продавец (Селлер) — ФТ-7..ФТ-10"])
def seller_create_inbound_supply(req: InboundSupplyRequest, db: Session = Depends(get_db)):
    """
    ФТ-9: Продавец оформляет заявку на поставку партии товаров на склад фулфилмента и пополняет остатки.
    """
    stock = (
        db.query(WarehouseStock)
        .filter(
            WarehouseStock.warehouse_id == req.warehouse_id,
            WarehouseStock.product_id == req.product_id,
        )
        .first()
    )
    if not stock:
        stock = WarehouseStock(
            warehouse_id=req.warehouse_id,
            product_id=req.product_id,
            bin_location=req.bin_location,
            available_qty=req.quantity,
            reserved_qty=0,
        )
        db.add(stock)
    else:
        stock.available_qty += req.quantity
        stock.bin_location = req.bin_location
        stock.version += 1

    db.commit()
    return {
        "warehouse_id": req.warehouse_id,
        "product_id": req.product_id,
        "bin_location": stock.bin_location,
        "new_available_qty": stock.available_qty,
    }


@app.get("/api/v1/seller/{seller_id}/analytics", tags=["2. Продавец (Селлер) — ФТ-7..ФТ-10"])
def seller_get_analytics(seller_id: int, db: Session = Depends(get_db)):
    """
    ФТ-10: Продавец просматривает аналитику продаж, выручку (GMV), комиссию маркетплейса и сумму к выплате.
    """
    seller = db.query(Seller).filter(Seller.id == seller_id).first()
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    items = (
        db.query(OrderItem)
        .filter(OrderItem.seller_id == seller_id, OrderItem.item_status.in_(["PAID", "PACKED", "DELIVERED"]))
        .all()
    )
    gmv = sum(float(i.price_at_purchase) * i.quantity for i in items)
    units_sold = sum(i.quantity for i in items)
    commission = round(gmv * (float(seller.commission_pct) / 100.0), 2)
    net_payout = round(gmv - commission, 2)

    return {
        "seller_id": seller.id,
        "shop_name": seller.shop_name,
        "units_sold": units_sold,
        "gross_merchandise_value_rub": round(gmv, 2),
        "marketplace_commission_pct": float(seller.commission_pct),
        "marketplace_commission_rub": commission,
        "net_payout_to_seller_rub": net_payout,
    }


# =============================================================================
# РОЛЬ 3: СОТРУДНИК СКЛАДА ФУЛФИЛМЕНТА / WMS (ФТ-11 .. ФТ-13)
# =============================================================================

@app.post("/api/v1/wms/receive", tags=["3. Сотрудник склада (WMS) — ФТ-11..ФТ-13"])
def wms_receive_and_putaway(req: WmsReceiveRequest, db: Session = Depends(get_db)):
    """
    ФТ-11: Сотрудник склада сканирует поставку в ТСД и размещает товары в адресную ячейку хранения (bin_location).
    """
    stock = (
        db.query(WarehouseStock)
        .filter(
            WarehouseStock.warehouse_id == req.warehouse_id,
            WarehouseStock.product_id == req.product_id,
        )
        .first()
    )
    if not stock:
        stock = WarehouseStock(
            warehouse_id=req.warehouse_id,
            product_id=req.product_id,
            bin_location=req.bin_location,
            available_qty=req.quantity,
            reserved_qty=0,
        )
        db.add(stock)
    else:
        stock.available_qty += req.quantity
        stock.bin_location = req.bin_location
        stock.version += 1

    db.commit()
    return {
        "status": "PUTAWAY_COMPLETED",
        "warehouse_id": req.warehouse_id,
        "product_id": req.product_id,
        "bin_location": stock.bin_location,
        "available_qty": stock.available_qty,
    }


@app.get("/api/v1/wms/picking-tasks", tags=["3. Сотрудник склада (WMS) — ФТ-11..ФТ-13"])
def wms_get_picking_tasks(warehouse_id: int = Query(1), db: Session = Depends(get_db)):
    """
    ФТ-12: Сотрудник склада получает список оплаченных заказов (статус PAID) с маршрутом обхода адресных ячеек.
    """
    orders = (
        db.query(Order)
        .filter(Order.warehouse_id == warehouse_id, Order.status == "PAID")
        .order_by(Order.id.asc())
        .all()
    )
    tasks = []
    for o in orders:
        pick_bins = []
        for item in o.items:
            st = (
                db.query(WarehouseStock)
                .filter(
                    WarehouseStock.warehouse_id == warehouse_id,
                    WarehouseStock.product_id == item.product_id,
                )
                .first()
            )
            pick_bins.append({
                "product_id": item.product_id,
                "quantity": item.quantity,
                "bin_location": st.bin_location if st else "A-01-01",
            })
        # Сортируем по коду ячейки для оптимального маршрута обхода склада
        pick_bins.sort(key=lambda x: x["bin_location"])
        tasks.append({"order_id": o.id, "status": o.status, "route_bins": pick_bins})
    return {"warehouse_id": warehouse_id, "picking_tasks_count": len(tasks), "tasks": tasks}


@app.post("/api/v1/wms/orders/{order_id}/pack", tags=["3. Сотрудник склада (WMS) — ФТ-11..ФТ-13"])
def wms_pack_order(order_id: int, db: Session = Depends(get_db)):
    """
    ФТ-13: Сотрудник склада упаковывает собранный заказ, списывает зарезервированный сток
    (reserved_qty) и переводит отправление в статус PACKED («Готов к отгрузке курьеру»).
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status not in ("PAID", "PICKING"):
        raise HTTPException(status_code=409, detail=f"Order #{order_id} must be PAID before packing (current: {order.status})")

    for item in order.items:
        stock = (
            db.query(WarehouseStock)
            .filter(
                WarehouseStock.warehouse_id == order.warehouse_id,
                WarehouseStock.product_id == item.product_id,
            )
            .first()
        )
        if stock:
            stock.reserved_qty = max(0, stock.reserved_qty - item.quantity)
            stock.version += 1
        item.item_status = "PACKED"

    order.status = "PACKED"
    emit_outbox_event(
        db,
        topic="warehouse.packed",
        aggregate_id=str(order.id),
        payload={"order_id": order.id, "warehouse_id": order.warehouse_id, "barcode": f"PKG-{order.id:06d}"},
    )
    db.commit()
    return {
        "order_id": order.id,
        "status": order.status,
        "package_barcode": f"PKG-{order.id:06d}",
        "ready_for_courier_handover": True,
    }


# =============================================================================
# РОЛЬ 4: КУРЬЕР СЛУЖБЫ ДОСТАВКИ (ФТ-14 .. ФТ-15)
# =============================================================================

@app.post("/api/v1/delivery/orders/{order_id}/pickup", tags=["4. Курьер доставки — ФТ-14..ФТ-15"])
def courier_pickup_order(order_id: int, req: CourierAssignRequest, db: Session = Depends(get_db)):
    """
    ФТ-14: Курьер принимает заказ в маршрутный лист, сканирует коробку на складе и выезжает к покупателю.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    courier = db.query(Courier).filter(Courier.id == req.courier_id).first()
    if not courier:
        raise HTTPException(status_code=404, detail="Courier not found")
    if order.status != "PACKED":
        raise HTTPException(status_code=409, detail=f"Order must be PACKED before courier pickup (current: {order.status})")

    order.courier_id = courier.id
    order.status = "IN_DELIVERY"
    courier.shift_status = "BUSY"
    emit_outbox_event(
        db,
        topic="delivery.assigned",
        aggregate_id=str(order.id),
        payload={"order_id": order.id, "courier_id": courier.id, "status": "IN_DELIVERY"},
    )
    db.commit()
    return {"order_id": order.id, "status": order.status, "courier_id": courier.id, "courier_name": courier.name}


@app.post("/api/v1/delivery/couriers/{courier_id}/location", tags=["4. Курьер доставки — ФТ-14..ФТ-15"])
def courier_update_gps(courier_id: int, req: CourierLocationUpdate, db: Session = Depends(get_db)):
    """
    ФТ-15 + NFR трекинга: Курьер передаёт свои актуальные GPS-координаты в реальном времени.
    """
    courier = db.query(Courier).filter(Courier.id == courier_id).first()
    if not courier:
        raise HTTPException(status_code=404, detail="Courier not found")

    courier.current_lat = req.lat
    courier.current_lon = req.lon
    courier.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "courier_id": courier.id,
        "current_lat": float(courier.current_lat),
        "current_lon": float(courier.current_lon),
        "updated_at": courier.updated_at,
    }


@app.post("/api/v1/delivery/orders/{order_id}/complete", tags=["4. Курьер доставки — ФТ-14..ФТ-15"])
def courier_complete_delivery(order_id: int, req: DeliveryCompleteRequest, db: Session = Depends(get_db)):
    """
    ФТ-15: Курьер передаёт заказ покупателю и подтверждает вручение проверочным кодом из приложения клиента.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "IN_DELIVERY":
        raise HTTPException(status_code=409, detail=f"Order is not IN_DELIVERY (current: {order.status})")
    if order.verification_code != req.verification_code:
        raise HTTPException(status_code=403, detail="Invalid verification code from buyer app")

    order.status = "DELIVERED"
    for item in order.items:
        item.item_status = "DELIVERED"

    courier = db.query(Courier).filter(Courier.id == req.courier_id).first()
    if courier:
        courier.shift_status = "ONLINE"

    emit_outbox_event(
        db,
        topic="order.delivered",
        aggregate_id=str(order.id),
        payload={"order_id": order.id, "courier_id": req.courier_id, "status": "DELIVERED"},
    )
    db.commit()
    return {"order_id": order.id, "status": order.status, "delivered": True}
