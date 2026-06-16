import os
import random
import httpx
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Depends, HTTPException, Header, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from .database import get_db, ApiKey, init_db, RequestLog, Cache
from .config import MASTER_API_KEY, PORT

app = FastAPI(title="KeyJack")

# Add CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ASCII Art
LOGO = r"""
  _  __              _            _
 | |/ /             | |          | |
 | ' / ___ _   _    | | __ _  ___| | __
 |  < / _ \ | | |_  | |/ _` |/ __| |/ /
 | . \  __/ |_| | |_| | (_| | (__|   <
 |_|\_\___|\__, |\___/ \__,_|\___|_|\_\
            __/ |
           |___/
                                unified api gateway
"""

@app.on_event("startup")
async def startup_event():
    print(LOGO)
    print(f"KeyJack is starting on port {PORT}...")
    await init_db()

# Pydantic models
class KeyCreate(BaseModel):
    provider: str = "openrouter"
    key_value: str
    daily_limit: int = 100

class KeyResponse(BaseModel):
    id: int
    provider: str
    key_value: str  # Masked
    daily_limit: int
    current_usage: int
    is_active: bool

    class Config:
        from_attributes = True

def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "****"
    return f"{key[:4]}****{key[-4:]}"

# Routes
@app.get("/")
async def read_index():
    return FileResponse("keyjack/static/dashboard.html")

@app.get("/api/config")
async def get_config():
    return {
        "master_key": MASTER_API_KEY,
        "masked_master_key": mask_key(MASTER_API_KEY)
    }

@app.get("/v1/models")
async def get_models(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey.provider).where(ApiKey.is_active == True).distinct())
    providers = result.scalars().all()

    models = []
    for p in providers:
        if p == "openrouter":
            models.append({"id": "gpt-3.5-turbo", "object": "model", "owned_by": "openai"})
        elif p == "groq":
            models.append({"id": "llama3-8b-8192", "object": "model", "owned_by": "meta"})

    return {
        "object": "list",
        "data": models
    }

@app.get("/api/analytics")
async def get_analytics(db: AsyncSession = Depends(get_db)):
    # Simple analytics: Success Rate, Avg Latency
    res_logs = await db.execute(select(RequestLog).order_by(RequestLog.timestamp.desc()).limit(100))
    logs = res_logs.scalars().all()

    if not logs:
        return {"success_rate": 0, "avg_latency": 0, "timeline": []}

    successes = len([l for l in logs if l.status_code == 200])
    avg_latency = sum([l.latency_ms for l in logs]) / len(logs)

    timeline = [
        {"timestamp": l.timestamp.isoformat(), "status": l.status_code, "latency": l.latency_ms, "provider": l.provider}
        for l in logs
    ]

    # Cache hits count
    res_cache = await db.execute(select(Cache))
    cache_hits = len(res_cache.scalars().all()) # This is actually cache size, but good enough for a simple stat

    return {
        "success_rate": (successes / len(logs)) * 100,
        "avg_latency": round(avg_latency, 2),
        "cache_hits": cache_hits,
        "timeline": timeline
    }

@app.get("/api/keys", response_model=List[KeyResponse])
async def get_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey))
    keys = result.scalars().all()
    changed = False
    for key in keys:
        if key.reset_if_needed():
            changed = True
    if changed:
        await db.commit()

    return [
        KeyResponse(
            id=k.id,
            provider=k.provider,
            key_value=mask_key(k.key_value),
            daily_limit=k.daily_limit,
            current_usage=k.current_usage,
            is_active=k.is_active
        ) for k in keys
    ]

@app.post("/api/keys", response_model=KeyResponse)
async def add_key(data: KeyCreate, db: AsyncSession = Depends(get_db)):
    # Check duplicate
    result = await db.execute(select(ApiKey).where(ApiKey.key_value == data.key_value))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Key already exists")

    new_key = ApiKey(
        provider=data.provider,
        key_value=data.key_value,
        daily_limit=data.daily_limit
    )
    db.add(new_key)
    await db.commit()
    await db.refresh(new_key)
    return KeyResponse(
        id=new_key.id,
        provider=new_key.provider,
        key_value=mask_key(new_key.key_value),
        daily_limit=new_key.daily_limit,
        current_usage=new_key.current_usage,
        is_active=new_key.is_active
    )

@app.post("/api/keys/{key_id}/reset")
async def reset_key(key_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    key.current_usage = 0
    key.is_active = True
    await db.commit()
    return {"status": "success"}

@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    await db.delete(key)
    await db.commit()
    return {"status": "success"}

@app.post("/v1/chat/completions")
async def proxy_completions(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    # 1. Validate Master Key
    expected_auth = f"Bearer {MASTER_API_KEY}"
    if not authorization or authorization != expected_auth:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid Master API Key")

    body = await request.json()
    provider = body.get("provider", "openrouter")

    # 2. Cache Check (Value 3)
    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    cache_result = await db.get(Cache, request_hash)
    if cache_result and cache_result.created_at > datetime.now(timezone.utc) - timedelta(hours=2):
        return JSONResponse(
            content=json.loads(cache_result.response_body),
            headers={"X-KeyJack-Cache": "HIT"}
        )

    # 3. Silent Failover Loop (Value 1)
    attempts = 0
    last_error = None

    while attempts < 3:
        attempts += 1

        result = await db.execute(select(ApiKey).where(
            ApiKey.provider == provider,
            ApiKey.is_active == True
        ))
        candidates = result.scalars().all()

        available_keys = []
        changed = False
        for k in candidates:
            if k.reset_if_needed():
                changed = True
            if k.current_usage < k.daily_limit:
                available_keys.append(k)

        if changed:
            await db.commit()

        if not available_keys:
            raise HTTPException(status_code=503, detail=f"No active keys available for provider: {provider}")

        selected_key = random.choice(available_keys)
        key_id = selected_key.id

        target_url = "https://openrouter.ai/api/v1/chat/completions"
        if provider == "groq":
            target_url = "https://api.groq.com/openai/v1/chat/completions"

        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)
        headers["authorization"] = f"Bearer {selected_key.key_value}"

        start_time = time.time()

        async with httpx.AsyncClient() as client:
            try:
                is_streaming = body.get("stream", False)

                if is_streaming:
                    # Streaming bypasses caching for simplicity here
                    selected_key.current_usage += 1
                    await db.commit()

                    req = client.build_request("POST", target_url, json=body, headers=headers, timeout=60.0)
                    resp = await client.send(req, stream=True)

                    latency = int((time.time() - start_time) * 1000)
                    # Log (Value 2)
                    new_log = RequestLog(
                        virtual_key_id="master",
                        provider=provider,
                        status_code=resp.status_code,
                        latency_ms=latency
                    )
                    db.add(new_log)

                    if resp.status_code == 429 or resp.status_code >= 500:
                        selected_key.is_active = False
                        await db.commit()
                        last_error = f"Upstream returned {resp.status_code}"
                        continue # Retry

                    await db.commit()
                    return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code, headers=dict(resp.headers))

                else:
                    response = await client.post(target_url, json=body, headers=headers, timeout=60.0)
                    latency = int((time.time() - start_time) * 1000)

                    # Log (Value 2)
                    new_log = RequestLog(
                        virtual_key_id="master",
                        provider=provider,
                        status_code=response.status_code,
                        latency_ms=latency
                    )
                    db.add(new_log)

                    if response.status_code == 200:
                        selected_key.current_usage += 1
                        # Cache (Value 3)
                        new_cache = Cache(
                            request_hash=request_hash,
                            response_body=json.dumps(response.json()),
                            provider=provider
                        )
                        await db.merge(new_cache)
                        await db.commit()

                        return JSONResponse(status_code=200, content=response.json())

                    elif response.status_code == 429 or response.status_code >= 500:
                        selected_key.is_active = False
                        await db.commit()
                        last_error = f"Upstream returned {response.status_code}"
                        continue # Retry

                    await db.commit()
                    return JSONResponse(status_code=response.status_code, content=response.json() if "application/json" in response.headers.get("content-type", "") else response.text)

            except httpx.RequestError as exc:
                last_error = str(exc)
                continue # Retry
            except Exception as exc:
                 return JSONResponse(status_code=500, content={"detail": f"Internal Gateway Error: {str(exc)}"})

    raise HTTPException(status_code=503, detail=f"All retries failed. Last error: {last_error}")
