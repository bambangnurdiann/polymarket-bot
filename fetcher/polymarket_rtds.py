"""
fetcher/polymarket_rtds.py
==========================
Subscribe ke Polymarket Real-Time Data Socket (RTDS) untuk harga Chainlink.

Endpoint: wss://ws-live-data.polymarket.com
Topic   : crypto_prices_chainlink

Ini adalah SUMBER YANG SAMA dengan yang Polymarket tampilkan di UI!
Jauh lebih akurat dari query Chainlink Polygon RPC langsung.

Docs: https://docs.polymarket.com/market-data/websocket/rtds
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger(__name__)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

# Mapping nama coin ke format Polymarket Chainlink (slash-separated)
COIN_TO_CHAINLINK_SYMBOL = {
    "BTC":  "btc/usd",
    "ETH":  "eth/usd",
    "SOL":  "sol/usd",
    "XRP":  "xrp/usd",
    "DOGE": "doge/usd",
}


class PolymarketRTDS:
    """
    WebSocket client ke Polymarket RTDS untuk harga Chainlink real-time.

    Ini adalah harga yang PERSIS sama dengan yang Polymarket lihat —
    bukan dari Polygon RPC yang bisa lag/berbeda.

    Attributes:
        prices      : Dict[str, float] — harga terbaru per coin (uppercase key)
        price_ts    : Dict[str, float] — timestamp update per coin
        is_connected: bool
    """

    def __init__(self, coins: list):
        self.coins       = [c.upper() for c in coins]
        self.prices:     Dict[str, Optional[float]] = {c: None for c in self.coins}
        self.price_ts:   Dict[str, float]           = {c: 0.0  for c in self.coins}
        self.is_connected = False
        self._ws          = None
        self._task        = None
        self._running     = False
        self._ping_task   = None

    async def start(self) -> None:
        if websockets is None:
            logger.error("[RTDS] websockets tidak terinstall: pip install websockets")
            return
        self._running = True
        self._task    = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        self.is_connected = False

    async def _run(self) -> None:
        delay = 2
        while self._running:
            try:
                await self._connect_once()
                delay = 2
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.is_connected = False
                logger.warning(f"[RTDS] Reconnect dalam {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            RTDS_WS_URL,
            ping_interval=None,  # kita handle manual ping
            close_timeout=10,
        ) as ws:
            self._ws          = ws
            self.is_connected = True
            logger.info(f"[RTDS] Connected — subscribing Chainlink for {self.coins}")

            # Subscribe Chainlink per coin
            for coin in self.coins:
                cl_symbol = COIN_TO_CHAINLINK_SYMBOL.get(coin)
                if not cl_symbol:
                    continue

                sub_msg = json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic":   "crypto_prices_chainlink",
                        "type":    "*",
                        "filters": json.dumps({"symbol": cl_symbol}),
                    }]
                })
                await ws.send(sub_msg)

            # Start ping loop (wajib setiap 5 detik per dokumentasi)
            self._ping_task = asyncio.create_task(self._ping_loop(ws))

            try:
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        self._handle(raw)
                    except Exception as e:
                        logger.debug(f"[RTDS] Parse error: {e}")
            finally:
                if self._ping_task:
                    self._ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        """Kirim PING setiap 5 detik untuk keep-alive."""
        while True:
            try:
                await ws.send("PING")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception:
                break

    def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return  # bukan JSON (misal PONG response)

        topic   = data.get("topic", "")
        payload = data.get("payload", {})

        if topic != "crypto_prices_chainlink":
            return

        symbol = payload.get("symbol", "").upper()
        value  = payload.get("value")
        ts_ms  = payload.get("timestamp", 0)

        if not value or not symbol:
            return

        # Konversi "BTC/USD" → "BTC"
        coin = symbol.split("/")[0].upper()
        if coin not in self.coins:
            return

        self.prices[coin]   = float(value)
        self.price_ts[coin] = ts_ms / 1000.0 if ts_ms > 1e10 else ts_ms

        logger.debug(f"[RTDS] {coin} = ${float(value):,.2f}")

    def get_price(self, coin: str) -> Optional[float]:
        return self.prices.get(coin.upper())

    def get_price_age(self, coin: str) -> float:
        ts = self.price_ts.get(coin.upper(), 0)
        if not ts:
            return 999.0
        return time.time() - ts

    def is_fresh(self, coin: str, max_age: float = 15.0) -> bool:
        return self.get_price_age(coin) <= max_age

    @property
    def status(self) -> str:
        return "OK" if self.is_connected else "ERR"