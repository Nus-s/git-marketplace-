# Маркетплейс с фулфилментом и механикой Flash Sales — Backend в Docker (Часть 2 / СРСП)

Рабочая реализация второй части проекта по дисциплине проектирования информационных систем: переход от системного дизайна (функциональные и нефункциональные требования, оценка нагрузки, микросервисная схема, глубокий разбор узла защиты от оверселлинга и ER-диаграмма) к контейнеризированному сервису в **Docker + Docker Compose** с реляционной БД **PostgreSQL**, кэшем **Redis**, **Mock-сервисом банковского эквайринга** и интеграционными автотестами **pytest**.

---

## 1. Архитектура и состав контейнеров (`docker-compose.yml`)

Система поднимается одной командой и включает 4 взаимодействующих сервиса:

1. **`backend` (`FastAPI / Python 3.12`, порт `8000`)** — основной REST API сервис, реализующий все 15 функциональных требований (`ФТ-1 .. ФТ-15`) для 4 ролей пользователей (Покупатель, Продавец, Сотрудник склада WMS, Курьер доставки), координатор паттерна **Saga**, таблицу **Transactional Outbox** и движок шардированных бакетов для **Flash Sales**.
2. **`postgres` (`PostgreSQL 16-alpine`, порт `5432`)** — основная реляционная СУБД, автоматически накатывающая миграцию [`migrations/001_init_schema.sql`](./migrations/001_init_schema.sql) при старте (реализация ER-диаграммы из Этапа 6 первой части со всеми внешними ключами, индексами и `CHECK`-ограничениями `available_qty >= 0`).
3. **`redis` (`Redis 7-alpine`, порт `6379`)** — In-Memory хранилище для шардированных счётчиков остатков (`sku:{id}:bucket:{0..9}`) и временного резерва с `TTL = 10 минут` (реализация сложного узла из Этапа 5 первой части).
4. **`payment-mock` (`FastAPI Mock Service`, порт `8001`)** — имитация внешнего банковского эквайринга и ОФД онлайн-кассы (54-ФЗ) с поддержкой ключей идемпотентности (`Idempotency-Key`).

---

## 2. Структура репозитория

```text
marketplace-service/
├── app/
│   ├── main.py              # REST API эндпоинты (ФТ-1..ФТ-15), middleware логирования и Rate Limit
│   ├── config.py            # Конфигурация через переменные окружения (12-Factor App)
│   ├── database.py          # Подключение SQLAlchemy к PostgreSQL / SQLite
│   ├── models.py            # ORM-модели ER-диаграммы (User, Seller, Warehouse, Courier, Product, WarehouseStock, Order, OrderItem, Payment, OutboxEvent, Review)
│   ├── schemas.py           # Pydantic-схемы валидации запросов и ответов (OpenAPI / Swagger)
│   ├── flash_sale_engine.py # Сложный узел (Этап 5): 10 шардированных бакетов стока + мягкий резерв с TTL
│   └── seed.py              # Автоматическое заполнение БД начальными тестовыми данными (Seed)
├── mock_payment/
│   ├── Dockerfile           # Контейнер имитации платёжного шлюза банка
│   └── main.py              # Эндпоинт эквайринга с проверкой Idempotency-Key и выдачей чека
├── migrations/
│   └── 001_init_schema.sql  # SQL-скрипт миграции БД (таблицы, PK/FK, индексы)
├── tests/
│   └── test_marketplace.py  # Интеграционные автотесты (pytest) всех сценариев и анти-оверселлинга
├── Dockerfile               # Dockerfile основного сервиса
├── docker-compose.yml       # Оркестрация контейнеров (backend + postgres + redis + payment-mock)
├── requirements.txt         # Зависимости Python
├── .env.example             # Образец переменных окружения
└── README.md                # Документация и инструкция по запуску
```

---

## 3. Как запустить проект

### Вариант А. Запуск всей системы в Docker Compose (рекомендуемый)

```bash
cd marketplace-service
docker compose up --build -d
```

При старте:
* Контейнер `postgres` автоматически создаст схему БД из `migrations/001_init_schema.sql`.
* Контейнер `backend` дождётся готовности `postgres` и `redis` (`healthcheck`) и автоматически выполнит `seed_initial_data()` — заполнит базу тестовыми покупателями, продавцами, складами фулфилмента, курьерами, товарами и разобьёт акционный смартфон **iPhone 16 Pro (SKU #1)** на **10 шардированных бакетов** по 10 штук.

После запуска доступны:
* **Swagger UI (интерактивная документация OpenAPI):** [http://localhost:8000/docs](http://localhost:8000/docs)
* **ReDoc документация:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
* **Healthcheck основного сервиса:** [http://localhost:8000/health](http://localhost:8000/health)
* **Healthcheck Mock-сервиса банка:** [http://localhost:8001/health](http://localhost:8001/health)

### Вариант Б. Локальный запуск без Docker (для быстрой разработки и тестов)

```bash
cd marketplace-service
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
*(В локальном режиме при отсутствии внешних контейнеров Postgres/Redis сервис автоматически переключается на встроенную БД SQLite и потокобезопасные In-Memory бакеты).*

---

## 4. Запуск интеграционных тестов (`pytest`)

Набор интеграционных тестов в `tests/test_marketplace.py` проверяет все 15 функциональных требований, идемпотентность платежей и защиту от оверселлинга в момент Flash Sale:

```bash
# Запуск внутри контейнера Docker:
docker compose exec backend pytest -v

# Или локально:
pytest -v
```

---

## 5. Примеры тестирования API (`curl`)

### 5.1. Поиск товаров и просмотр шардированных бакетов Flash Sale (ФТ-1, ФТ-2)
```bash
# Найти все товары, участвующие в горячей распродаже (Flash Sale):
curl -X GET "http://localhost:8000/api/v1/catalog/products?only_flash_sale=true"

# Открыть карточку товара #1 (покажет остатки по ячейкам складов и 10 бакетов Redis):
curl -X GET "http://localhost:8000/api/v1/catalog/products/1"
```

### 5.2. Добавление в корзину с промокодом (ФТ-3)
```bash
curl -X POST "http://localhost:8000/api/v1/cart/items" \
  -H "Content-Type: application/json" \
  -d '{"buyer_id": 1, "product_id": 2, "quantity": 1, "promo_code": "SALE5"}'
```

### 5.3. Оформление заказа и атомарный резерв стока на 10 минут (ФТ-4 + Узел Этапа 5)
```bash
curl -X POST "http://localhost:8000/api/v1/orders" \
  -H "Content-Type: application/json" \
  -d '{
    "buyer_id": 1,
    "warehouse_id": 1,
    "delivery_address": "г. Москва, Ленинский пр-т, д. 45, кв. 12",
    "delivery_slot": "18:00-20:00",
    "items": [{"product_id": 1, "quantity": 1}]
  }'
```

### 5.4. Идемпотентная онлайн-оплата через Mock-банк (ФТ-5)
```bash
curl -X POST "http://localhost:8000/api/v1/orders/1/pay" \
  -H "Content-Type: application/json" \
  -d '{"idempotency_key": "pay-order-1-unique-uuid", "payment_method": "SBP"}'
```
*(Повторный вызов с тем же `idempotency_key` вернёт `"idempotent_replay": true` без повторного списания денег).*

### 5.5. Сборка и упаковка заказа на складе фулфилмента WMS (ФТ-12, ФТ-13)
```bash
# Получить задания на сборку с маршрутом по ячейкам склада:
curl -X GET "http://localhost:8000/api/v1/wms/picking-tasks?warehouse_id=1"

# Упаковать заказ #1 и сгенерировать штрихкод коробки:
curl -X POST "http://localhost:8000/api/v1/wms/orders/1/pack"
```

### 5.6. Курьерская доставка, GPS-трекинг и вручение по коду (ФТ-14, ФТ-15, ФТ-6)
```bash
# Курьер #1 забирает упакованный заказ со склада:
curl -X POST "http://localhost:8000/api/v1/delivery/orders/1/pickup" \
  -H "Content-Type: application/json" \
  -d '{"courier_id": 1}'

# Курьер отправляет свои GPS-координаты:
curl -X POST "http://localhost:8000/api/v1/delivery/couriers/1/location" \
  -H "Content-Type: application/json" \
  -d '{"lat": 55.751244, "lon": 37.618423}'

# Покупатель отслеживает статус и координаты курьера на карте:
curl -X GET "http://localhost:8000/api/v1/orders/1/tracking"

# Курьер вручает заказ по проверочному коду покупателя (например, "1379"):
curl -X POST "http://localhost:8000/api/v1/delivery/orders/1/complete" \
  -H "Content-Type: application/json" \
  -d '{"courier_id": 1, "verification_code": "1379"}'
```

---

## 6. Записка о соответствии требованиям первой части проекта

| Требование первой части | Статус | Как именно реализовано в коде |
| :--- | :---: | :--- |
| **ФТ-1..ФТ-6 (Покупатель)** | ✅ Реализовано 100% | Эндпоинты `/api/v1/catalog/*`, `/api/v1/cart/*`, `/api/v1/orders/*`, `/tracking`, `/reviews`. |
| **ФТ-7..ФТ-10 (Продавец)** | ✅ Реализовано 100% | Эндпоинты `/api/v1/seller/products`, `/flash-sale`, `/inbound-supplies`, `/analytics`. |
| **ФТ-11..ФТ-13 (Склад WMS)** | ✅ Реализовано 100% | Эндпоинты `/api/v1/wms/receive`, `/picking-tasks` (сортировка маршрута по ячейкам `bin_location`), `/pack`. |
| **ФТ-14..ФТ-15 (Курьер)** | ✅ Реализовано 100% | Эндпоинты `/api/v1/delivery/orders/{id}/pickup`, `/couriers/{id}/location`, `/complete` (проверка кода). |
| **ER-диаграмма (Этап 6)** | ✅ Реализовано 100% | Таблицы `users`, `sellers`, `warehouses`, `couriers`, `products`, `warehouse_stocks` (N:M), `orders` (центр), `order_items` (N:M), `payments`. |
| **Сложный узел Flash Sale (Этап 5)** | ✅ Реализовано 100% | Класс `FlashSaleReservationEngine`: деление стока на 10 бакетов `sku:{id}:bucket:{0..9}`, выбор по `hash(user_id) % 10`, `TTL = 600 сек`. |
| **NFR: Идемпотентность оплат** | ✅ Реализовано 100% | Уникальный индекс `idempotency_key` в таблице `payments` и кэш ключей в `payment-mock`. |
| **NFR: Конфигурация, логи, Rate Limit** | ✅ Реализовано 100% | Настройки через ENV (`app/config.py`), замер латентности в `X-Process-Time-Ms`, Rate Limiter в middleware. |
| **Компромиссы и упрощения для Docker-сборки** | ℹ️ Примечание | Вместо развёртывания тяжёлого кластера из 64 физических серверов (Kafka + Elasticsearch + ClickHouse) шина событий реализована через паттерн **Transactional Outbox** (`outbox_events`), а внешняя платёжная система вынесена в отдельный контейнер `payment-mock`. |
