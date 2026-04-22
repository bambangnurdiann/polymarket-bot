"""
fetcher/hyperliquid_ws.py
=========================
WebSocket client untuk mendapatkan harga BTC real-time dari Hyperliquid.

Hyperliquid adalah DEX perpetual dengan feed harga BTC yang cepat dan akurat.
Bot menggunakan harga ini untuk menentukan posisi relatif terhadap beat price.

Cara pakai:
    ws = HyperliquidWS()
    await ws.connect()
    price = ws.btc_price   # float, harga BTC terkini
    await ws.disconnect()
"""

import asyncio
import json
import logging
import time

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger(__name__)

HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidWS:
    """
    WebSocket client Hyperliquid untuk harga BTC real-time.

    Attributes:
        btc_price      : float  — Harga BTC terkini (None jika belum connect)
        last_update    : float  — Unix timestamp update terakhir
        is_connected   : bool   — Status koneksi WebSocket
        error_count    : int    — Jumlah error sejak terakhir connect
    """

    def __init__(self):
        self.btc_price: float | None = None
        self.last_update: float = 0.0
        self.is_connected: bool = False
        self.error_count: int = 0
        self._ws = None
        self._task = None
        self._running = False

    async def connect(self) -> None:
        """Mulai koneksi WebSocket dan subscribe ke feed BTC."""
        if websockets is None:
            logger.error("[HyperliquidWS] websockets library tidak terinstall")
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        """Tutup koneksi WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
        self.is_connected = False

    async def _run(self) -> None:
        """Loop utama WebSocket dengan auto-reconnect."""
        reconnect_delay = 2
        while self._running:
            try:
                await self._connect_once()
                reconnect_delay = 2  # Reset delay jika sukses
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.error_count += 1
                self.is_connected = False
                logger.warning(f"[HyperliquidWS] Disconnected: {e}. Reconnect dalam {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def _connect_once(self) -> None:
        """Satu sesi koneksi WebSocket."""
        async with websockets.connect(
            HYPERLIQUID_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self.is_connected = True
            self.error_count = 0
            logger.info("[HyperliquidWS] Connected")

            # Subscribe ke mid price BTC
            subscribe_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "allMids"
                }
            }
            await ws.send(json.dumps(subscribe_msg))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle_message(raw)
                except Exception as e:
                    logger.debug(f"[HyperliquidWS] Parse error: {e}")

    def _handle_message(self, raw: str) -> None:
        """Parse pesan WebSocket dan update harga BTC."""
        data = json.loads(raw)

        # Format allMids: {"channel": "allMids", "data": {"mids": {"BTC": "94500.0", ...}}}
        if data.get("channel") == "allMids":
            mids = data.get("data", {}).get("mids", {})
            btc_raw = mids.get("BTC") or mids.get("@107")  # @107 = BTC di Hyperliquid
            if btc_raw:
                self.btc_price = float(btc_raw)
                self.last_update = time.time()
                return

        # Format l2Book / trades fallback
        if data.get("channel") == "trades":
            trades = data.get("data", [])
            if trades:
                coin = trades[0].get("coin", "")
                if coin == "BTC":
                    self.btc_price = float(trades[0].get("px", 0))
                    self.last_update = time.time()

    @property
    def is_stale(self) -> bool:
        """True jika data lebih dari 10 detik yang lalu."""
        return (time.time() - self.last_update) > 10

    @property
    def status(self) -> str:
        """Status string untuk dashboard."""
        if not self.is_connected:
            return "ERR"
        if self.is_stale:
            return "STALE"
        return "OK"
