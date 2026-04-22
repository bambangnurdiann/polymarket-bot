"""
fetcher/cvd_tracker.py
======================
Cumulative Volume Delta (CVD) tracker untuk BTC dari Hyperliquid.

CVD = selisih kumulatif antara volume beli agresif dan volume jual agresif.
  - CVD naik  → lebih banyak market buy → tekanan beli → bullish
  - CVD turun → lebih banyak market sell → tekanan jual → bearish

Late Bot menggunakan CVD 2 menit:
  - Threshold: |cvd_2min| >= $25,000
  - Arah CVD harus sesuai dengan arah sinyal

Cara hitung CVD dari trades:
  - Trade dengan side "BUY"  (taker beli)  → tambah ke CVD
  - Trade dengan side "SELL" (taker jual)  → kurangi dari CVD
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


class TradeEvent:
    """Satu trade event."""
    __slots__ = ("timestamp", "side", "size_usd")

    def __init__(self, timestamp: float, side: str, size_usd: float):
        self.timestamp = timestamp
        self.side      = side      # "BUY" atau "SELL"
        self.size_usd  = size_usd


class CVDTracker:
    """
    Cumulative Volume Delta tracker real-time dari Hyperliquid trades.

    Attributes:
        is_connected : bool
        cvd_2min     : float  — CVD 2 menit terakhir (positif = bullish)
        cvd_5min     : float  — CVD 5 menit terakhir
        cvd_1min     : float  — CVD 1 menit terakhir
        total_volume_2min : float — Total volume 2 menit (untuk context)
    """

    def __init__(self, max_history: int = 2000):
        self._trades: deque = deque(maxlen=max_history)
        self.is_connected = False
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._shares_ws = False  # True jika WebSocket sudah dibagi dengan HyperliquidWS

    def feed_trade(self, side: str, size_usd: float) -> None:
        """
        Feed trade dari sumber eksternal (jika WebSocket sudah dibuka di tempat lain).
        Berguna untuk menghindari double connect.
        """
        self._trades.append(TradeEvent(time.time(), side, size_usd))

    async def connect(self) -> None:
        """Buka koneksi WebSocket sendiri untuk trades."""
        if websockets is None:
            logger.error("[CVDTracker] websockets tidak terinstall")
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
        delay = 2
        while self._running:
            try:
                await self._connect_once()
                delay = 2
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.is_connected = False
                logger.debug(f"[CVDTracker] Reconnect dalam {delay}s: {e}")
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
            logger.info("[CVDTracker] Connected")

            sub = {
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": "BTC"}
            }
            await ws.send(json.dumps(sub))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle(raw)
                except Exception as e:
                    logger.debug(f"[CVDTracker] Parse error: {e}")

    def _handle(self, raw: str) -> None:
        data = json.loads(raw)
        if data.get("channel") != "trades":
            return

        trades = data.get("data", [])
        if not isinstance(trades, list):
            trades = [trades]

        for t in trades:
            coin = t.get("coin", "")
            if coin != "BTC":
                continue

            # Hyperliquid: side "B" = buyer is taker (market buy), "A" = seller is taker (market sell)
            raw_side = t.get("side", "")
            side = "BUY" if raw_side == "B" else "SELL"

            px  = float(t.get("px", 0))
            sz  = float(t.get("sz", 0))
            usd = sz * px

            if usd > 0:
                self._trades.append(TradeEvent(time.time(), side, usd))

    def _cvd_window(self, seconds: float) -> float:
        """Hitung CVD dalam window N detik terakhir."""
        cutoff = time.time() - seconds
        cvd = 0.0
        for t in self._trades:
            if t.timestamp < cutoff:
                continue
            if t.side == "BUY":
                cvd += t.size_usd
            else:
                cvd -= t.size_usd
        return cvd

    def _volume_window(self, seconds: float) -> float:
        """Total volume dalam window N detik terakhir."""
        cutoff = time.time() - seconds
        return sum(t.size_usd for t in self._trades if t.timestamp >= cutoff)

    @property
    def cvd_1min(self) -> float:
        return self._cvd_window(60)

    @property
    def cvd_2min(self) -> float:
        return self._cvd_window(120)

    @property
    def cvd_5min(self) -> float:
        return self._cvd_window(300)

    @property
    def total_volume_2min(self) -> float:
        return self._volume_window(120)

    def check_signal(
        self,
        direction: str,
        threshold: float = 25_000,
    ) -> tuple[bool, str]:
        """
        Cek apakah CVD 2 menit mendukung arah sinyal.

        Args:
            direction : "UP" atau "DOWN"
            threshold : minimum |CVD| dalam USD

        Returns:
            (pass: bool, reason: str)
        """
        cvd = self.cvd_2min
        abs_cvd = abs(cvd)

        # CVD harus cukup besar
        if abs_cvd < threshold:
            return False, f"CVD 2min=${cvd/1000:+.1f}k (min ±${threshold/1000:.0f}k)"

        # Arah CVD harus sesuai sinyal
        if direction == "UP" and cvd > 0:
            return True, f"CVD 2min=${cvd/1000:+.0f}k ✓ (bullish)"
        elif direction == "DOWN" and cvd < 0:
            return True, f"CVD 2min=${cvd/1000:+.0f}k ✓ (bearish)"
        else:
            direction_cvd = "bullish" if cvd > 0 else "bearish"
            return False, f"CVD 2min=${cvd/1000:+.0f}k berlawanan ({direction_cvd} vs sinyal {direction})"

    @property
    def status(self) -> str:
        return "OK" if self.is_connected else "ERR"

    def summary(self) -> str:
        return (
            f"CVD 1m=${self.cvd_1min/1000:+.0f}k "
            f"2m=${self.cvd_2min/1000:+.0f}k "
            f"5m=${self.cvd_5min/1000:+.0f}k"
        )
