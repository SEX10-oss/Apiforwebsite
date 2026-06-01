# main.py
import time
import uuid
import secrets
import httpx
import json
import base64
import gzip
import orjson
from fastapi import FastAPI, Depends, HTTPException, Security, status, Response
from fastapi.security.api_key import APIKeyHeader
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field, EmailStr
from typing import Optional

# Local Imports
import database as db
import carx_cloner
from config import WORKER_SECRET_TOKEN

# Supabase catalogs
CAR_LIST_URL = "https://rznrrywtfiyehwkfntfj.supabase.co/storage/v1/object/public/profiles/carlist.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Initializing standalone Web API...")
    await db.init_db()
    yield
    if db.pool:
        await db.pool.close()
        print("✅ Database connections closed.")

app = FastAPI(title="CarX Standalone API", lifespan=lifespan)

# API Security Check
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if not WORKER_SECRET_TOKEN:
        raise HTTPException(status_code=500, detail="Server config error: WORKER_SECRET_TOKEN missing.")
    if api_key != WORKER_SECRET_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized: Invalid API Key.")
    return api_key

# --- SCHEMAS ---

class CreateAccountRequest(BaseModel):
    email: EmailStr = Field(..., description="Target email address to register")
    password: Optional[str] = Field(None, description="Account password (auto-generated if omitted)")
    mod_id: int = Field(..., description="Mod Package ID to clone")

class InjectCarRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")
    car_id: str = Field(..., description="Database Car ID")

class InjectResourcesRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")
    silver: Optional[float] = Field(0.0)
    gold: Optional[int] = Field(0)
    xp: Optional[int] = Field(0)

class InjectNitroRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")
    car_id: Optional[str] = Field(None)

class InjectMapsRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")

# --- UTILITY ENDPOINTS ---

@app.get("/")
async def health_check():
    return Response(content="CarX Standalone Web API Service is online.", status_code=200)

# --- CLONER / CREATOR ROUTE ---

@app.post("/api/v1/create-account", dependencies=[Depends(verify_api_key)])
async def api_create_account(payload: CreateAccountRequest):
    mod = await db.get_mod_by_id(payload.mod_id)
    if not mod:
        raise HTTPException(status_code=404, detail="Selected Mod Package not found.")
    if not mod.get('src_email'):
        raise HTTPException(status_code=400, detail="Mod configuration lacks source account data.")

    target_email = payload.email.strip()
    target_password = payload.password.strip() if payload.password else secrets.token_hex(5)

    try:
        if mod["src_email"] and mod["src_email"].startswith("http"):
            await carx_cloner.execute_clone_from_snapshot(mod["src_email"], target_email, target_password)
        else:
            await carx_cloner.execute_clone(
                src_email=mod['src_email'],
                src_pass=mod['src_pass'],
                src_dev=mod['src_dev_id'],
                src_carx=mod.get('src_carx_id', ''),
                tgt_email=target_email,
                tgt_pass=target_password
            )
        await db.create_account_creation_job("WEBSITE_API", target_email, target_password, mod['id'])
        return {
            "status": "success",
            "account_credentials": {"email": target_email, "password": target_password}
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- INJECTION ROUTES ---

@app.post("/api/v1/inject/car", dependencies=[Depends(verify_api_key)])
async def api_inject_car(payload: InjectCarRequest):
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient() as client:
            r_list = await client.get(CAR_LIST_URL)
            if r_list.status_code != 200:
                raise Exception("Failed to retrieve master vehicle catalog.")
            content = r_list.text.strip()
            if not content.startswith("{"): content = "{" + content
            if not content.endswith("}"): content = content + "}"
            car_db = {}
            def scan(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k.isdigit() and isinstance(v, dict) and ("tuning" in v or "body_part_set" in v):
                            car_db[k] = v
                        else: scan(v)
                elif isinstance(d, list):
                    for item in d: scan(item)
            scan(json.loads(content))

        if payload.car_id not in car_db:
            raise HTTPException(status_code=404, detail="Selected Car ID not found in database.")

        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            garage = profile["cars"]["items"] if ("cars" in profile and "items" in profile["cars"]) else profile
            
            existing_keys = sorted([int(k) for k in garage.keys() if k.isdigit()])
            last_id = existing_keys[-1] if existing_keys else 1000
            
            pushed_id = str(last_id + 1)
            garage[pushed_id] = garage.pop(str(last_id))
            garage[str(last_id)] = car_db[payload.car_id]
            
            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
            return {"status": "success", "message": "Car injected successfully."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/inject/resources", dependencies=[Depends(verify_api_key)])
async def api_inject_resources(payload: InjectResourcesRequest):
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            
            res = profile.get("resources", {})
            if "experience" not in res or not isinstance(res["experience"], dict):
                res["experience"] = {"amount": 0}
            
            if payload.silver:
                res.setdefault("soft", {"amount": 0.0})["amount"] += float(payload.silver)
            if payload.gold:
                res.setdefault("hard", {"amount": 0})["amount"] += int(payload.gold)
            if payload.xp:
                res["experience"]["amount"] = res["experience"].get("amount", 0) + int(payload.xp)
                
            profile["resources"] = res
            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
            return {"status": "success", "added": {"silver": payload.silver, "gold": payload.gold, "xp": payload.xp}}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/inject/nitro", dependencies=[Depends(verify_api_key)])
async def api_inject_nitro(payload: InjectNitroRequest):
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            garage = profile["cars"]["items"] if ("cars" in profile and "items" in profile["cars"]) else profile
            
            current_timestamp = int(time.time())
            if payload.car_id:
                if payload.car_id not in garage:
                    raise HTTPException(status_code=404, detail="Target car not found in garage.")
                c_res = garage[payload.car_id].setdefault("consumed_resources", {})
                nitro = c_res.setdefault("nitro", {})
                nitro["ts"] = current_timestamp
                nitro["max_amount"] = 20000000
                nitro["amount"] = 20000000
            else:
                owned_cars = [k for k in garage.keys() if k.isdigit() and isinstance(garage[k], dict)]
                for c_id in owned_cars:
                    c_res = garage[c_id].setdefault("consumed_resources", {})
                    nitro = c_res.setdefault("nitro", {})
                    nitro["ts"] = current_timestamp
                    nitro["max_amount"] = 20000000
                    nitro["amount"] = 20000000

            profile["lastSyncTime"] = current_timestamp
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
            return {"status": "success", "message": "Nitro maxed successfully."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/inject/maps", dependencies=[Depends(verify_api_key)])
async def api_inject_maps(payload: InjectMapsRequest):
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            
            world_parts = profile.setdefault("game_world_parts", {})
            target_regions = ["industrial", "midtown", "suburb", "port", "mountain", "sunset"]
            for r in target_regions:
                world_parts.setdefault(r, {})["unlocked"] = True
                
            quests = profile.setdefault("quests", {})
            map_quests = [
                "move_to_industrial_intro_quest", "move_to_midtown_intro_quest",
                "move_to_suburb_intro_quest", "move_to_mountain_intro_quest", "move_to_port_intro_quest"
            ]
            for mq in map_quests:
                quest_node = quests.setdefault(mq, {})
                quest_node["completed"] = True
                quest_node["rewarded"] = True
                
            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
            return {"status": "success", "message": "Maps and regions unlocked."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
