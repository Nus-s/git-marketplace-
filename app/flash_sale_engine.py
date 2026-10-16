import threading
import time
from typing import Dict, Optional, Tuple
from app.config import settings

try:
    import redis
except ImportError:
    redis = None


class FlashSaleReservationEngine:
    """
    Реализация сложного узла из Этапа 5 первой части проекта:
    1) Разбиение остатка горячего SKU на K = 10 независимых шардированных бакетов (Striped Counters):
       sku:{product_id}:bucket_0 ... sku:{product_id}:bucket_9
    2) Выбор стартового бакета по хешу пользователя: bucket = hash(user_id) % K
       (с быстрым обходом соседних непустых бакетов на излёте распродажи).
    3) Временный мягкий резерв (Soft Reservation) с TTL = 10 минут (600 сек) и авто-возвратом стока.
    Поддерживает работу как поверх кластера Redis (в Docker), так и потокобезопасный In-Memory режим (для локальных тестов).
    """

    def __init__(self, num_buckets: int = settings.FLASH_SALE_BUCKETS, ttl_seconds: int = settings.RESERVE_TTL_SECONDS):
        self.num_buckets = num_buckets
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._buckets: Dict[str, int] = {}
        self._reservations: Dict[str, dict] = {}
        self._redis = None

        if redis is not None:
            try:
                client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=0.5)
                client.ping()
                self._redis = client
            except Exception:
                self._redis = None

    def init_sku_buckets(self, product_id: int, total_qty: int) -> Dict[str, int]:
        """
        Делит общий пул товара (например, 1 000 шт.) на K = 10 независимых бакетов по 100 шт.
        """
        base = total_qty // self.num_buckets
        rem = total_qty % self.num_buckets
        distribution = {}

        with self._lock:
            for b in range(self.num_buckets):
                qty = base + (1 if b < rem else 0)
                key = f"sku:{product_id}:bucket:{b}"
                distribution[key] = qty
                self._buckets[key] = qty
                if self._redis is not None:
                    try:
                        self._redis.set(key, qty)
                    except Exception:
                        pass
        return distribution

    def get_buckets_snapshot(self, product_id: int) -> Dict[str, int]:
        """Возвращает текущее состояние всех 10 бакетов конкретного SKU."""
        self.cleanup_expired_reservations()
        snapshot = {}
        for b in range(self.num_buckets):
            key = f"sku:{product_id}:bucket:{b}"
            if self._redis is not None:
                try:
                    val = self._redis.get(key)
                    if val is not None:
                        snapshot[key] = int(val)
                        continue
                except Exception:
                    pass
            snapshot[key] = self._buckets.get(key, 0)
        return snapshot

    def try_reserve(
        self,
        reservation_id: str,
        user_id: int,
        product_id: int,
        quantity: int = 1,
        custom_ttl: Optional[int] = None,
    ) -> Tuple[bool, Optional[int], str]:
        """
        Атомарно резервирует `quantity` единиц товара из шардированного бакета.
        Возвращает: (успех, номер_бакета, сообщение).
        """
        self.cleanup_expired_reservations()
        ttl = custom_ttl if custom_ttl is not None else self.ttl_seconds
        start_bucket = hash(str(user_id)) % self.num_buckets

        with self._lock:
            for offset in range(self.num_buckets):
                b_idx = (start_bucket + offset) % self.num_buckets
                key = f"sku:{product_id}:bucket:{b_idx}"

                current_qty = self._buckets.get(key, 0)
                if self._redis is not None:
                    try:
                        r_val = self._redis.get(key)
                        if r_val is not None:
                            current_qty = int(r_val)
                    except Exception:
                        pass

                if current_qty >= quantity:
                    new_qty = current_qty - quantity
                    self._buckets[key] = new_qty
                    if self._redis is not None:
                        try:
                            self._redis.set(key, new_qty)
                        except Exception:
                            pass

                    expires_at = time.time() + ttl
                    self._reservations[reservation_id] = {
                        "product_id": product_id,
                        "bucket_idx": b_idx,
                        "quantity": quantity,
                        "user_id": user_id,
                        "status": "RESERVED",
                        "expires_at": expires_at,
                    }
                    return True, b_idx, f"Reserved {quantity} unit(s) in bucket #{b_idx} with TTL={ttl}s"

            return False, None, "Out of stock across all shards (0 units available)"

    def confirm_payment(self, reservation_id: str) -> bool:
        """Фиксирует резерв как проданный (SOLD) после успешной оплаты."""
        with self._lock:
            res = self._reservations.get(reservation_id)
            if not res or res["status"] != "RESERVED":
                return False
            res["status"] = "SOLD"
            return True

    def release_reservation(self, reservation_id: str) -> bool:
        """
        Компенсирующая транзакция Saga: возвращает зарезервированный сток (+quantity)
        обратно в свой бакет при отмене заказа или истечении 10-минутного TTL.
        """
        with self._lock:
            res = self._reservations.get(reservation_id)
            if not res or res["status"] != "RESERVED":
                return False
            res["status"] = "EXPIRED_RETURNED"
            key = f"sku:{res['product_id']}:bucket:{res['bucket_idx']}"
            self._buckets[key] = self._buckets.get(key, 0) + res["quantity"]
            if self._redis is not None:
                try:
                    self._redis.incrby(key, res["quantity"])
                except Exception:
                    pass
            return True

    def cleanup_expired_reservations(self) -> int:
        """Автоматически снимает просроченные по TTL неоплаченные резервы."""
        now = time.time()
        expired_ids = [
            rid for rid, r in list(self._reservations.items())
            if r["status"] == "RESERVED" and r["expires_at"] <= now
        ]
        count = 0
        for rid in expired_ids:
            if self.release_reservation(rid):
                count += 1
        return count


flash_engine = FlashSaleReservationEngine()
