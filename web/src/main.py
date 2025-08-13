# web/src/main.py
import os
from pydantic import BaseModel
import secrets

from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional, Tuple

# --- Windows-friendly: load .env early ---
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", ".env"))  # fallback
load_dotenv()  # also load from current working dir

import asyncpg
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse

# ---- Config ----
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in .env or environment")

VALID_COUNTRIES = {"RU", "KZ"}

app = FastAPI(title="Redirect Service", version="1.1.1")

# ---- DB pool ----
@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()

# ---- Health ----
@app.get("/health")
async def health():
    return {"status": "ok"}

# ---- Redirect ----
@app.get("/r/{slug}")
async def redirect(slug: str, c: str, u: Optional[str] = None):
    if c not in VALID_COUNTRIES:
        raise HTTPException(status_code=400, detail="Invalid country")

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT url, active FROM offers WHERE slug=$1", slug)
        if not row or not row["active"]:
            raise HTTPException(status_code=404, detail="Offer not found or inactive")

        await conn.execute(
            "INSERT INTO clicks (offer_slug, country, uid_hash) VALUES ($1, $2, $3)",
            slug, c, u
        )

    return RedirectResponse(url=row["url"])

# ---- Helpers ----
def _parse_dates(from_date: str, to_date: str) -> Tuple[datetime, datetime]:
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format (YYYY-MM-DD)")
    if end < start:
        raise HTTPException(status_code=400, detail="to_date must be ≥ from_date")
    # включаем конечный день полностью
    return start, end + timedelta(days=1)

async def _fetch_clicks_grouped(conn, start: datetime, end: datetime, country: Optional[str] = None):
    if country and country not in VALID_COUNTRIES:
        raise HTTPException(status_code=400, detail="Unknown country")
    if country:
        q = """
            SELECT offer_slug, COUNT(*) AS clicks
            FROM clicks
            WHERE ts >= $1 AND ts < $2 AND country = $3
            GROUP BY offer_slug
            ORDER BY clicks DESC
        """
        rows = await conn.fetch(q, start, end, country)
    else:
        q = """
            SELECT offer_slug, COUNT(*) AS clicks
            FROM clicks
            WHERE ts >= $1 AND ts < $2
            GROUP BY offer_slug
            ORDER BY clicks DESC
        """
        rows = await conn.fetch(q, start, end)
    return rows

async def _fetch_daily_series(conn, start: datetime, end: datetime, country: Optional[str] = None):
    if country and country not in VALID_COUNTRIES:
        raise HTTPException(status_code=400, detail="Unknown country")
    if country:
        q = """
            SELECT date_trunc('day', ts) AS d, COUNT(*) AS clicks
            FROM clicks
            WHERE ts >= $1 AND ts < $2 AND country = $3
            GROUP BY d
            ORDER BY d
        """
        rows = await conn.fetch(q, start, end, country)
    else:
        q = """
            SELECT date_trunc('day', ts) AS d, COUNT(*) AS clicks
            FROM clicks
            WHERE ts >= $1 AND ts < $2
            GROUP BY d
            ORDER BY d
        """
        rows = await conn.fetch(q, start, end)
    return rows

# ---- JSON stats ----
@app.get("/stats/range")
async def stats_range(from_date: str, to_date: str, country: Optional[str] = None):
    start, end = _parse_dates(from_date, to_date)

    async with app.state.pool.acquire() as conn:
        by_offer = await _fetch_clicks_grouped(conn, start, end, country)
        daily = await _fetch_daily_series(conn, start, end, country)

    data = {
        "range": {"from": from_date, "to": to_date, "country": country},
        "by_offer": [{"offer_slug": r["offer_slug"], "clicks": r["clicks"]} for r in by_offer],
        "daily": [{"date": r["d"].date().isoformat(), "clicks": r["clicks"]} for r in daily],
    }
    return JSONResponse(data)

# ---- PNG chart ----
@app.get("/stats/plot")
async def stats_plot(from_date: str, to_date: str, country: Optional[str] = None, top: int = 10):
    start, end = _parse_dates(from_date, to_date)

    async with app.state.pool.acquire() as conn:
        by_offer_rows = await _fetch_clicks_grouped(conn, start, end, country)
        daily_rows = await _fetch_daily_series(conn, start, end, country)

    import pandas as pd  # ensure available in this scope
    df_offer = pd.DataFrame([{"offer_slug": r["offer_slug"], "clicks": int(r["clicks"])} for r in by_offer_rows])
    df_daily = pd.DataFrame([{"date": r["d"].date(), "clicks": int(r["clicks"])} for r in daily_rows])

    from io import BytesIO
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "DejaVu Sans"  # на Windows обычно есть
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), dpi=150)

    # Бар-чарт
    if df_offer.empty:
        axes[0].text(0.5, 0.5, "Нет данных по офферам", ha="center", va="center")
        axes[0].axis("off")
    else:
        df_offer = df_offer.sort_values("clicks", ascending=False)
        if len(df_offer) > top:
            top_df = df_offer.head(top)
            other = df_offer["clicks"].iloc[top:].sum()
            top_df = pd.concat([top_df, pd.DataFrame([{"offer_slug": "Другое", "clicks": other}])], ignore_index=True)
        else:
            top_df = df_offer
        axes[0].barh(top_df["offer_slug"], top_df["clicks"], color="#4C78A8")
        axes[0].invert_yaxis()
        axes[0].set_title(f"Клики по офферам (топ {min(top, len(top_df))})")
        axes[0].set_xlabel("Клики")

    # Линия по дням
    if df_daily.empty:
        axes[1].text(0.5, 0.5, "Нет данных по дням", ha="center", va="center")
        axes[1].axis("off")
    else:
        idx = pd.date_range(df_daily["date"].min(), df_daily["date"].max(), freq="D")
        df_daily = df_daily.set_index("date").reindex(idx, fill_value=0).rename_axis("date").reset_index()
        axes[1].plot(df_daily["date"], df_daily["clicks"], color="#F58518", marker="o", linewidth=2)
        axes[1].set_title("Динамика кликов")
        axes[1].set_xlabel("Дата")
        axes[1].set_ylabel("Клики")
        fig.autofmt_xdate(rotation=30)

    country_title = f" ({country})" if country else ""
    fig.suptitle(f"Статистика кликов{country_title}: {from_date} — {to_date}", fontsize=13, y=0.98)
    fig.tight_layout(rect=[0, 0.02, 1, 0.95])

    buf = BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

# ---- Short links ----
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
def _b62(n: int) -> str:
    if n == 0: return "0"
    s = []
    while n:
        n, r = divmod(n, 62)
        s.append(_B62[r])
    return "".join(reversed(s))

class ShortReq(BaseModel):
    slug: str
    c: str
    u: str

async def _make_id(conn) -> str:
    # 8 символов base62 ~ 47 бит, коллизии крайне маловероятны
    for _ in range(5):
        sid = _b62(secrets.randbits(47)).rjust(8, "0")
        exists = await conn.fetchval("SELECT 1 FROM short_links WHERE id=$1", sid)
        if not exists:
            return sid
    raise HTTPException(500, "Could not allocate id")

@app.post("/s/new")
async def short_new(req: ShortReq):
    if req.c not in VALID_COUNTRIES:
        raise HTTPException(status_code=400, detail="Invalid country")

    async with app.state.pool.acquire() as conn:
        sid = await _make_id(conn)
        await conn.execute(
            "INSERT INTO short_links(id, slug, c, u) VALUES ($1,$2,$3,$4)",
            sid, req.slug, req.c, req.u
        )
    # возвращаем путь, бот сам добавит домен
    return {"id": sid, "path": f"/s/{sid}"}

@app.get("/s/{sid}")
async def short_get(sid: str):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT slug, c, u FROM short_links WHERE id=$1", sid)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    # редиректим на существующий длинный маршрут
    return RedirectResponse(url=f"/r/{row['slug']}?c={row['c']}&u={row['u']}")


# ---- Local run ----
if __name__ == "__main__":
    import uvicorn
    # host=127.0.0.1 — чтобы Windows Firewall не спрашивал про публичные сети
    uvicorn.run(app, host="127.0.0.1", port=8000)
