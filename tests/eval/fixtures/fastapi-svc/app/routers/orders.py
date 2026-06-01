from fastapi import APIRouter

from app.models import Order

router = APIRouter(prefix="/orders", tags=["orders"])

_ORDERS: dict[int, Order] = {}


@router.get("/")
def list_orders() -> list[Order]:
    return list(_ORDERS.values())


@router.post("/")
def create_order(order: Order) -> Order:
    _ORDERS[order.id] = order
    return order
