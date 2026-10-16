import os
import pytest
from fastapi.testclient import TestClient

# Используем отдельную тестовую БД SQLite для изолированного прогона pytest
os.environ["DATABASE_URL"] = "sqlite:///./test_marketplace.db"

from app.main import app
from app.database import Base, engine, SessionLocal
from app.seed import seed_initial_data


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_initial_data(db)
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if os.path.exists("test_marketplace.db"):
        os.remove("test_marketplace.db")


client = TestClient(app)


def test_01_health_and_observability_headers():
    """Проверка работоспособности сервиса и NFR: заголовок времени отклика X-Process-Time-Ms."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "X-Process-Time-Ms" in resp.headers
    assert float(resp.headers["X-Process-Time-Ms"]) < 200.0  # NFR: Latency < 200 ms


def test_02_catalog_search_and_product_card_ft1_ft2():
    """ФТ-1 и ФТ-2: Поиск товаров по фильтрам и просмотр детальной карточки SKU с бакетами."""
    resp = client.get("/api/v1/catalog/products", params={"only_flash_sale": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert data["items"][0]["is_flash_sale"] is True

    # Детальная карточка товара #1
    card = client.get("/api/v1/catalog/products/1")
    assert card.status_code == 200
    card_json = card.json()
    assert card_json["id"] == 1
    assert len(card_json["flash_sale_buckets"]) == 10
    assert sum(card_json["flash_sale_buckets"].values()) == 100


def test_03_cart_with_promo_code_ft3():
    """ФТ-3: Добавление товара в корзину и применение промокода SALE5."""
    resp = client.post(
        "/api/v1/cart/items",
        json={"buyer_id": 1, "product_id": 2, "quantity": 2, "promo_code": "SALE5"},
    )
    assert resp.status_code == 200
    cart = resp.json()
    assert cart["buyer_id"] == 1
    assert len(cart["items"]) == 1
    assert cart["items"][0]["discount_per_unit"] > 0


def test_04_full_order_lifecycle_ft4_to_ft15():
    """
    Сквозной интеграционный тест полного жизненного цикла заказа (Saga):
    Покупатель оформляет заказ (ФТ-4) -> Оплачивает с проверкой идемпотентности (ФТ-5) ->
    Склад видит задание на сборку и упаковывает заказ (ФТ-12, ФТ-13) ->
    Курьер забирает заказ, обновляет GPS и вручает по коду (ФТ-14, ФТ-15) ->
    Покупатель отслеживает трек и оставляет отзыв (ФТ-6).
    """
    # 1. Оформление заказа и резерв стока (ФТ-4)
    order_resp = client.post(
        "/api/v1/orders",
        json={
            "buyer_id": 1,
            "warehouse_id": 1,
            "delivery_address": "г. Москва, Ленинский пр-т, д. 45, кв. 12",
            "delivery_slot": "18:00-20:00",
            "items": [{"product_id": 1, "quantity": 1}],
        },
    )
    assert order_resp.status_code == 201
    order_data = order_resp.json()
    order_id = order_data["order_id"]
    ver_code = order_data["verification_code"]
    assert order_data["status"] == "RESERVED"
    assert len(order_data["flash_sale_reservation"]) == 1

    # 2. Оплата заказа (ФТ-5) + Проверка 100% идемпотентности по Idempotency Key (NFR)
    idem_key = "idem-uuid-test-0001"
    pay1 = client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"idempotency_key": idem_key, "payment_method": "SBP"},
    )
    assert pay1.status_code == 200
    assert pay1.json()["idempotent_replay"] is False
    assert pay1.json()["status"] == "SUCCEEDED"

    # Повторный запрос с тем же ключом (имитация сетевого ретрая в метро)
    pay2 = client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"idempotency_key": idem_key, "payment_method": "SBP"},
    )
    assert pay2.status_code == 200
    assert pay2.json()["idempotent_replay"] is True
    assert pay2.json()["payment_id"] == pay1.json()["payment_id"]

    # 3. Сотрудник склада получает задание на сборку (ФТ-12) и упаковывает заказ (ФТ-13)
    tasks_resp = client.get("/api/v1/wms/picking-tasks", params={"warehouse_id": 1})
    assert tasks_resp.status_code == 200
    assert any(t["order_id"] == order_id for t in tasks_resp.json()["tasks"])

    pack_resp = client.post(f"/api/v1/wms/orders/{order_id}/pack")
    assert pack_resp.status_code == 200
    assert pack_resp.json()["status"] == "PACKED"

    # 4. Курьер забирает заказ со склада (ФТ-14) и отправляет GPS-координаты (ФТ-15)
    pickup_resp = client.post(
        f"/api/v1/delivery/orders/{order_id}/pickup",
        json={"courier_id": 1},
    )
    assert pickup_resp.status_code == 200
    assert pickup_resp.json()["status"] == "IN_DELIVERY"

    gps_resp = client.post(
        "/api/v1/delivery/couriers/1/location",
        json={"lat": 55.701234, "lon": 37.561234},
    )
    assert gps_resp.status_code == 200

    # 5. Покупатель проверяет онлайн-трекинг заказа и координат курьера (ФТ-6)
    track_resp = client.get(f"/api/v1/orders/{order_id}/tracking")
    assert track_resp.status_code == 200
    assert track_resp.json()["status"] == "IN_DELIVERY"
    assert abs(track_resp.json()["courier"]["current_lat"] - 55.701234) < 1e-4

    # 6. Курьер завершает доставку с проверкой кода из приложения покупателя (ФТ-15)
    complete_resp = client.post(
        f"/api/v1/delivery/orders/{order_id}/complete",
        json={"courier_id": 1, "verification_code": ver_code},
    )
    assert complete_resp.status_code == 200
    assert complete_resp.json()["status"] == "DELIVERED"

    # 7. Покупатель оставляет отзыв (ФТ-6)
    rev_resp = client.post(
        f"/api/v1/orders/{order_id}/reviews",
        json={"buyer_id": 1, "product_id": 1, "rating": 5, "comment": "Отличная доставка!"},
    )
    assert rev_resp.status_code == 201


def test_05_seller_cabinet_and_flash_sale_anti_overselling_stage5():
    """
    ФТ-7..ФТ-10 + Глубокий разбор узла Этапа 5:
    • Продавец создаёт новый товар и запускает на него Flash Sale с лимитом ровно 5 штук.
    • Приходят 8 запросов на покупку: ровно 5 проходят успешно, а 3 получают отказ 409 Conflict (0% Overselling!).
    • Один неоплаченный заказ отменяется по TTL/компенсации Saga -> 1 штука возвращается в бакет и успешно выкупается.
    """
    # ФТ-7: Создание нового SKU продавцом
    create_prod = client.post(
        "/api/v1/seller/products",
        json={
            "seller_id": 1,
            "category": "Игровые приставки",
            "brand": "Sony",
            "title": "PlayStation 5 Pro 2TB Digital Edition",
            "base_price": 89990.0,
            "promo_price": 59990.0,
            "is_flash_sale": False,
            "flash_sale_limit": 0,
            "weight_g": 3100,
        },
    )
    assert create_prod.status_code == 201
    new_pid = create_prod.json()["product_id"]

    # ФТ-8: Запуск Flash Sale на 5 штук (распределяются по шардированным бакетам)
    flash_cfg = client.put(
        f"/api/v1/seller/products/{new_pid}/flash-sale",
        json={
            "base_price": 89990.0,
            "promo_price": 49990.0,
            "is_flash_sale": True,
            "flash_sale_limit": 5,
            "warehouse_id": 1,
        },
    )
    assert flash_cfg.status_code == 200
    assert sum(flash_cfg.json()["sharded_buckets"].values()) == 5

    # Попытка купить 8 раз подряд при остатке 5 штук
    created_orders = []
    rejected_count = 0
    for i in range(8):
        r = client.post(
            "/api/v1/orders",
            json={
                "buyer_id": (i % 3) + 1,
                "warehouse_id": 1,
                "delivery_address": "г. Москва, Тестовая ул., д. 1",
                "delivery_slot": "10:00-14:00",
                "items": [{"product_id": new_pid, "quantity": 1}],
            },
        )
        if r.status_code == 201:
            created_orders.append(r.json()["order_id"])
        elif r.status_code == 409:
            rejected_count += 1

    # Строгая проверка отсутствия оверселлинга: ровно 5 резервов создано и 3 отклонено!
    assert len(created_orders) == 5
    assert rejected_count == 3

    # Отменяем один неоплаченный резерв (имитация истечения TTL = 10 мин / компенсация Saga)
    cancel_r = client.post(f"/api/v1/orders/{created_orders[0]}/cancel")
    assert cancel_r.status_code == 200
    assert cancel_r.json()["stock_returned"] is True

    # Теперь снова доступна ровно 1 единица товара — следующий покупатель может её оформить!
    retry_order = client.post(
        "/api/v1/orders",
        json={
            "buyer_id": 2,
            "warehouse_id": 1,
            "delivery_address": "г. Санкт-Петербург, Невский пр-т, д. 88",
            "delivery_slot": "14:00-18:00",
            "items": [{"product_id": new_pid, "quantity": 1}],
        },
    )
    assert retry_order.status_code == 201

    # ФТ-10: Проверка аналитики продавца и Outbox-событий
    analytics = client.get("/api/v1/seller/1/analytics")
    assert analytics.status_code == 200
    assert analytics.json()["units_sold"] >= 1

    outbox = client.get("/api/v1/system/outbox")
    assert outbox.status_code == 200
    assert len(outbox.json()) >= 5
