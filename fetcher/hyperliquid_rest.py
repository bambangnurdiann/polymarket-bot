"""
fetcher/hyperliquid_rest.py
===========================
REST fallback untuk mendapatkan harga BTC dari Hyperliquid.
Digunakan saat WebSocket belum connect atau stale.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

HYPERLIQUID_REST_URL = "https://api.hyperliquid.xyz/info"


def get_btc_price_rest(timeout: float = 3.0) -> float | None:
    """
    Ambil harga BTC dari Hyperliquid REST API.

    Returns:
        float: Harga BTC, atau None jika gagal
    """
    try:
        payload = {"type": "allMids"}
        resp = requests.post(HYPERLIQUID_REST_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        # Response: {"BTC": "94500.0", ...}
        btc_raw = data.get("BTC") or data.get("@107")
        if btc_raw:
            return float(btc_raw)
    except Exception as e:
        logger.debug(f"[HyperliquidREST] Error: {e}")
    return None


class HyperliquidREST:
    """REST poller sebagai fallback harga BTC."""

    def __init__(self, poll_interval: float = 2.0):
        self.poll_interval = poll_interval
        self.btc_price: float | None = None
        self.last_update: float = 0.0

    def update(self) -> bool:
        """
        Update harga BTC dari REST.
        Returns True jika berhasil.
        """
        now = time.time()
        if now - self.last_update < self.poll_interval:
            return self.btc_price is not None

        price = get_btc_price_rest()
        if price:
            self.btc_price = price
            self.last_update = now
            return True
        return False
