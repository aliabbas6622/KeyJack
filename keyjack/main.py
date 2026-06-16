import os
import random
import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from .database import get_db, ApiKey, init_db
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

    # 2. Find available keys
    # We can detect provider from body or headers, but default to openrouter
    body = await request.json()
    provider = body.get("provider", "openrouter")

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

    # 3. Select random key
    selected_key = random.choice(available_keys)
    key_id = selected_key.id # Keep ID to update later

    # 4. Forward request
    # Simple hardcoded target for now, but could be dynamic based on provider
    target_url = "https://openrouter.ai/api/v1/chat/completions"
    if provider == "groq":
        target_url = "https://api.groq.com/openai/v1/chat/completions"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["authorization"] = f"Bearer {selected_key.key_value}"

    async def stream_generator(response):
        async for chunk in response.aiter_bytes():
            yield chunk

        # After stream finished, if it was successful, increment usage
        if response.status_code == 200:
             # We need a new session because the request session might be closed or not thread safe here
             # But since this is async, we can just use the DB again if we are careful.
             # Actually, simpler: increment usage BEFORE starting the stream or just after a successful non-stream
             pass

    async with httpx.AsyncClient() as client:
        try:
            # Check if streaming is requested
            is_streaming = body.get("stream", False)

            if is_streaming:
                # For streaming, we increment usage upfront to be safe,
                # or we'd need to handle it after the stream.
                # Let's increment upfront for simplicity in this proxy.
                selected_key.current_usage += 1
                await db.commit()

                req = client.build_request("POST", target_url, json=body, headers=headers, timeout=60.0)
                resp = await client.send(req, stream=True)

                if resp.status_code == 429:
                    # Deactivate key
                    res = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
                    k = res.scalar_one()
                    k.is_active = False
                    await db.commit()

                return StreamingResponse(
                    resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers=dict(resp.headers)
                )
            else:
                response = await client.post(
                    target_url,
                    json=body,
                    headers=headers,
                    timeout=60.0
                )

                if response.status_code == 200:
                    selected_key.current_usage += 1
                    await db.commit()
                elif response.status_code == 429:
                    selected_key.is_active = False
                    await db.commit()

                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    content = response.json()
                else:
                    content = response.text

                return JSONResponse(
                    status_code=response.status_code,
                    content=content
                )

        except httpx.RequestError as exc:
            return JSONResponse(status_code=502, content={"detail": f"Upstream error: {str(exc)}"})
        except Exception as exc:
             return JSONResponse(status_code=500, content={"detail": f"Internal Gateway Error: {str(exc)}"})
