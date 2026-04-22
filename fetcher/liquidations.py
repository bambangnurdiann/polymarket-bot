"""
fetcher/liquidations.py
=======================
Tracker data likuidasi BTC dari Hyperliquid WebSocket.

Liquidation = posisi trader yang di-force close oleh exchange karena margin habis.
- Likuidasi SHORT besar → tekanan beli mendadak → harga cenderung naik (signal UP)
- Likuidasi LONG besar  → tekanan jual mendadak → harga cenderung turun (signal DOWN)

Late Bot menggunakan 2 window:
  - recent  (3s)  : likuidasi sangat baru, menangkap momen spike
  - sustained (30s): likuidasi berkelanjutan, konfirmasi momentum

Threshold dari screenshot:
  - recent(3s)  >= $15,000
  - sustained(30s) >= $50,000
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger(__name__)

HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"


class LiquidationEvent:
    """Satu event likuidasi."""
    __slots__ = ("timestamp", "side", "size_usd", "price")

    def __init__(self, timestamp: float, side: str, size_usd: float, price: float):
        self.timestamp = timestamp  # unix time
        self.side      = side       # "SHORT" atau "LONG" (posisi yang dilikuidasi)
        self.size_usd  = size_usd   # nilai USD
        self.price     = price


class LiquidationTracker:
    """
    Tracker real-time likuidasi BTC dari Hyperliquid.

    Attributes:
        is_connected : bool
        liq_short_3s : float  — Total likuidasi SHORT dalam 3 detik terakhir
        liq_long_3s  : float  — Total likuidasi LONG dalam 3 detik terakhir
        liq_short_30s: float  — Total likuidasi SHORT dalam 30 detik terakhir
        liq_long_30s : float  — Total likuidasi LONG dalam 30 detik terakhir
    """

    def __init__(self, max_history: int = 500):
        self._events: deque = deque(maxlen=max_history)
        self.is_connected = False
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def connect(self) -> None:
        if websockets is None:
            logger.error("[LiqTracker] websockets tidak terinstall")
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except:
                pass
        if self._task:
            self._task.cancel()
        self.is_connected = False

    async def _run(self) -> None:
        """Loop dengan auto-reconnect."""
        delay = 2
        while self._running:
            try:
                await self._connect_once()
                delay = 2
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.is_connected = False
                logger.debug(f"[LiqTracker] Reconnect dalam {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            HYPERLIQUID_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            self.is_connected = True
            logger.info("[LiqTracker] Connected")

            # Subscribe ke liquidations feed
            sub = {
                "method": "subscribe",
                "subscription": {"type": "liquidations"}
            }
            await ws.send(json.dumps(sub))

            # Juga subscribe ke trades untuk detect large market orders
            # yang merupakan proxy likuidasi di beberapa exchange
            sub2 = {
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": "BTC"}
            }
            await ws.send(json.dumps(sub2))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle(raw)
                except Exception as e:
                    logger.debug(f"[LiqTracker] Parse error: {e}")

    def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        channel = data.get("channel", "")

        # Format liquidations: {"channel": "liquidations", "data": [...]}
        if channel == "liquidations":
            events = data.get("data", [])
            if not isinstance(events, list):
                events = [events]
            for ev in events:
                coin = ev.get("coin", "")
                if coin != "BTC":
                    continue
                # side = posisi yang dilikuidasi
                # "long" dilikuidasi = LONG blow up
                # "short" dilikuidasi = SHORT blow up
                raw_side = ev.get("side", "").upper()
                # Hyperliquid: "A" = ask/sell (long dilikuidasi), "B" = bid/buy (short dilikuidasi)
                if raw_side in ("A", "SELL", "LONG"):
                    side = "LONG"
                elif raw_side in ("B", "BUY", "SHORT"):
                    side = "SHORT"
                else:
                    side = raw_side

                price    = float(ev.get("px", ev.get("price", 0)))
                size     = float(ev.get("sz", ev.get("size", 0)))
                size_usd = size * price if price > 0 else size

                if size_usd > 100:  # filter noise
                    self._events.append(
                        LiquidationEvent(time.time(), side, size_usd, price)
                    )

        # Fallback: trades besar sebagai proxy likuidasi
        elif channel == "trades":
            trades = data.get("data", [])
            if not isinstance(trades, list):
                trades = [trades]
            for t in trades:
                coin = t.get("coin", "")
                if coin != "BTC":
                    continue
                # Cek apakah ini liquidation trade
                if not t.get("liquidation", False):
                    continue
                side_raw = t.get("side", "").upper()
                side = "SHORT" if side_raw == "B" else "LONG"
                px   = float(t.get("px", 0))
                sz   = float(t.get("sz", 0))
                usd  = sz * px
                if usd > 100:
                    self._events.append(
                        LiquidationEvent(time.time(), side, usd, px)
                    )

    def _sum_window(self, side: str, seconds: float) -> float:
        """Hitung total likuidasi sisi tertentu dalam N detik terakhir."""
        cutoff = time.time() - seconds
        return sum(
            e.size_usd for e in self._events
            if e.side == side and e.timestamp >= cutoff
        )

    @property
    def liq_short_3s(self) -> float:
        return self._sum_window("SHORT", 3)

    @property
    def liq_long_3s(self) -> float:
        return self._sum_window("LONG", 3)

    @property
    def liq_short_30s(self) -> float:
        return self._sum_window("SHORT", 30)

    @property
    def liq_long_30s(self) -> float:
        return self._sum_window("LONG", 30)

    def check_signal(
        self,
        direction: str,
        recent_threshold: float = 15_000,
        sustained_threshold: float = 50_000,
    ) -> tuple[bool, str]:
        """
        Cek apakah liquidation data mendukung arah sinyal.

        Args:
            direction: "UP" atau "DOWN"
            recent_threshold: min likuidasi sisi lawan dalam 3s
            sustained_threshold: min likuidasi sisi lawan dalam 30s

        Returns:
            (pass: bool, reason: str)
        """
        if direction == "UP":
            # Untuk UP: butuh SHORT yang dilikuidasi (squeeze)
            recent    = self.liq_short_3s
            sustained = self.liq_short_30s
            label     = "SHORT"
        else:
            # Untuk DOWN: butuh LONG yang dilikuidasi
            recent    = self.liq_long_3s
            sustained = self.liq_long_30s
            label     = "LONG"

        ok_recent    = recent    >= recent_threshold
        ok_sustained = sustained >= sustained_threshold

        if ok_recent and ok_sustained:
            return True, f"Liq {label}: 3s=${recent/1000:.0f}k ✓, 30s=${sustained/1000:.0f}k ✓"
        elif not ok_recent:
            return False, f"Liq {label} 3s=${recent/1000:.1f}k < ${recent_threshold/1000:.0f}k"
        else:
            return False, f"Liq {label} 30s=${sustained/1000:.0f}k < ${sustained_threshold/1000:.0f}k"

    @property
    def status(self) -> str:
        return "OK" if self.is_connected else "ERR"
