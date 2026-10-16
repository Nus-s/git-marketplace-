import os


class Settings:
    """
    Конфигурация сервиса через переменные окружения (NFR: Конфигурируемость).
    """
    APP_NAME: str = os.getenv("APP_NAME", "Marketplace Fulfillment & Flash Sale API")
    APP_VERSION: str = "1.0.0"
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # По умолчанию в Docker используется PostgreSQL; при локальном запуске без Docker — SQLite
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./marketplace.db")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    PAYMENT_MOCK_URL: str = os.getenv("PAYMENT_MOCK_URL", "http://payment-mock:8001")

    # Параметры узла Flash Sale и мягкого резерва (Этап 5 первой части)
    RESERVE_TTL_SECONDS: int = int(os.getenv("RESERVE_TTL_SECONDS", "600"))  # 10 минут
    FLASH_SALE_BUCKETS: int = int(os.getenv("FLASH_SALE_BUCKETS", "10"))     # 10 шардированных бакетов

    # Защита от перегрузки (Rate Limiting на уровне шлюза/приложения)
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "1200"))


settings = Settings()
