# main.py
import time
import secrets
import httpx
import json
import base64
import gzip
import orjson
import uuid 
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
CAR_LIST_URL = "https://kqybljxyobhlakrxcrld.supabase.co/storage/v1/object/public/Profile/carlist.json"
CAR_IMAGES_URL = "https://kqybljxyobhlakrxcrld.supabase.co/storage/v1/object/public/Profile/car_images.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Initializing standalone Web API...")
    await db.init_db()
    yield
    if db.pool:
        await db.pool.close()
        print("✅ Database connection closed.")

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
    account_id: str = Field(..., description="The UUID of the package in the accounts table")

class GetGarageRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")

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

class InjectLevelRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")
    xp_amount: Optional[int] = Field(93060, description="The absolute XP value to set for max level")

class InjectCustomsRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")

class InjectRealEstateRequest(BaseModel):
    email: EmailStr = Field(..., description="Target Account Email")
    password: str = Field(..., description="Target Account Password")

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
    package = await db.get_account_by_id(payload.account_id)
    if not package:
        raise HTTPException(status_code=404, detail="Selected package ID not found in database accounts table.")
    
    snapshot_url = package.get('snapshot_url')
    if not snapshot_url or not snapshot_url.startswith("http"):
        raise HTTPException(status_code=400, detail="The selected package lacks a valid Supabase snapshot_url.")

    target_email = payload.email.strip()
    target_password = payload.password.strip() if payload.password else secrets.token_hex(5)

    try:
        await carx_cloner.execute_clone_from_snapshot(snapshot_url, target_email, target_password)
        return {
            "status": "success",
            "account_credentials": {"email": target_email, "password": target_password}
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- GET GARAGE CARS ROUTE ---

@app.post("/api/v1/get-garage", dependencies=[Depends(verify_api_key)])
async def api_get_garage(payload: GetGarageRequest):
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            garage = profile["cars"]["items"] if ("cars" in profile and "items" in profile["cars"]) else profile
            
            owned_cars = []
            for c_id, c_data in garage.items():
                if c_id.isdigit() and isinstance(c_data, dict):
                    owned_cars.append({
                        "car_id": c_id,
                        "name": c_data.get("__desc_id", f"Car {c_id}")
                    })
            return {"status": "success", "cars": owned_cars}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to retrieve garage inventory: {str(e)}")

# --- GET MASTER VEHICLE CATALOG ROUTE ---

@app.get("/api/v1/master-catalog", dependencies=[Depends(verify_api_key)])
async def api_get_master_catalog():
    try:
        async with httpx.AsyncClient() as client:
            response_list = await client.get(CAR_LIST_URL)
            if response_list.status_code != 200:
                raise Exception("Failed to retrieve master vehicle catalog.")
            
            content = response_list.text.strip()
            if not content.startswith("{"): content = "{" + content
            if not content.endswith("}"): content = content + "}"
            raw_car_data = json.loads(content)

            car_maps = {}
            response_maps = await client.get(CAR_IMAGES_URL)
            if response_maps.status_code == 200:
                try:
                    car_maps = response_maps.json()
                except Exception:
                    pass

            car_registry = []
            
            def scan(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k.isdigit() and isinstance(v, dict) and ("tuning" in v or "body_part_set" in v):
                            mapping = car_maps.get(k, {})
                            display_name = mapping.get("name", v.get("__desc_id", f"Car {k}"))
                            image_url = mapping.get("image_url", "N/A")
                            
                            car_registry.append({
                                "car_id": k,
                                "name": display_name,
                                "image_url": image_url
                            })
                        else:
                            scan(v)
                elif isinstance(d, list):
                    for item in d:
                        scan(item)
            
            scan(raw_car_data)
            return {"status": "success", "catalog": car_registry}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load master catalog: {str(e)}")

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

@app.post("/api/v1/inject/level", dependencies=[Depends(verify_api_key)])
async def api_inject_level(payload: InjectLevelRequest):
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            
            res = profile.get("resources", {})
            res["experience"] = {"amount": payload.xp_amount}
            
            profile["resources"] = res
            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
            return {"status": "success", "message": f"Account experience set directly to {payload.xp_amount} (Max Level)."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/inject/customs", dependencies=[Depends(verify_api_key)])
async def api_inject_customs(payload: InjectCustomsRequest):
    """Unlocks all customization assets (Banners, Avatars, Frames) and Wheel Rims."""
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            
            bp_rewards = profile.setdefault("battle_pass_event_rewards", {})
            keys_list = bp_rewards.setdefault("keys", [])
            
            for i in range(1, 17):
                for item_type in ["banner", "avatar", "frame"]:
                    item_key = f"unlock_{item_type}_{i}"
                    if item_key not in keys_list:
                        keys_list.append(item_key)
            
            if "unlock_wheel_rim_1380" not in keys_list:
                keys_list.append("unlock_wheel_rim_1380")

            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
                
            return {"status": "success", "message": "All Banners, Avatars, Frames, and Wheel Rims unlocked."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/inject/realestate", dependencies=[Depends(verify_api_key)])
async def api_inject_realestate(payload: InjectRealEstateRequest):
    """Unlocks all Real Estates (Apartments & Houses) across the map."""
    dev_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(http2=True, timeout=60.0) as client:
            client.headers.update({"User-Agent": "UnityPlayer/6000.0.64f1", "X-Project": "STREET"})
            cont, h = await carx_cloner.get_profile(client, payload.email, payload.password, dev_id)
            profile = carx_cloner.decrypt_payload(cont["compressed_data"])
            
            real_estates = profile.setdefault("real_estates", {})
            
            individual_apts = [
                "apartment_01", "apartment_51", "apartment_95", 
                "apartment_industrial_SP", "apartment_midtown_SP", 
                "apartment_midtown2_SP", "apartment_midtown3_SP"
            ]
            for apt in individual_apts:
                real_estates.setdefault(apt, {})["is_bought"] = True
                
            grouped_estates = {
                "Industrial_apartment": 6,
                "Midtown_apartment": 12,
                "Mountain_apartment": 19,
                "Prigorod_apartment": 7,
                "Speedway_apartment": 3
            }
            for prefix, count in grouped_estates.items():
                for i in range(1, count + 1):
                    real_estates.setdefault(f"{prefix}_{i}", {})["is_bought"] = True

            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
                
            return {"status": "success", "message": "All Map Real Estate Apartments unlocked."}
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
            
            # Unlock Map Regions
            world_parts = profile.setdefault("game_world_parts", {})
            target_regions = ["industrial", "midtown", "suburb", "port", "mountain", "sunset"]
            for r in target_regions:
                world_parts.setdefault(r, {})["unlocked"] = True
                
            # Complete Progression Quests
            quests = profile.setdefault("quests", {})
            map_quests = [
                "move_to_industrial_intro_quest", "move_to_midtown_intro_quest",
                "move_to_suburb_intro_quest", "move_to_mountain_intro_quest", "move_to_port_intro_quest"
            ]
            for mq in map_quests:
                quest_node = quests.setdefault(mq, {})
                quest_node["completed"] = True
                quest_node["rewarded"] = True
                
            # Inject Shop Packs
            shop_packs = profile.setdefault("shop_owned_packs", {})
            shop_keys = shop_packs.setdefault("keys", [])
            
            new_shop_keys = [
                "special_55", "special_78", "special_54", "special_17", "special_7",
                "special_68", "special_39", "special_51", "special_10", "special_4",
                "special_49", "special_3", "special_50", "special_69", "special_1",
                "special_2", "special_43", "special_31", "special_72", "special_80",
                "special_77", "special_76", "special_75", "special_74", "special_73",
                "special_71", "special_70", "special_67", "special_8", "special_6",
                "special_11", "special_16", "special_5", "special_9", "special_12",
                "special_13", "special_14", "special_18", "special_22", "special_30",
                "special_24", "special_27", "special_28", "special_21", "special_19",
                "special_29", "special_20", "special_26", "special_23", "special_25",
                "special_15", "special_32", "special_33", "special_34", "special_35",
                "special_36", "special_37", "special_38", "special_40", "special_41",
                "special_42", "special_44", "special_45", "special_46", "special_47",
                "special_48", "special_52", "special_53", "special_56", "special_57",
                "special_58", "special_59", "special_60", "special_63", "special_61",
                "special_62", "special_64", "special_65", "special_66", "special_79",
                "special_81", "special_game_world_part_sunset_iap_full",
                "special_game_world_part_mountain_iap_full", "special_85", "special_86"
            ]

            for key in new_shop_keys:
                if key not in shop_keys:
                    shop_keys.append(key)

            profile["lastSyncTime"] = int(time.time())
            cont["compressed_data"] = carx_cloner.encrypt_payload_strict(profile)
            r_up = await client.post(f"{carx_cloner.BASE_SYNC}/profiles", json=cont, headers=h)
            if r_up.status_code != 200:
                raise Exception(r_up.text)
            return {"status": "success", "message": "Maps, regions, and shop packs unlocked."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
