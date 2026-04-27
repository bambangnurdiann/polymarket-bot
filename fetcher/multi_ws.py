"""
fetcher/multi_ws.py
===================
Single WebSocket ke Hyperliquid — data price, CVD, liquidation.

PATCH:
  - inject_chainlink_price(): sync harga Chainlink ke CoinDataStore
    agar get_price() return harga yang sama dengan Polymarket UI
  - add_large_trade(): proxy liquidation dari large trades (>$50k)
    karena Hyperliquid liquidation channel tidak reliable
  - get_price(): prioritas Chainlink jika fresh (<15s), fallback Hyperliquid
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Dict, Optional

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger(__name__)

HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"

COIN_SYMBOLS = {
    "BTC":  "BTC",
    "ETH":  "ETH",
    "SOL":  "SOL",
    "DOGE": "DOGE",
    "XRP":  "XRP",
}

# Threshold large trade proxy untuk liquidation detection
# Trade > nilai ini dianggap sebagai liquidation signal
LARGE_TRADE_THRESHOLD_USD = {
    "BTC":  50_000,
    "ETH":  20_000,
    "SOL":   5_000,
    "DOGE":  2_000,
    "XRP":   3_000,
}
DEFAULT_LARGE_TRADE = 10_000


class CoinDataStore:
    """
    Menyimpan semua data real-time untuk satu coin.
    """

    HYPERLIQUID_REST = "https://api.hyperliquid.xyz/info"

    def __init__(self, symbol: str):
        self.symbol = symbol

        # Harga dari Hyperliquid WS (untuk CVD reference)
        self.price:    Optional[float] = None
        self.price_ts: float = 0.0
        self._rest_ts: float = 0.0

        # Harga dari Chainlink (sync dengan Polymarket UI)
        self.chainlink_price:    Optional[float] = None
        self.chainlink_price_ts: float = 0.0

        # Liquidations: (timestamp, side, usd)
        self._liqs: deque = deque(maxlen=1000)

        # Trades untuk CVD: (timestamp, side, usd)
        self._trades: deque = deque(maxlen=5000)

        # Large trade threshold untuk proxy liquidation
        self._large_threshold = LARGE_TRADE_THRESHOLD_USD.get(symbol, DEFAULT_LARGE_TRADE)

    # ── Price ──────────────────────────────────────────────────

    def update_price(self, price: float) -> None:
        """Update harga dari Hyperliquid WebSocket."""
        self.price    = price
        self.price_ts = time.time()

    def update_chainlink_price(self, price: float) -> None:
        """Update harga dari Chainlink oracle (sinkron dengan Polymarket)."""
        self.chainlink_price    = price
        self.chainlink_price_ts = time.time()

    def fetch_price_rest(self) -> Optional[float]:
        """Fallback REST saat WS putus."""
        now = time.time()
        if now - self._rest_ts < 2:
            return self.price
        self._rest_ts = now
        try:
            import requests as _req
            resp = _req.post(
                self.HYPERLIQUID_REST,
                json={"type": "allMids"},
                timeout=3,
            )
            if resp.status_code == 200:
                mids = resp.json()
                raw  = mids.get(self.symbol)
                if raw:
                    price = float(raw)
                    self.update_price(price)
                    return price
        except Exception:
            pass
        return self.price

    def get_price(self) -> Optional[float]:
        """
        Ambil harga terbaik:
          1. Chainlink (fresh < 15s) — sama dengan Polymarket UI
          2. Hyperliquid WS (fresh < 10s)
          3. Hyperliquid REST fallback
        """
        # Chainlink fresh
        if self.chainlink_price and (time.time() - self.chainlink_price_ts) < 15:
            return self.chainlink_price
        # Hyperliquid WS
        if self.price and not self.price_stale:
            return self.price
        # Fallback REST
        return self.fetch_price_rest()

    def get_hyperliquid_price(self) -> Optional[float]:
        """Harga Hyperliquid murni — untuk CVD direction check."""
        if self.price and not self.price_stale:
            return self.price
        return self.fetch_price_rest()

    @property
    def price_stale(self) -> bool:
        return (time.time() - self.price_ts) > 10

    # ── Liquidations ───────────────────────────────────────────

    def add_liq(self, side: str, usd: float) -> None:
        """Tambah liquidation event. side: 'SHORT' atau 'LONG'."""
        if usd > 100:
            self._liqs.append((time.time(), side, usd))

    def add_large_trade(self, side: str, usd: float) -> None:
        """
        Large trade proxy untuk liquidation.
        Trade besar sering merupakan triggered liquidation.
        BUY besar  → SHORT squeeze  → liq side = SHORT
        SELL besar → LONG dump      → liq side = LONG
        """
        if usd >= self._large_threshold:
            liq_side = "SHORT" if side == "BUY" else "LONG"
            self._liqs.append((time.time(), liq_side, usd))

    def _liq_sum(self, side: str, seconds: float) -> float:
        cutoff = time.time() - seconds
        return sum(usd for ts, s, usd in self._liqs if s == side and ts >= cutoff)

    @property
    def liq_short_3s(self)  -> float: return self._liq_sum("SHORT", 3)
    @property
    def liq_long_3s(self)   -> float: return self._liq_sum("LONG", 3)
    @property
    def liq_short_30s(self) -> float: return self._liq_sum("SHORT", 30)
    @property
    def liq_long_30s(self)  -> float: return self._liq_sum("LONG", 30)

    def check_liq(self, direction: str, recent_min: float, sustained_min: float) -> tuple:
        if direction == "UP":
            r, s, label = self.liq_short_3s, self.liq_short_30s, "SHORT"
        else:
            r, s, label = self.liq_long_3s, self.liq_long_30s, "LONG"

        ok = r >= recent_min and s >= sustained_min
        if ok:
            return True, f"Liq {label}: 3s=${r/1000:.0f}k ✓ 30s=${s/1000:.0f}k ✓"
        elif r < recent_min:
            return False, f"Liq {label} 3s=${r/1000:.1f}k < ${recent_min/1000:.0f}k"
        else:
            return False, f"Liq {label} 30s=${s/1000:.0f}k < ${sustained_min/1000:.0f}k"

    # ── CVD ────────────────────────────────────────────────────

    def add_trade(self, side: str, usd: float) -> None:
        """Tambah trade untuk CVD. side: 'BUY' atau 'SELL'."""
        if usd > 0:
            self._trades.append((time.time(), side, usd))

    def _cvd(self, seconds: float) -> float:
        cutoff = time.time() - seconds
        total  = 0.0
        for ts, side, usd in self._trades:
            if ts < cutoff:
                continue
            total += usd if side == "BUY" else -usd
        return total

    def _fetch_cvd_rest(self) -> float:
        try:
            import requests as _req
            resp = _req.post(
                self.HYPERLIQUID_REST,
                json={"type": "recentTrades", "coin": self.symbol},
                timeout=3,
            )
            if resp.status_code == 200:
                trades = resp.json()
                cutoff = time.time() - 120
                cvd    = 0.0
                for t in trades:
                    ts   = float(t.get("time", 0)) / 1000
                    if ts < cutoff:
                        continue
                    side = "BUY" if t.get("side", "") == "B" else "SELL"
                    px   = float(t.get("px", 0))
                    sz   = float(t.get("sz", 0))
                    usd  = px * sz
                    cvd += usd if side == "BUY" else -usd
                    self._trades.append((ts, side, usd))
                return cvd
        except Exception:
            pass
        return 0.0

    @property
    def cvd_1min(self) -> float:
        cvd = self._cvd(60)
        return cvd if cvd != 0 else self._fetch_cvd_rest() / 2

    @property
    def cvd_2min(self) -> float:
        cvd = self._cvd(120)
        return cvd if cvd != 0 else self._fetch_cvd_rest()

    @property
    def cvd_5min(self) -> float:
        return self._cvd(300)

    def check_cvd(self, direction: str, threshold: float) -> tuple:
        cvd = self.cvd_2min
        if abs(cvd) < threshold:
            return False, f"CVD 2min=${cvd/1000:+.1f}k (min ±${threshold/1000:.0f}k)"
        if (direction == "UP" and cvd > 0) or (direction == "DOWN" and cvd < 0):
            return True, f"CVD 2min=${cvd/1000:+.0f}k ✓"
        return False, f"CVD 2min=${cvd/1000:+.0f}k berlawanan dgn {direction}"

    def signal_strength(self, direction: str) -> float:
        if direction == "UP":
            liq_r = self.liq_short_3s
            liq_s = self.liq_short_30s
        else:
            liq_r = self.liq_long_3s
            liq_s = self.liq_long_30s
        cvd   = abs(self.cvd_2min)
        score = (
            (liq_r / 15_000) * 0.3 +
            (liq_s / 50_000) * 0.4 +
            (cvd   / 25_000) * 0.3
        )
        return score


class MultiWS:
    """
    Single WebSocket ke Hyperliquid untuk semua coin.
    """

    def __init__(self, symbols: list):
        self.symbols = [s.upper() for s in symbols]
        self.coins: Dict[str, CoinDataStore] = {
            s: CoinDataStore(s) for s in self.symbols
        }
        self.is_connected = False
        self._task:        Optional[asyncio.Task] = None
        self._running      = False
        self.error_count   = 0

    def inject_chainlink_price(self, coin: str, price: float) -> None:
        """
        Inject harga Chainlink ke CoinDataStore.
        Dipanggil dari beat_sync_loop setiap 3 detik.
        """
        coin = coin.upper()
        if coin in self.coins and price and price > 0:
            self.coins[coin].update_chainlink_price(price)

    async def connect(self) -> None:
        if websockets is None:
            logger.error("[MultiWS] websockets tidak terinstall")
            return
        self._running = True
        self._task    = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        self._running = False
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
                self.error_count += 1
                logger.warning(f"[MultiWS] Reconnect dalam {delay}s: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            HYPERLIQUID_WS_URL,
            ping_interval=30,
            ping_timeout=20,
            close_timeout=10,
            max_size=10_000_000,
        ) as ws:
            self.is_connected = True
            self.error_count  = 0
            logger.info(f"[MultiWS] Connected — coins: {self.symbols}")

            # Subscribe allMids
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "allMids"}
            }))

            # Subscribe trades per coin
            for sym in self.symbols:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": sym}
                }))

            # Subscribe liquidations (bonus)
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "liquidations"}
            }))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._dispatch(raw)
                except Exception as e:
                    logger.debug(f"[MultiWS] Parse error: {e}")

    def _dispatch(self, raw: str) -> None:
        data = json.loads(raw)
        ch   = data.get("channel", "")

        # allMids: update harga Hyperliquid semua coin
        if ch == "allMids":
            mids = data.get("data", {}).get("mids", {})
            for sym in self.symbols:
                price_raw = mids.get(sym)
                if price_raw:
                    self.coins[sym].update_price(float(price_raw))
            return

        # trades: update CVD + large trade proxy
        if ch == "trades":
            trades = data.get("data", [])
            if not isinstance(trades, list):
                trades = [trades]
            for t in trades:
                coin = t.get("coin", "").upper()
                if coin not in self.coins:
                    continue
                raw_side = t.get("side", "")
                side     = "BUY" if raw_side == "B" else "SELL"
                px       = float(t.get("px", 0))
                sz       = float(t.get("sz", 0))
                usd      = px * sz

                # CVD
                self.coins[coin].add_trade(side, usd)

                # Large trade proxy untuk liquidation
                self.coins[coin].add_large_trade(side, usd)

                # Explicit liquidation flag
                if t.get("liquidation", False):
                    liq_side = "SHORT" if raw_side == "B" else "LONG"
                    self.coins[coin].add_liq(liq_side, usd)
            return

        # liquidations: bonus channel
        if ch == "liquidations":
            events = data.get("data", [])
            if not isinstance(events, list):
                events = [events]
            for ev in events:
                coin = ev.get("coin", "").upper()
                if coin not in self.coins:
                    continue
                raw_side = ev.get("side", "").upper()
                if raw_side in ("A", "SELL", "LONG"):
                    side = "LONG"
                elif raw_side in ("B", "BUY", "SHORT"):
                    side = "SHORT"
                else:
                    continue
                px  = float(ev.get("px", ev.get("price", 0)))
                sz  = float(ev.get("sz", ev.get("size", 0)))
                usd = sz * px if px > 0 else sz
                self.coins[coin].add_liq(side, usd)

    @property
    def status(self) -> str:
        return "OK" if self.is_connected else "ERR"
