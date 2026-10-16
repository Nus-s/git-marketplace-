from datetime import datetime, timezone
from typing import Dict
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="Mock Acquiring Bank & OFD Fiscal Service",
    version="1.0.0",
    description="Имитация внешнего банковского платёжного шлюза и онлайн-кассы (54-ФЗ) с поддержкой Idempotency Key.",
)

_processed_keys: Dict[str, dict] = {}


class ChargeRequest(BaseModel):
    order_id: int
    amount: float
    idempotency_key: str
    method: str = "CARD"


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-mock", "processed_transactions": len(_processed_keys)}


@app.post("/api/v1/acquiring/charge")
def process_charge(req: ChargeRequest):
    if req.idempotency_key in _processed_keys:
        cached = _processed_keys[req.idempotency_key].copy()
        cached["idempotent_cached"] = True
        return cached

    res = {
        "status": "SUCCEEDED",
        "provider": f"MOCK_BANK_{req.method}",
        "order_id": req.order_id,
        "amount": req.amount,
        "idempotency_key": req.idempotency_key,
        "receipt_url": f"https://ofd-receipts.mock.local/fiscal/{req.order_id}/{req.idempotency_key}",
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "idempotent_cached": False,
    }
    _processed_keys[req.idempotency_key] = res
    return res
