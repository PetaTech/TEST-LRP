import os
import json
import sys
import asyncio
from fastapi import FastAPI, Request, HTTPException, WebSocket
import httpx

app = FastAPI()

def dbg(*args):
    # Print debug messages directly to stderr for Heroku logging
    print(*args, file=sys.stderr, flush=True)

# ─── BASIC ROUTES ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Root is working"}

@app.get("/health")
async def health_check():
    return {"status": "alive"}

# ─── PING FOR DEBUG ─────────────────────────────────────────────────
@app.get("/ping")
async def ping():
    dbg("🏓 PONG!")
    return {"pong": True}

# ─── CONFIG ────────────────────────────────────────────────────────
TP_CORE_URL   = os.getenv("TP_CORE_URL")
TP_RUNNER_URL = os.getenv("TP_RUNNER_URL")
TP_ALT_URL    = os.getenv("TP_ALT_URL")

# ─── WEBSOCKET HUB ─────────────────────────────────────────────────
connected_websockets = set()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.add(ws)
    dbg("🔌 WebSocket connected")
    try:
        while True:
            await ws.receive_text()
    except:
        pass
    finally:
        connected_websockets.discard(ws)
        dbg("❌ WebSocket disconnected")

async def broadcast_ws(payload):
    for ws in list(connected_websockets):
        try:
            await ws.send_text(json.dumps(payload))
        except:
            connected_websockets.discard(ws)

# ─── SEND TO ALT (TradersPost) ──────────────────────────────────────
async def send_alt_ws(data):
    dbg(f"▶▶ Sending to ALT URL: {TP_ALT_URL}")
    dbg(f"▶▶ ALT payload: {json.dumps(data, indent=2)}")
    if not TP_ALT_URL:
        dbg("❌ ALT URL not configured.")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(TP_ALT_URL, json=data)
            resp.raise_for_status()
            dbg(f"✅ ALT HTTP response: {resp.text}")
    except Exception as e:
        dbg(f"❌ Error sending ALT HTTP message: {e}")

# ─── PINE ENTRY ENDPOINT ──────────────────────────────────────────
@app.post("/pine-entry")
async def pine_entry(req: Request):
    raw = await req.body()
    dbg(f"🛠️ Raw body: {raw}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        text = raw.decode("utf-8", errors="ignore").strip('"')
        data = json.loads(text)
    dbg(f"🔍 PINE PAYLOAD: {json.dumps(data, indent=2)}")

    sid    = data.get("strategy_id")
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    dbg(f"🛈 Received strategy_id={sid}, action={action}, ticker={ticker}")
    
    # Basic validation
    if not sid or action not in ("buy", "sell", "exit") or not ticker:
        raise HTTPException(status_code=400, detail="Missing or invalid strategy_id/action/ticker")

    # broadcast raw payload to websockets
    asyncio.create_task(broadcast_ws(data))

    # ── Tiger-Alt entry (auto-trail) ─────────────────────────────
    if sid == "Tiger-Alt" and action in ("buy", "sell"):
        dbg("🛎️ Entering auto-trail branch for Tiger-Alt entry")
        
        # Extract and validate required fields
        try:
            qty = data.get("quantity")
            if not qty:
                raise ValueError("Missing quantity")
            
            # Get price from various possible locations
            price = (data.get("price") or 
                    data.get("params", {}).get("entryVersion", {}).get("price") or
                    data.get("close"))
            
            # Get order type
            order_type = (data.get("orderType") or 
                         data.get("params", {}).get("entryVersion", {}).get("orderType") or 
                         "market")
            
            # Build base TradersPost payload
            tp_payload = {
                "ticker": ticker,
                "action": action,
                "orderType": order_type,
                "quantityType": "fixed_quantity",
                "quantity": qty,
                "timeInForce": (data.get("timeInForce") or 
                               data.get("params", {}).get("entryVersion", {}).get("timeInForce") or 
                               "gtc")
            }
            
            # Add optional fields if present
            if data.get("strategy_id"):
                tp_payload["strategy_id"] = data["strategy_id"]
            if data.get("sentiment"):
                tp_payload["sentiment"] = data["sentiment"]
            if data.get("orderStrategyTypeId"):
                tp_payload["orderStrategyTypeId"] = data["orderStrategyTypeId"]
            
            # Add price for limit orders
            if order_type == "limit":
                if not price:
                    raise ValueError("Missing price - required for limit orders")
                tp_payload["price"] = price
            
            # Get trailing stop parameters
            initial_amt = (data.get("stopLoss", {}).get("amount") or 
                          data.get("stopLoss", {}).get("stopPrice"))
            trail_amt = (data.get("trailAmount") or 
                        data.get("extras", {}).get("autoTrail", {}).get("stopLoss"))
            trigger_dist = (data.get("triggerDistance") or 
                           data.get("extras", {}).get("autoTrail", {}).get("trigger"))
            
            # Add trailing stop if all parameters are present
            if initial_amt and trail_amt and trigger_dist:
                # Convert to float to ensure proper JSON serialization
                tp_payload.update({
                    "stopLoss": {
                        "type": "stop",
                        "amount": float(initial_amt)
                    },
                    "trailingStop": True,
                    "trailAmount": float(trail_amt),
                    "triggerDistance": float(trigger_dist)
                })
                dbg(f"🛎️ Added trailing stop: initial_amt={initial_amt}, trail_amt={trail_amt}, trigger_dist={trigger_dist}")
            else:
                dbg(f"🛎️ Missing trailing stop parameters - initial_amt={initial_amt}, trail_amt={trail_amt}, trigger_dist={trigger_dist}")
            
            dbg(f"→ Final TradersPost payload: {json.dumps(tp_payload, indent=2)}")
            await send_alt_ws(tp_payload)
            
            return {
                "status": "alt_http_sent",
                "order_type": order_type,
                "has_trailing": "trailingStop" in tp_payload,
                "tp_payload": tp_payload
            }
            
        except Exception as e:
            dbg(f"❌ Error building Tiger-Alt payload: {e}")
            raise HTTPException(status_code=400, detail=f"Malformed bracket data: {str(e)}")

    # ── Tiger-Alt exit (flat) ─────────────────────────────────────
    if sid == "Tiger-Alt" and action == "exit":
        dbg("🛎️ Handling exit for Tiger-Alt")
        
        # Build exit payload
        exit_payload = {
            "ticker": ticker,
            "action": "exit"
        }
        
        # Add optional fields if present
        if data.get("strategy_id"):
            exit_payload["strategy_id"] = data["strategy_id"]
        if data.get("sentiment"):
            exit_payload["sentiment"] = data["sentiment"]
        if data.get("quantity"):
            exit_payload["quantity"] = data["quantity"]
            exit_payload["quantityType"] = "fixed_quantity"
        
        # Try to get price from various sources
        price = (data.get("price") or 
                data.get("close") or 
                data.get("params", {}).get("entryVersion", {}).get("price"))
        if price:
            exit_payload["price"] = price
            dbg(f"🛎️ Added price {price} to exit signal")
        
        dbg(f"→ Exit payload: {json.dumps(exit_payload, indent=2)}")
        await send_alt_ws(exit_payload)
        return {"status": "alt_http_sent", "payload": exit_payload}

    # ── Core/Runner fallback ──────────────────────────────────────
    url = TP_CORE_URL if sid == "Tiger-Core" else TP_RUNNER_URL if sid == "Tiger-Runner" else None
    if not url:
        raise HTTPException(status_code=400, detail=f"Unknown strategy_id: {sid}")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=data)
            resp.raise_for_status()
            dbg(f"✅ Forwarded to {sid}: {resp.status_code}")
            return {"status": "forwarded", "to": sid}
    except Exception as e:
        dbg(f"❌ Error forwarding to CORE/RUNNER: {e}")
        raise HTTPException(status_code=500, detail="Forwarding error")
