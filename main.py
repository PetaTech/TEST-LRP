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
            
            # Only add signalPrice if we have a price and need it for relative calculations
            # Check if we'll need signalPrice for relative stop loss calculations
            needs_signal_price = False
            if data.get("stopLoss", {}).get("amount") or data.get("stopLoss", {}).get("percent"):
                needs_signal_price = True
            
            if price and needs_signal_price:
                tp_payload["signalPrice"] = float(price)
                dbg(f"🛎️ Added signalPrice: {price} (needed for relative calculations)")
            elif price:
                dbg(f"🛎️ Price available ({price}) but not needed for absolute stop loss")
            else:
                dbg("🛎️ No price available - using absolute stop loss values")
            
            # Add price for limit orders
            if order_type == "limit":
                if not price:
                    raise ValueError("Missing price - required for limit orders")
                tp_payload["limitPrice"] = float(price)
            
            # Handle stop loss configuration
            stop_loss_config = data.get("stopLoss", {})
            extras_config = data.get("extras", {}).get("autoTrail", {})
            
            # Check if we have trailing stop configuration
            trail_amt = extras_config.get("stopLoss")  # This is the trail amount (40)
            trigger_dist = extras_config.get("trigger")  # This is the trigger distance (60)
            
            if trail_amt and trigger_dist:
                # This is a trailing stop configuration
                dbg(f"🛎️ Configuring trailing stop: trail_amt={trail_amt}, trigger_dist={trigger_dist}")
                
                # For trailing stops, we use the relative trail amount, not the absolute stopPrice
                tp_payload.update({
                    "stopLoss": {
                        "type": "trailing_stop",
                        "trailAmount": float(trail_amt)
                    }
                })
                
                # Add trigger distance if supported (this might be custom logic)
                if trigger_dist:
                    tp_payload["triggerDistance"] = float(trigger_dist)
                
                dbg(f"🛎️ Added trailing stop: type=trailing_stop, trailAmount={trail_amt}, triggerDistance={trigger_dist}")
                
            elif stop_loss_config:
                # This is a regular stop loss
                stop_price = stop_loss_config.get("stopPrice")
                stop_amount = stop_loss_config.get("amount")
                stop_percent = stop_loss_config.get("percent")
                stop_type = stop_loss_config.get("type", "stop")
                
                if stop_price:
                    # Absolute stop price
                    tp_payload["stopLoss"] = {
                        "type": stop_type,
                        "stopPrice": float(stop_price)
                    }
                    dbg(f"🛎️ Added absolute stop loss: stopPrice={stop_price}")
                    
                elif stop_amount:
                    # Relative stop amount (requires signalPrice)
                    tp_payload["stopLoss"] = {
                        "type": stop_type,
                        "amount": float(stop_amount)
                    }
                    dbg(f"🛎️ Added relative stop loss: amount={stop_amount}")
                    
                elif stop_percent:
                    # Relative stop percent (requires signalPrice)
                    tp_payload["stopLoss"] = {
                        "type": stop_type,
                        "percent": float(stop_percent)
                    }
                    dbg(f"🛎️ Added relative stop loss: percent={stop_percent}")
                    
            else:
                dbg("🛎️ No stop loss configuration found")
            
            dbg(f"→ Final TradersPost payload: {json.dumps(tp_payload, indent=2)}")
            await send_alt_ws(tp_payload)
            
            return {
                "status": "alt_http_sent",
                "order_type": order_type,
                "has_stop_loss": "stopLoss" in tp_payload,
                "stop_loss_type": tp_payload.get("stopLoss", {}).get("type"),
                "has_signal_price": "signalPrice" in tp_payload,
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
            exit_payload["signalPrice"] = float(price)
            dbg(f"🛎️ Added signalPrice {price} to exit signal")
        
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
