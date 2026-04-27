"""
fetcher/polymarket_scraper.py
==============================
Adapter layer antara bot_late.py dan polymarket_scraper.py (Selenium).

bot_late.py mengimport:
    from fetcher.polymarket_scraper import ScraperBeatSource

Kelas ScraperBeatSource di sini mem-wrap PolymarketScraper dari
polymarket_scraper.py agar kompatibel dengan interface yang dipakai
bot_late.py, yaitu:

    scraper_source = ScraperBeatSource(coins, retry_interval)
    price          = await scraper_source.try_get_beat(coin)
    scraper_source.on_new_window(coin)
    scraper_source.stop()
    scraper_source.status           → str (untuk dashboard)

CATATAN: polymarket_scraper.py TIDAK diubah sama sekali.
         Semua penyesuaian ada di file ini saja.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

# Import PolymarketScraper dari root project (polymarket_scraper.py)
# Pastikan polymarket_scraper.py ada di direktori yang sama dengan bot_late.py
# atau sudah ada di sys.path.
try:
    from polymarket_scraper import PolymarketScraper
except ImportError as e:
    raise ImportError(
        "Tidak dapat menemukan polymarket_scraper.py. "
        "Pastikan file polymarket_scraper.py ada di direktori root project "
        "(sejajar dengan bot_late.py)."
    ) from e


logger = logging.getLogger(__name__)


class ScraperBeatSource:
    """
    Wrapper async-compatible untuk PolymarketScraper.

    Digunakan oleh bot_late.py untuk mengambil beat price dari UI
    Polymarket via Selenium.

    Interface yang dipakai bot_late.py:
        - __init__(coins, retry_interval)
        - async try_get_beat(coin) -> Optional[float]
        - on_new_window(coin)
        - stop()
        - status  (property str)
    """

    def __init__(
        self,
        coins: List[str],
        retry_interval: float = 45.0,
        headless: bool = True,
    ):
        """
        Parameters
        ----------
        coins           : list coin yang aktif, misal ["BTC", "ETH"]
        retry_interval  : interval minimum (detik) antar scrape per coin
                          per window. Default 45 detik.
        headless        : jalankan Chrome headless. Default True (server).
                          Set False untuk debug visual.
        """
        self.coins          = [c.upper() for c in coins]
        self.retry_interval = retry_interval

        # Satu instance scraper, driver-nya di-share antar coin
        self._scraper = PolymarketScraper(headless=headless)

        # Tracking per coin: kapan terakhir scrape, hasil cache, sudah scrape window ini
        # { coin: { "last_try": float, "last_price": float|None, "window_done": bool } }
        self._state: Dict[str, Dict] = {
            coin: {"last_try": 0.0, "last_price": None, "window_done": False}
            for coin in self.coins
        }

        # Status string untuk dashboard
        self._status: str = "INIT"

        logger.info(
            f"[ScraperBeatSource] Init — coins={self.coins} "
            f"headless={headless} retry_interval={retry_interval}s"
        )

    # ── Public interface ──────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    async def try_get_beat(self, coin: str) -> Optional[float]:
        """
        Coba ambil beat price untuk coin dari Polymarket UI.

        Mengembalikan harga float jika berhasil, None jika gagal atau
        belum waktunya retry.

        Method ini aman dipanggil dari async context — blocking Selenium
        dijalankan di executor agar event loop tidak freeze.
        """
        coin = coin.upper()
        st   = self._state.get(coin)
        if st is None:
            logger.warning(f"[ScraperBeatSource] Coin {coin} tidak terdaftar")
            return None

        now = time.time()

        # Jika sudah berhasil dapat harga di window ini, kembalikan cache
        if st["window_done"] and st["last_price"] is not None:
            return st["last_price"]

        # Belum waktunya retry
        if now - st["last_try"] < self.retry_interval:
            return st["last_price"]  # bisa None kalau belum pernah berhasil

        st["last_try"] = now
        self._status   = f"SCRAPING {coin}"

        try:
            # Jalankan scrape di thread pool agar tidak block event loop
            price = await asyncio.get_event_loop().run_in_executor(
                None, self._scraper.get_price, coin
            )

            if price:
                st["last_price"]  = price
                st["window_done"] = True
                self._status = f"OK:{coin}=${price:,.2f}"
                logger.info(
                    f"[ScraperBeatSource] ✅ {coin} beat = ${price:,.2f}"
                )
            else:
                self._status = f"MISS:{coin}"
                logger.debug(f"[ScraperBeatSource] {coin} — scrape miss")

            return price

        except Exception as e:
            self._status = f"ERR:{coin}"
            logger.warning(f"[ScraperBeatSource] {coin} scrape error: {e}")
            return None

    def on_new_window(self, coin: str) -> None:
        """
        Dipanggil bot_late.py saat window baru dimulai untuk coin tertentu.
        Reset state sehingga scraper akan coba lagi di window baru.
        """
        coin = coin.upper()
        if coin in self._state:
            self._state[coin]["last_price"]  = None
            self._state[coin]["window_done"] = False
            self._state[coin]["last_try"]    = 0.0
            logger.debug(f"[ScraperBeatSource] {coin} — reset untuk window baru")

    def stop(self) -> None:
        """
        Tutup browser Selenium. Dipanggil bot_late.py saat shutdown.
        """
        try:
            self._scraper.close()
            self._status = "STOPPED"
            logger.info("[ScraperBeatSource] Selenium driver ditutup")
        except Exception as e:
            logger.warning(f"[ScraperBeatSource] Error saat close driver: {e}")
