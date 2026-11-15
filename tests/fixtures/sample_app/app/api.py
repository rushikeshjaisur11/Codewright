import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, text

from .rates import fetch_rate

app = FastAPI()


@app.get("/rate/{currency}")
def get_rate(currency: str):
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("select 1"))
    return {"rate": fetch_rate(currency)}


@app.get("/proxy")
async def proxy():
    async with httpx.AsyncClient() as client:
        r = await client.get("https://example.com")
    return {"status": r.status_code}
