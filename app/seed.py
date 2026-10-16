from sqlalchemy.orm import Session
from app.models import User, Seller, Warehouse, Courier, Product, WarehouseStock
from app.flash_sale_engine import flash_engine


def seed_initial_data(db: Session) -> dict:
    """
    Заполняет БД начальными тестовыми данными (Покупатели, Продавцы, Склады, Курьеры,
    Товары и адресные остатки), если база ещё пуста.
    Инициализирует шардированные бакеты для акционного товара Flash Sale.
    """
    if db.query(User).count() > 0:
        # Даже если БД уже заполнена, синхронизируем бакеты для Flash Sale товаров
        flash_products = db.query(Product).filter(Product.is_flash_sale == True).all()
        for fp in flash_products:
            stock = db.query(WarehouseStock).filter(WarehouseStock.product_id == fp.id).first()
            qty = stock.available_qty if stock else fp.flash_sale_limit
            flash_engine.init_sku_buckets(fp.id, qty)
        return {"status": "already_seeded"}

    # 1. Покупатели (User)
    buyers = [
        User(id=1, name="Алексей Смирнов", phone="+79991002030", email="alexey@example.com",
             default_address="г. Москва, Ленинский пр-т, д. 45, кв. 12"),
        User(id=2, name="Мария Соколова", phone="+79992003040", email="maria@example.com",
             default_address="г. Санкт-Петербург, Невский пр-т, д. 88, кв. 5"),
        User(id=3, name="Дмитрий Орлов", phone="+79993004050", email="dmitry@example.com",
             default_address="г. Екатеринбург, ул. Малышева, д. 19, кв. 104"),
    ]
    db.add_all(buyers)

    # 2. Продавцы (Seller)
    sellers = [
        Seller(id=1, shop_name="TechnoMir Official", inn="7701234567", rating=4.95,
               commission_pct=10.0, payout_account="40702810400000012345"),
        Seller(id=2, shop_name="HomeComfort Store", inn="7709876543", rating=4.85,
               commission_pct=14.0, payout_account="40702810400000054321"),
    ]
    db.add_all(sellers)

    # 3. Склады фулфилмента (Warehouse)
    warehouses = [
        Warehouse(id=1, name="Фулфилмент Хоругвино (Москва)", region_code="RU-MOW",
                  lat=55.982500, lon=37.214400, warehouse_type="FULFILLMENT", is_active=True),
        Warehouse(id=2, name="Даркстор Тверской (Экспресс)", region_code="RU-MOW",
                  lat=55.765200, lon=37.605100, warehouse_type="DARKSTORE", is_active=True),
    ]
    db.add_all(warehouses)

    # 4. Курьеры доставки (Courier)
    couriers = [
        Courier(id=1, name="Иван Петров", phone="+79031112233", vehicle_type="CAR",
                rating=4.98, current_zone="MOSCOW_CENTER", shift_status="ONLINE",
                current_lat=55.755800, current_lon=37.617300),
        Courier(id=2, name="Сергей Волков", phone="+79034445566", vehicle_type="BIKE",
                rating=4.91, current_zone="MOSCOW_NORTH", shift_status="ONLINE",
                current_lat=55.812300, current_lon=37.581200),
    ]
    db.add_all(couriers)
    db.flush()

    # 5. Товары / SKU (Product), включая горячий товар Flash Sale (#1)
    products = [
        Product(
            id=1,
            seller_id=1,
            category="Смартфоны",
            brand="Apple",
            title="Смартфон iPhone 16 Pro 256GB Natural Titanium (Flash Sale)",
            description="Участник горячей распродажи! Атомарный резерв через шардированные бакеты.",
            base_price=129990.00,
            promo_price=79990.00,
            is_flash_sale=True,
            flash_sale_limit=100,
            weight_g=227,
            rating=4.97,
        ),
        Product(
            id=2,
            seller_id=1,
            category="Ноутбуки",
            brand="ASUS",
            title="Ноутбук ASUS ZenBook 14 OLED (Core Ultra 7, 16GB, 1TB SSD)",
            description="Ультрабук с OLED-экраном 120 Гц и алюминиевым корпусом.",
            base_price=104990.00,
            promo_price=94990.00,
            is_flash_sale=False,
            flash_sale_limit=0,
            weight_g=1280,
            rating=4.89,
        ),
        Product(
            id=3,
            seller_id=2,
            category="Бытовая техника",
            brand="Dyson",
            title="Вертикальный пылесос Dyson V15 Detect Absolute",
            description="Лазерная подсветка пыли и пьезоэлектрический датчик.",
            base_price=69990.00,
            promo_price=None,
            is_flash_sale=False,
            flash_sale_limit=0,
            weight_g=3080,
            rating=4.94,
        ),
    ]
    db.add_all(products)
    db.flush()

    # 6. Остатки по адресным ячейкам складов (WarehouseStock — развязка N:M)
    stocks = [
        WarehouseStock(warehouse_id=1, product_id=1, bin_location="A-01-01", available_qty=100, reserved_qty=0),
        WarehouseStock(warehouse_id=1, product_id=2, bin_location="B-03-12", available_qty=40, reserved_qty=0),
        WarehouseStock(warehouse_id=1, product_id=3, bin_location="C-07-04", available_qty=25, reserved_qty=0),
        WarehouseStock(warehouse_id=2, product_id=1, bin_location="D-01-02", available_qty=15, reserved_qty=0),
    ]
    db.add_all(stocks)
    db.commit()

    # Инициализируем 10 шардированных бакетов в памяти/Redis для Flash Sale SKU #1 (10 x 10 = 100 шт.)
    flash_engine.init_sku_buckets(product_id=1, total_qty=100)

    return {
        "status": "seeded",
        "users": len(buyers),
        "sellers": len(sellers),
        "warehouses": len(warehouses),
        "couriers": len(couriers),
        "products": len(products),
        "stocks": len(stocks),
    }
