import os
import json
import sys
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any, Set
from fastapi import FastAPI, Request, HTTPException, WebSocket
import httpx
import redis.asyncio as redis
import websockets
import ssl
import re

app = FastAPI()

FUTURES_MONTH_CODES = {
    'F': 'January',
    'G': 'February', 
    'H': 'March',
    'J': 'April',
    'K': 'May',
    'M': 'June',
    'N': 'July',
    'Q': 'August',
    'U': 'September',
    'V': 'October',
    'X': 'November',
    'Z': 'December'
}

def get_month_name(month_code: str) -> str:
    """Get month name from futures month code"""
    return FUTURES_MONTH_CODES.get(month_code, f"Unknown({month_code})")

# ─── REDIS CONNECTION ──────────────────────────────────────────────
redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise Exception("REDIS_URL environment variable not set")
        redis_client = redis.from_url(redis_url, decode_responses=True)
    return redis_client

def dbg(*args):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}]", *args, file=sys.stderr, flush=True)

# ─── BASIC ROUTES ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Tiger-Alt Bot API v1.8 - Polygon.io Integration"}

@app.get("/health")
async def health_check():
    try:
        r = await get_redis()
        await r.ping()
        polygon_status = "connected" if price_monitor.is_connected else "disconnected"
        return {
            "status": "healthy", 
            "redis": "connected",
            "polygon": polygon_status,
            "monitored_symbols": list(price_monitor.monitored_symbols)
        }
    except Exception as e:
        return {"status": "unhealthy", "redis": f"error: {str(e)}"}

@app.get("/ping")
async def ping():
    dbg("🏓 PONG!")
    return {"pong": True, "timestamp": datetime.now(timezone.utc).isoformat()}

# ─── CONFIG ────────────────────────────────────────────────────────
TP_CORE_URL   = os.getenv("TP_CORE_URL")
TP_RUNNER_URL = os.getenv("TP_RUNNER_URL")
TP_ALT_URL    = os.getenv("TP_ALT_URL")
POLYGON_KEY   = os.getenv("POLYGON_KEY")

if not POLYGON_KEY:
    dbg("❌ POLYGON_KEY environment variable not set")
    raise Exception("POLYGON_KEY environment variable is required")

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

# ─── SYMBOL CONVERSION ─────────────────────────────────────────────
def tradingview_to_polygon(tv_symbol: str) -> str:
    """Convert TradingView symbol to Polygon.io format"""
    
    # ─── CME FUTURES CONTRACT PATTERN ─────────────────────────────────
    # Match patterns like: MNQU2025, ESU2024, NQZ2023, etc.
    cme_pattern = r'^([A-Z]{2,4})([FGHJKMNQUVXZ])(\d{4})$'
    cme_match = re.match(cme_pattern, tv_symbol)
    
    if cme_match:
        root_symbol = cme_match.group(1)
        month_code = cme_match.group(2)
        year = cme_match.group(3)
        
        # Convert to 2-digit year (2025 -> 25, 2024 -> 24)
        year_2digit = year[-2:]
        
        # Build Polygon.io futures format: I:ROOTMONTHYEAR
        polygon_symbol = f"I:{root_symbol}{month_code}{year_2digit}"
        
        dbg(f"🔄 CME Contract: {tv_symbol} -> {polygon_symbol}")
        return polygon_symbol
    
    # ─── STATIC SYMBOL MAPPINGS ───────────────────────────────────────
    symbol_map = {
        # Generic Futures (front month)
        "NQ": "I:NQ",           # NASDAQ-100 E-mini
        "ES": "I:ES",           # S&P 500 E-mini
        "YM": "I:YM",           # Dow Jones E-mini
        "RTY": "I:RTY",         # Russell 2000 E-mini
        "GC": "I:GC",           # Gold
        "CL": "I:CL",           # Crude Oil
        "NG": "I:NG",           # Natural Gas
        "ZB": "I:ZB",           # 30-Year Treasury Bond
        "ZN": "I:ZN",           # 10-Year Treasury Note
        "ZF": "I:ZF",           # 5-Year Treasury Note
        "6E": "I:6E",           # Euro FX
        "6J": "I:6J",           # Japanese Yen
        "6B": "I:6B",           # British Pound
        "6A": "I:6A",           # Australian Dollar
        "6C": "I:6C",           # Canadian Dollar
        "6S": "I:6S",           # Swiss Franc
        
        # Micro Futures (generic)
        "MNQ": "I:MNQ",         # Micro NASDAQ-100
        "MES": "I:MES",         # Micro S&P 500
        "MYM": "I:MYM",         # Micro Dow Jones
        "M2K": "I:M2K",         # Micro Russell 2000
        "MGC": "I:MGC",         # Micro Gold
        "MCL": "I:MCL",         # Micro Crude Oil
        
        # Crypto
        "BTCUSD": "X:BTCUSD",   # Bitcoin
        "ETHUSD": "X:ETHUSD",   # Ethereum
        "ADAUSD": "X:ADAUSD",   # Cardano
        "SOLUSD": "X:SOLUSD",   # Solana
        
        # Forex
        "EURUSD": "C:EURUSD",   # Euro/USD
        "GBPUSD": "C:GBPUSD",   # Pound/USD
        "USDJPY": "C:USDJPY",   # USD/Yen
        "AUDUSD": "C:AUDUSD",   # Aussie/USD
        "USDCAD": "C:USDCAD",   # USD/CAD
        "USDCHF": "C:USDCHF",   # USD/Swiss
    }
    
    # Direct mapping check
    if tv_symbol in symbol_map:
        return symbol_map[tv_symbol]
    
    # ─── STOCK SYMBOL HANDLING ────────────────────────────────────────
    # For stocks, use as-is (should work for most major stocks)
    if tv_symbol.replace(".", "").isalpha():
        return tv_symbol
    
    # ─── FALLBACK ──────────────────────────────────────────────────────
    # Default: return as-is and log warning
    dbg(f"⚠️ Unknown symbol mapping: {tv_symbol} -> using as-is")
    return tv_symbol

# ─── POLYGON.IO PRICE MONITOR ──────────────────────────────────────
class PolygonPriceMonitor:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.ws = None
        self.is_connected = False
        self.monitored_symbols: Set[str] = set()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.position_manager = None
        
    async def connect(self):
        """Connect to Polygon.io WebSocket"""
        try:
            uri = f"wss://socket.polygon.io/forex"
            ssl_context = ssl.create_default_context()
            
            dbg("🔌 Connecting to Polygon.io WebSocket...")
            self.ws = await websockets.connect(uri, ssl=ssl_context)
            
            # Authenticate
            auth_message = {"action": "auth", "params": self.api_key}
            await self.ws.send(json.dumps(auth_message))

            # Wait for auth response
            response = await self.ws.recv()
            auth_list = json.loads(response)

            if isinstance(auth_list, list):
                for msg in auth_list:
                    if msg.get("ev") == "status" and msg.get("status") == "auth_success":
                        dbg("✅ Polygon.io authentication successful")
                        self.is_connected = True
                        self.reconnect_attempts = 0

                        if self.monitored_symbols:
                            await self._subscribe_to_symbols()

                        asyncio.create_task(self._listen_for_messages())
                        return

                dbg(f"❌ Polygon.io auth failed: {auth_list}")
                self.is_connected = False
            else:
                dbg(f"❌ Unexpected Polygon auth response: {auth_list}")
                self.is_connected = False

        except Exception as e:
            dbg(f"❌ Polygon.io connection error: {e}")
            self.is_connected = False
            await self._handle_reconnect()
    
    async def _listen_for_messages(self):
        """Listen for incoming WebSocket messages"""
        try:
            async for message in self.ws:
                data = json.loads(message)
                
                # Handle different message types
                if isinstance(data, list):
                    for msg in data:
                        await self._process_message(msg)
                else:
                    await self._process_message(data)
                    
        except websockets.exceptions.ConnectionClosed:
            dbg("⚠️ Polygon.io WebSocket connection closed")
            self.is_connected = False
            await self._handle_reconnect()
        except Exception as e:
            dbg(f"❌ Error listening to Polygon.io messages: {e}")
            self.is_connected = False
            await self._handle_reconnect()
    
    async def _process_message(self, msg: Dict):
        """Process individual WebSocket message"""
        try:
            # Handle trade messages (real-time prices)
            if msg.get("ev") == "T":  # Trade event
                symbol = msg.get("sym")
                price = msg.get("p")
                timestamp = msg.get("t")
                
                if symbol and price:
                    # Log every price update
                    dbg(f"📊 PRICE: {symbol} = ${price} @ {timestamp}")
                    
                    # Convert polygon symbol back to TradingView format for position lookup
                    tv_symbol = self._polygon_to_tradingview(symbol)
                    
                    # Check for auto-trail updates
                    if self.position_manager and tv_symbol in self.monitored_symbols:
                        trail_result = await self.position_manager.calculate_auto_trail(tv_symbol, float(price))
                        
                        if trail_result:
                            dbg(f"🔄 Auto-trail triggered for {tv_symbol}: {trail_result}")
                            
                            # Broadcast trail update
                            await broadcast_ws({
                                "type": "auto_trail_update",
                                "symbol": tv_symbol,
                                "price": price,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "trail_result": trail_result
                            })
            
            # Handle aggregated bars (A messages)
            elif msg.get("ev") == "A":
                symbol = msg.get("sym")
                close_price = msg.get("c")
                
                if symbol and close_price:
                    dbg(f"📊 BAR: {symbol} close = ${close_price}")
            
            # Handle status messages
            elif msg.get("ev") == "status":
                status = msg.get("status")
                message = msg.get("message", "")
                dbg(f"📡 Polygon status: {status} - {message}")
                
        except Exception as e:
            dbg(f"❌ Error processing Polygon message: {e}")
    
    def _polygon_to_tradingview(self, polygon_symbol: str) -> str:
        """Convert Polygon symbol back to TradingView format"""
        
        # ─── CME FUTURES CONTRACT PATTERN ─────────────────────────────────
        # Match patterns like: I:MNQU25, I:ESU24, I:NQZ23, etc.
        cme_pattern = r'^I:([A-Z]{2,4})([FGHJKMNQUVXZ])(\d{2})$'
        cme_match = re.match(cme_pattern, polygon_symbol)
        
        if cme_match:
            root_symbol = cme_match.group(1)
            month_code = cme_match.group(2)
            year_2digit = cme_match.group(3)
            
            # Convert to 4-digit year (25 -> 2025, 24 -> 2024)
            # Assume years 00-30 are 2000s, 31-99 are 1900s (but for futures, likely 2000s)
            year_int = int(year_2digit)
            if year_int <= 30:
                year_4digit = f"20{year_2digit}"
            else:
                year_4digit = f"19{year_2digit}"
            
            # Build TradingView format: ROOTMONTHYEAR
            tv_symbol = f"{root_symbol}{month_code}{year_4digit}"
            
            dbg(f"🔄 CME Contract: {polygon_symbol} -> {tv_symbol}")
            return tv_symbol
        
        # ─── PREFIX REMOVAL ───────────────────────────────────────────────
        # Remove prefixes and return base symbol
        if polygon_symbol.startswith("I:"):
            return polygon_symbol[2:]
        elif polygon_symbol.startswith("X:"):
            return polygon_symbol[2:]
        elif polygon_symbol.startswith("C:"):
            return polygon_symbol[2:]
        else:
            return polygon_symbol
    
    async def _subscribe_to_symbols(self):
        """Subscribe to all monitored symbols"""
        if not self.is_connected or not self.monitored_symbols:
            return
        
        # Convert TradingView symbols to Polygon format
        polygon_symbols = [tradingview_to_polygon(symbol) for symbol in self.monitored_symbols]
        
        # Subscribe to trades
        subscribe_message = {
            "action": "subscribe",
            "params": ",".join([f"T.{symbol}" for symbol in polygon_symbols])
        }
        
        await self.ws.send(json.dumps(subscribe_message))
        dbg(f"📡 Subscribed to Polygon symbols: {polygon_symbols}")
    
    async def add_symbol(self, tv_symbol: str):
        """Add a symbol to monitoring"""
        self.monitored_symbols.add(tv_symbol)
        
        if self.is_connected:
            polygon_symbol = tradingview_to_polygon(tv_symbol)
            subscribe_message = {
                "action": "subscribe",
                "params": f"T.{polygon_symbol}"
            }
            await self.ws.send(json.dumps(subscribe_message))
            dbg(f"📡 Added symbol to monitoring: {tv_symbol} -> {polygon_symbol}")
    
    async def remove_symbol(self, tv_symbol: str):
        """Remove a symbol from monitoring"""
        self.monitored_symbols.discard(tv_symbol)
        
        if self.is_connected:
            polygon_symbol = tradingview_to_polygon(tv_symbol)
            unsubscribe_message = {
                "action": "unsubscribe",
                "params": f"T.{polygon_symbol}"
            }
            await self.ws.send(json.dumps(unsubscribe_message))
            dbg(f"📡 Removed symbol from monitoring: {tv_symbol} -> {polygon_symbol}")
    
    async def _handle_reconnect(self):
        """Handle reconnection logic"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            dbg(f"❌ Max reconnection attempts reached ({self.max_reconnect_attempts})")
            return
        
        self.reconnect_attempts += 1
        wait_time = min(2 ** self.reconnect_attempts, 60)  # Exponential backoff, max 60s
        
        dbg(f"🔄 Reconnecting to Polygon.io in {wait_time}s (attempt {self.reconnect_attempts})")
        await asyncio.sleep(wait_time)
        await self.connect()
    
    async def disconnect(self):
        """Disconnect from Polygon.io"""
        if self.ws:
            await self.ws.close()
        self.is_connected = False
        dbg("🔌 Disconnected from Polygon.io")

# Initialize price monitor
price_monitor = PolygonPriceMonitor(POLYGON_KEY)

# ─── REDIS POSITION MANAGEMENT ────────────────────────────────────
class PositionManager:
    def __init__(self):
        self.redis = None
    
    async def init_redis(self):
        if self.redis is None:
            self.redis = await get_redis()
    
    async def get_position(self, ticker: str) -> Optional[Dict]:
        """Get position data from Redis"""
        await self.init_redis()
        try:
            position_data = await self.redis.get(f"position:{ticker}")
            if position_data:
                return json.loads(position_data)
            return None
        except Exception as e:
            dbg(f"❌ Redis get error: {e}")
            return None
    
    async def save_position(self, ticker: str, position_data: Dict):
        """Save position data to Redis"""
        await self.init_redis()
        try:
            await self.redis.set(f"position:{ticker}", json.dumps(position_data), ex=86400)  # 24h expiry
            dbg(f"💾 Position saved for {ticker}: {position_data}")
            
            # Add to price monitoring
            await price_monitor.add_symbol(ticker)
            
        except Exception as e:
            dbg(f"❌ Redis save error: {e}")
    
    async def delete_position(self, ticker: str):
        """Delete position from Redis"""
        await self.init_redis()
        try:
            await self.redis.delete(f"position:{ticker}")
            dbg(f"🗑️ Position deleted for {ticker}")
            
            # Remove from price monitoring
            await price_monitor.remove_symbol(ticker)
            
        except Exception as e:
            dbg(f"❌ Redis delete error: {e}")
    
    async def calculate_auto_trail(self, ticker: str, current_price: float) -> Optional[Dict]:
        """Calculate auto-trail logic"""
        position = await self.get_position(ticker)
        if not position:
            return None
        
        entry_price = position["entryPrice"]
        side = position["side"]
        arm_after_profit = position["armAfterProfit"]
        trail_step = position["trailStep"]
        
        # Calculate current profit in dollars
        if side == "buy":
            profit_dollars = (current_price - entry_price) * position["pointValue"] * position["quantity"]
        else:
            profit_dollars = (entry_price - current_price) * position["pointValue"] * position["quantity"]
        
        dbg(f"📈 Auto-trail check: {ticker} | Side: {side} | Entry: {entry_price} | Current: {current_price} | Profit: ${profit_dollars:.2f}")
        # Check if we should arm trailing or update trail
        if profit_dollars >= arm_after_profit:
            excess_profit = profit_dollars - arm_after_profit
            trail_increments = int(excess_profit / trail_step)

            # Lock in (arm_after_profit - trail_step) initially, then add trail_step increments
            locked_profit = (arm_after_profit - trail_step) + (trail_increments * trail_step)

            max_lockable = profit_dollars - trail_step
            locked_profit = min(locked_profit, max_lockable)

            if side == "buy":
                new_stop = entry_price + (locked_profit / (position["pointValue"] * position["quantity"]))
            else:
                new_stop = entry_price - (locked_profit / (position["pointValue"] * position["quantity"]))

            current_stop = position.get("currentStop")
            current_locked_profit = position.get("lockedProfit", 0)
            if current_stop is None or locked_profit > current_locked_profit:
                position["currentStop"] = new_stop
                position["lockedProfit"] = locked_profit
                await self.save_position(ticker, position)

                # Send updated stop to TradersPost
                stop_update_payload = {
                    "strategy_id": "Tiger-Alt",
                    "ticker": ticker,
                    "action": "sell" if side == "buy" else "buy",
                    "quantity": position["quantity"],
                    "orderType": "stop",
                    "stopPrice": new_stop,
                    "timeInForce": "gtc"
                }

                await send_traderspost(stop_update_payload)

        return None

position_manager = PositionManager()
price_monitor.position_manager = position_manager

# ─── SEND TO TRADERSPOST ───────────────────────────────────────────
async def send_traderspost(data: Dict):
    """Send data to TradersPost webhook"""
    dbg(f"📤 Sending to TradersPost: {TP_ALT_URL}")
    dbg(f"📤 Payload: {json.dumps(data, indent=2)}")
    
    if not TP_ALT_URL:
        dbg("❌ TradersPost URL not configured")
        return {"error": "TradersPost URL not configured"}
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(TP_ALT_URL, json=data)
            response.raise_for_status()
            response_data = response.json() if response.content else {}
            
            dbg(f"✅ TradersPost response ({response.status_code}): {response_data}")
            return {"success": True, "response": response_data, "status_code": response.status_code}
            
    except Exception as e:
        dbg(f"❌ TradersPost error: {str(e)}")
        return {"error": str(e)}

# ─── MAIN WEBHOOK ENDPOINT ─────────────────────────────────────────
@app.post("/pine-entry")
async def pine_entry(req: Request):
    """Main webhook endpoint for Pine Script alerts"""
    raw_body = await req.body()
    dbg(f"🔄 Raw webhook body: {raw_body}")
    
    try:
        # Parse JSON from body
        if raw_body.startswith(b'"') and raw_body.endswith(b'"'):
            # Handle escaped JSON string
            text = raw_body.decode("utf-8", errors="ignore").strip('"')
            data = json.loads(text)
        else:
            data = json.loads(raw_body)
        
        dbg(f"🔍 Parsed webhook data: {json.dumps(data, indent=2)}")
        
    except json.JSONDecodeError as e:
        dbg(f"❌ JSON decode error: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    # Extract basic fields
    strategy_id = data.get("strategy_id")
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    
    # Validate required fields
    if not strategy_id or not action or not ticker:
        dbg(f"❌ Missing required fields: strategy_id={strategy_id}, action={action}, ticker={ticker}")
        raise HTTPException(status_code=400, detail="Missing required fields: strategy_id, action, ticker")
    
    dbg(f"🎯 Processing: {strategy_id} | {action} | {ticker}")
    
    # Broadcast to WebSocket clients
    asyncio.create_task(broadcast_ws({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "action": action,
        "ticker": ticker,
        "data": data
    }))
    
    # ─── TIGER-ALT STRATEGY HANDLING ──────────────────────────────────
    if strategy_id == "Tiger-Alt":
        return await handle_tiger_alt(data)
    
    # ─── OTHER STRATEGIES ─────────────────────────────────────────────
    elif strategy_id == "Tiger-Core":
        return await forward_to_webhook(TP_CORE_URL, data, "Tiger-Core")
    elif strategy_id == "Tiger-Runner":
        return await forward_to_webhook(TP_RUNNER_URL, data, "Tiger-Runner")
    else:
        dbg(f"❌ Unknown strategy: {strategy_id}")
        raise HTTPException(status_code=400, detail=f"Unknown strategy_id: {strategy_id}")

async def handle_tiger_alt(data: Dict) -> Dict:
    """Handle Tiger-Alt strategy with auto-trail logic"""
    action = data.get("action", "").lower()
    ticker = data.get("ticker")
    
    dbg(f"🐅 Tiger-Alt handler: {action} | {ticker}")
    
    if action in ["buy", "sell"]:
        return await handle_tiger_alt_entry(data)
    elif action == "exit":
        return await handle_tiger_alt_exit(data)
    else:
        dbg(f"❌ Unknown action for Tiger-Alt: {action}")
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

async def handle_tiger_alt_entry(data: Dict) -> Dict:
    """Handle Tiger-Alt entry with auto-trail setup"""
    ticker = data.get("ticker")
    action = data.get("action")
    quantity = data.get("quantity")
    price = data.get("price")
    
    dbg(f"🚀 Tiger-Alt ENTRY: {ticker} | {action} | qty:{quantity} | price:{price}")
    
    # Extract auto-trail parameters
    auto_trail = data.get("autoTrail", {})
    arm_after_profit = auto_trail.get("armAfterProfit")
    trail_step = auto_trail.get("trailStep")
    hard_stop = auto_trail.get("hardStop")
    
    # Extract extras for debugging
    extras = data.get("extras", {})
    pts_hard = extras.get("ptsHard")
    pts_arm = extras.get("ptsArm")
    pts_trail = extras.get("ptsTrail")
    point_value = extras.get("pointValue")
    
    dbg(f"📊 Auto-trail params: arm=${arm_after_profit}, trail=${trail_step}, hard=${hard_stop}")
    dbg(f"📊 Points: hard={pts_hard}, arm={pts_arm}, trail={pts_trail}, ptVal={point_value}")
    
    # Validate required fields
    if not all([quantity, price, arm_after_profit, trail_step, hard_stop]):
        missing = [k for k, v in {
            "quantity": quantity, "price": price, "arm_after_profit": arm_after_profit,
            "trail_step": trail_step, "hard_stop": hard_stop
        }.items() if v is None]
        dbg(f"❌ Missing required fields: {missing}")
        raise HTTPException(status_code=400, detail=f"Missing required fields: {missing}")
    
    # Save position to Redis for auto-trail tracking
    position_data = {
        "ticker": ticker,
        "side": action,
        "entryPrice": price,
        "quantity": quantity,
        "armAfterProfit": arm_after_profit,
        "trailStep": trail_step,
        "hardStop": hard_stop,
        "pointValue": point_value or 5.0,  # Default for NQ
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "currentStop": None,
        "lockedProfit": 0
    }
    
    await position_manager.save_position(ticker, position_data)

    # Build TradersPost payload - NO AUTO-TRAIL DATA
    tp_payload = {
        "strategy_id": "Tiger-Alt",
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "orderType": "market",
        "timeInForce": "gtc",
        "sentiment": data.get("sentiment", "bullish" if action == "buy" else "bearish"),
        "price": price,
        "signalPrice": data.get("signalPrice", price),
        "extras": {
            "strategy": "Tiger-Alt-AutoTrail-Polygon"
        }
    }
    
    # Send to TradersPost
    result = await send_traderspost(tp_payload)

    # Send separate hard stop order
    hard_stop_payload = {
        "strategy_id": "Tiger-Alt",
        "ticker": ticker,
        "action": "sell" if action == "buy" else "buy",
        "quantity": quantity,
        "orderType": "stop",
        "stopPrice": hard_stop,
        "timeInForce": "gtc"
    }
    stop_result = await send_traderspost(hard_stop_payload)
    
    return {
        "status": "tiger_alt_entry_processed",
        "ticker": ticker,
        "action": action,
        "autoTrail": "enabled",
        "priceMonitoring": "polygon_websocket",
        "position_saved": True,
        "traderspost_result": result,
        "tp_payload": tp_payload
    }

async def handle_tiger_alt_exit(data: Dict) -> Dict:
    """Handle Tiger-Alt exit signal"""
    ticker = data.get("ticker")
    price = data.get("price")
    
    dbg(f"🚪 Tiger-Alt EXIT: {ticker} | price:{price}")
    
    # Get position info before deleting
    position = await position_manager.get_position(ticker)
    
    # Clean up position tracking
    await position_manager.delete_position(ticker)
    
    # Build exit payload
    tp_payload = {
        "strategy_id": "Tiger-Alt",
        "ticker": ticker,
        "action": "exit",
        "orderType": "market",
        "timeInForce": "gtc",
        "price": price,
        "signalPrice": data.get("signalPrice", price),
        "extras": {
            "exitReason": "pine_signal",
            "strategy": "Tiger-Alt-AutoTrail-Polygon"
        }
    }
    
    # Send to TradersPost
    result = await send_traderspost(tp_payload)
    
    return {
        "status": "tiger_alt_exit_processed",
        "ticker": ticker,
        "position_cleaned": position is not None,
        "traderspost_result": result,
        "tp_payload": tp_payload
    }

async def forward_to_webhook(url: str, data: Dict, strategy_name: str) -> Dict:
    """Forward data to other strategy webhooks"""
    if not url:
        dbg(f"❌ No URL configured for {strategy_name}")
        raise HTTPException(status_code=500, detail=f"No URL configured for {strategy_name}")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=data)
            response.raise_for_status()
            
            dbg(f"✅ Forwarded to {strategy_name}: {response.status_code}")
            return {
                "status": "forwarded",
                "to": strategy_name,
                "status_code": response.status_code
            }
    except Exception as e:
        dbg(f"❌ Error forwarding to {strategy_name}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error forwarding to {strategy_name}: {str(e)}")

# ─── PRICE MONITORING MANAGEMENT ──────────────────────────────────
@app.post("/monitor/start")
async def start_monitoring():
    """Start price monitoring"""
    if not price_monitor.is_connected:
        await price_monitor.connect()
    return {"status": "monitoring_started", "connected": price_monitor.is_connected}

@app.post("/monitor/stop")
async def stop_monitoring():
    """Stop price monitoring"""
    await price_monitor.disconnect()
    return {"status": "monitoring_stopped"}

@app.get("/monitor/status")
async def monitor_status():
    """Get monitoring status"""
    return {
        "connected": price_monitor.is_connected,
        "monitored_symbols": list(price_monitor.monitored_symbols),
        "reconnect_attempts": price_monitor.reconnect_attempts
    }

# ─── STARTUP EVENT ─────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    """Initialize connections on startup"""
    try:
        await get_redis()
        dbg("✅ Redis connection established")
        
        # Start Polygon.io monitoring
        await price_monitor.connect()
        
        # Load existing positions and start monitoring them
        r = await get_redis()
        keys = await r.keys("position:*")
        
        for key in keys:
            ticker = key.replace("position:", "")
            await price_monitor.add_symbol(ticker)
        
        dbg("🚀 Tiger-Alt Bot API v1.8 started successfully")
        dbg(f"📊 TradersPost URL: {TP_ALT_URL}")
        dbg(f"📊 Core URL: {TP_CORE_URL}")
        dbg(f"📊 Runner URL: {TP_RUNNER_URL}")
        dbg(f"📊 Polygon.io connected: {price_monitor.is_connected}")
        dbg(f"📊 Monitoring symbols: {list(price_monitor.monitored_symbols)}")
        
    except Exception as e:
        dbg(f"❌ Startup error: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Clean up connections on shutdown"""
    try:
        await price_monitor.disconnect()
        if redis_client:
            await redis_client.close()
        dbg("✅ Clean shutdown completed")
    except Exception as e:
        dbg(f"❌ Shutdown error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
