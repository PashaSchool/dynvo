from fastapi import FastAPI

from app.routers import orders, users

app = FastAPI(title="fastapi-svc")

app.include_router(users.router)
app.include_router(orders.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
