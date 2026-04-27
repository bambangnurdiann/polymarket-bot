"""
fetcher/vatic_beat.py
=====================
Ambil "price to beat" langsung dari Vatic API.
Ini adalah sumber paling akurat — sama persis dengan yang ditampilkan
di Polymarket UI, karena Vatic track Chainlink + Polymarket pipeline.

Endpoint: https://api.vatic.trading/api/v1/targets/timestamp
Params  : asset=btc, type=5min, timestamp=<unix_window_start>
"""

import logging
import time
import requests
from typing import Optional

logger = logging.getLogger(__name__)

VATIC_URL  = "https://api.vatic.trading/api/v1/targets/timestamp"
COIN_MAP   = {
    "BTC":  "btc",
    "ETH":  "eth",
    "SOL":  "sol",
    "XRP":  "xrp",
    "DOGE": "doge",
}
WINDOW_DURATION = 300  # 5 menit


def get_window_start_ts(now: float = None) -> int:
    """Hitung unix timestamp awal window 5 menit saat ini."""
    if now is None:
        now = time.time()
    return int((now // WINDOW_DURATION) * WINDOW_DURATION)


def fetch_vatic_beat(
    coin:       str,
    window_ts:  int  = None,
    timeout:    float = 4.0,
) -> Optional[float]:
    """
    Ambil price to beat dari Vatic API untuk window tertentu.

    Args:
        coin      : "BTC", "ETH", dll
        window_ts : Unix timestamp awal window (default = window saat ini)
        timeout   : Request timeout dalam detik

    Returns:
        float harga beat, atau None jika gagal
    """
    coin_lower = COIN_MAP.get(coin.upper(), coin.lower())
    if window_ts is None:
        window_ts = get_window_start_ts()

    params = {
        "asset":     coin_lower,
        "type":      "5min",
        "timestamp": window_ts,
    }

    try:
        resp = requests.get(VATIC_URL, params=params, timeout=timeout)
        if resp.status_code != 200:
            logger.debug(f"[Vatic] HTTP {resp.status_code} untuk {coin} ts={window_ts}")
            return None

        data = resp.json()

        # Response: {"price": 94123.45} atau {"target": 94123.45}
        price = (
            data.get("price")
            or data.get("target")
            or data.get("beat_price")
            or data.get("value")
        )

        if price and float(price) > 0:
            val = float(price)
            logger.info(f"[Vatic] ✅ {coin} beat price: ${val:,.2f} (ts={window_ts})")
            return val

        logger.debug(f"[Vatic] Response tidak punya price: {data}")
        return None

    except requests.exceptions.Timeout:
        logger.debug(f"[Vatic] Timeout untuk {coin}")
        return None
    except Exception as e:
        logger.debug(f"[Vatic] Error untuk {coin}: {e}")
        return None


class VaticBeatFetcher:
    """
    Beat price fetcher dengan caching per window.
    Otomatis refresh saat window baru dimulai.
    """

    def __init__(self, poll_on_start: bool = True):
        self._cache: dict = {}       # coin -> (window_ts, beat_price)
        self._fail_count: dict = {}  # coin -> consecutive failures

    def get_beat(self, coin: str, force: bool = False) -> Optional[float]:
        """
        Ambil beat price untuk window saat ini.
        Cache per window — tidak re-fetch jika window sama.
        """
        coin     = coin.upper()
        win_ts   = get_window_start_ts()
        cached   = self._cache.get(coin)

        # Return cache jika masih window yang sama
        if not force and cached and cached[0] == win_ts and cached[1]:
            return cached[1]

        # Fetch baru
        price = fetch_vatic_beat(coin, win_ts)

        if price:
            self._cache[coin]     = (win_ts, price)
            self._fail_count[coin] = 0
            return price
        else:
            # Increment failure counter
            self._fail_count[coin] = self._fail_count.get(coin, 0) + 1
            # Return cache lama jika ada (window sebelumnya) sebagai fallback
            if cached and cached[1]:
                logger.warning(
                    f"[Vatic] {coin} fetch gagal (attempt {self._fail_count[coin]}), "
                    f"pakai cache lama: ${cached[1]:,.2f}"
                )
                return cached[1]
            return None

    def get_status(self, coin: str) -> str:
        coin   = coin.upper()
        cached = self._cache.get(coin)
        if not cached:
            return "NO_DATA"
        win_ts, price = cached
        cur_ts = get_window_start_ts()
        if win_ts == cur_ts:
            return f"✅ ${price:,.2f} (window ini)"
        else:
            age_windows = (cur_ts - win_ts) // WINDOW_DURATION
            return f"⚠️ ${price:,.2f} (dari {age_windows} window lalu)"