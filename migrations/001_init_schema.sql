-- =============================================================================
-- Миграция 001: Реализация ER-диаграммы маркетплейса с фулфилментом (Этап 6)
-- СУБД: PostgreSQL 16
-- =============================================================================

-- 1. Таблица Покупателей (User)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    phone VARCHAR(32) NOT NULL UNIQUE,
    email VARCHAR(150) NOT NULL UNIQUE,
    default_address TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Таблица Продавцов / Селлеров (Seller)
CREATE TABLE IF NOT EXISTS sellers (
    id SERIAL PRIMARY KEY,
    shop_name VARCHAR(200) NOT NULL,
    inn VARCHAR(20) NOT NULL UNIQUE,
    rating NUMERIC(3, 2) NOT NULL DEFAULT 5.00,
    commission_pct NUMERIC(5, 2) NOT NULL DEFAULT 12.00,
    payout_account VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Таблица Складов фулфилмента (Warehouse)
CREATE TABLE IF NOT EXISTS warehouses (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    region_code VARCHAR(32) NOT NULL,
    lat NUMERIC(9, 6) NOT NULL,
    lon NUMERIC(9, 6) NOT NULL,
    warehouse_type VARCHAR(32) NOT NULL DEFAULT 'FULFILLMENT',
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- 4. Таблица Курьеров доставки (Courier)
CREATE TABLE IF NOT EXISTS couriers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    phone VARCHAR(32) NOT NULL UNIQUE,
    vehicle_type VARCHAR(32) NOT NULL DEFAULT 'CAR',
    rating NUMERIC(3, 2) NOT NULL DEFAULT 4.95,
    current_zone VARCHAR(64) NOT NULL,
    shift_status VARCHAR(32) NOT NULL DEFAULT 'ONLINE',
    current_lat NUMERIC(9, 6) DEFAULT 55.755800,
    current_lon NUMERIC(9, 6) DEFAULT 37.617300,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_couriers_status_zone ON couriers (shift_status, current_zone);

-- 5. Таблица Товаров / SKU (Product)
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    category VARCHAR(100) NOT NULL,
    brand VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    base_price NUMERIC(12, 2) NOT NULL CHECK (base_price > 0),
    promo_price NUMERIC(12, 2) CHECK (promo_price IS NULL OR promo_price > 0),
    is_flash_sale BOOLEAN NOT NULL DEFAULT FALSE,
    flash_sale_limit INTEGER DEFAULT 0,
    weight_g INTEGER NOT NULL DEFAULT 500,
    rating NUMERIC(3, 2) NOT NULL DEFAULT 4.80,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_seller ON products (seller_id);
CREATE INDEX IF NOT EXISTS idx_products_category_price ON products (category, base_price);

-- 6. Промежуточная таблица Складских остатков (WarehouseStock — развязка N:M Склад <-> Товар)
CREATE TABLE IF NOT EXISTS warehouse_stocks (
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    bin_location VARCHAR(64) NOT NULL DEFAULT 'A-01-01',
    available_qty INTEGER NOT NULL DEFAULT 0 CHECK (available_qty >= 0),
    reserved_qty INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (warehouse_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_stock_product ON warehouse_stocks (product_id);

-- 7. Центральная сущность: Заказ (Order)
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    buyer_id INTEGER NOT NULL REFERENCES users(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    courier_id INTEGER REFERENCES couriers(id),
    status VARCHAR(32) NOT NULL DEFAULT 'RESERVED',
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    delivery_address TEXT NOT NULL,
    delivery_slot VARCHAR(64) NOT NULL DEFAULT '18:00-20:00',
    verification_code VARCHAR(8) NOT NULL DEFAULT '4821',
    reserve_expires_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_orders_buyer_created ON orders (buyer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_warehouse_status ON orders (warehouse_id, status);

-- 8. Промежуточная таблица Позиций заказа (OrderItem — развязка N:M Заказ <-> Товар)
CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_at_purchase NUMERIC(12, 2) NOT NULL CHECK (price_at_purchase > 0),
    discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    item_status VARCHAR(32) NOT NULL DEFAULT 'RESERVED'
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_seller ON order_items (seller_id, product_id);

-- 9. Таблица Платежей (Payment — строгий ACID + Idempotency Key)
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    provider VARCHAR(64) NOT NULL DEFAULT 'MOCK_ACQUIRING_BANK',
    status VARCHAR(32) NOT NULL DEFAULT 'SUCCEEDED',
    receipt_url VARCHAR(255),
    paid_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments (order_id);

-- 10. Таблица Transactional Outbox (для асинхронной шины событий Saga / Kafka)
CREATE TABLE IF NOT EXISTS outbox_events (
    id SERIAL PRIMARY KEY,
    topic VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    payload_json TEXT NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 11. Таблица Отзывов покупателей (ФТ-6)
CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    buyer_id INTEGER NOT NULL REFERENCES users(id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
