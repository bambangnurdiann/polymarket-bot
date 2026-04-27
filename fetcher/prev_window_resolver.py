"""
fetcher/prev_window_resolver.py
================================
Ambil final price dari window sebelumnya sebagai beat price window berikutnya.

KONSEP:
  Polymarket menentukan hasil UP/DOWN berdasarkan harga Chainlink
  di saat window close. Final price ini bisa diambil dari API
  setelah resolusi (biasanya delay 20-60 detik setelah close).

STRATEGI:
  1. Window baru mulai (misal 12:10:00)
  2. Tunggu 30-60 detik (Polymarket finalize window 12:05-12:10)
  3. Query Gamma API untuk resolved price window 12:05-12:10
  4. Gunakan sebagai beat price window 12:10-12:15
  5. Bet di 30 detik terakhir dengan beat price akurat

MENGAPA INI LEBIH BAIK:
  - Harga exact yang Polymarket gunakan untuk resolve
  - Tidak bergantung pada Chainlink real-time (sering drift/delay)
  - Tidak perlu sync/calibrate — ini ground truth
  - Sesuai dengan cara Polymarket benar-benar menentukan pemenang
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_BASE_URL  = "https://data-api.polymarket.com"

WINDOW_DURATION = 300  # 5 menit

# Cache: simpan resolved price per window_id agar tidak query berulang
_resolved_cache: Dict[str, Tuple[float, float]] = {}  # window_id -> (price, fetched_at)


def get_prev_window_timestamps(now: float = None) -> Tuple[int, int, str]:
    """
    Hitung timestamp untuk window SEBELUMNYA.

    Returns:
        (prev_start_ts, prev_end_ts, prev_window_id)
    """
    if now is None:
        now = time.time()
    cur_start  = int(now // WINDOW_DURATION) * WINDOW_DURATION
    prev_start = cur_start - WINDOW_DURATION
    prev_end   = cur_start  # = current window start

    dt        = datetime.fromtimestamp(prev_start, tz=timezone.utc)
    window_id = dt.strftime("%Y%m%d-%H%M")

    return prev_start, prev_end, window_id


def fetch_resolved_price_from_gamma(
    coin: str,
    prev_start_ts: int,
    prev_end_ts:   int,
    timeout:       float = 5.0,
) -> Optional[float]:
    """
    Coba ambil resolved price dari Gamma API.

    Gamma API menyimpan outcomePrices setelah market resolved.
    Kita cari market yang windownya = window sebelumnya.
    """
    coin = coin.upper()
    kw_list = {
        "BTC":  ["bitcoin", "btc"],
        "ETH":  ["ethereum", "eth"],
        "SOL":  ["solana", "sol"],
        "DOGE": ["dogecoin", "doge"],
        "XRP":  ["xrp", "ripple"],
    }.get(coin, [coin.lower()])

    # Generate slug candidates (Polymarket pakai ET timezone untuk slug)
    slug_candidates = []
    for base_ts in [prev_start_ts, prev_end_ts]:
        for et_offset in [0, -14400, -18000]:  # UTC, ET summer, ET winter
            adj_ts = int((base_ts + et_offset) // WINDOW_DURATION) * WINDOW_DURATION
            slug_candidates.append(f"{coin.lower()}-updown-5m-{adj_ts}")
            slug_candidates.append(f"{coin.lower()}-updown-5m-{adj_ts + WINDOW_DURATION}")
            slug_candidates.append(f"{coin.lower()}-updown-5m-{adj_ts - WINDOW_DURATION}")

    # Deduplicate
    seen = set()
    slugs = []
    for s in slug_candidates:
        if s not in seen:
            seen.add(s)
            slugs.append(s)

    # Coba via slug (events endpoint)
    for slug in slugs:
        try:
            resp = requests.get(
                f"{GAMMA_BASE_URL}/events",
                params={"slug": slug},
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue
            events = resp.json()
            if not isinstance(events, list) or not events:
                continue

            ev      = events[0]
            markets = ev.get("markets", [])
            if not markets:
                continue

            m = markets[0]

            # Cek apakah market ini sudah closed/resolved
            if not m.get("closed", False) and not m.get("resolved", False):
                logger.debug(f"[PrevWindow] Market {slug} belum resolved, skip")
                continue

            # Ambil outcome prices — setelah resolved, salah satu = ~1.0
            raw_prices   = m.get("outcomePrices", "[]")
            raw_outcomes = m.get("outcomes", "[]")
            prices   = json.loads(raw_prices)   if isinstance(raw_prices, str)   else raw_prices
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

            if not prices or len(prices) < 2:
                continue

            # Cek apakah ada winner (outcome price ~1.0)
            for i, p in enumerate(prices):
                try:
                    if float(p) >= 0.95:
                        # Market ini sudah resolved
                        # Ambil groupItemThreshold = strike price = final Chainlink price
                        strike = _extract_strike(m, ev)
                        if strike:
                            logger.info(
                                f"[PrevWindow] ✅ {coin} resolved via slug={slug} "
                                f"| strike=${strike:,.2f}"
                            )
                            return strike
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"[PrevWindow] Slug {slug} error: {e}")
            continue

    return None


def fetch_resolved_price_direct(
    coin:          str,
    prev_start_ts: int,
    timeout:       float = 5.0,
) -> Optional[float]:
    """
    Fallback: query Gamma markets langsung dengan filter waktu.
    """
    coin    = coin.upper()
    kw_list = {
        "BTC":  ["bitcoin", "btc"],
        "ETH":  ["ethereum", "eth"],
        "SOL":  ["solana", "sol"],
    }.get(coin, [coin.lower()])

    try:
        for kw in kw_list[:1]:
            resp = requests.get(
                f"{GAMMA_BASE_URL}/markets",
                params={
                    "search":   f"{kw} up or down 5",
                    "closed":   "true",
                    "limit":    "20",
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue

            markets = resp.json()
            if not isinstance(markets, list):
                continue

            for m in markets:
                # Cek apakah endDate sesuai dengan window sebelumnya
                end_date_str = m.get("endDate") or m.get("endDateIso", "")
                if not end_date_str:
                    continue

                try:
                    clean   = end_date_str.replace("Z", "+00:00")
                    end_dt  = datetime.fromisoformat(clean)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    end_ts  = int(end_dt.timestamp())
                    # Window sebelumnya berakhir di prev_start_ts + WINDOW_DURATION
                    prev_end = prev_start_ts + WINDOW_DURATION
                    if abs(end_ts - prev_end) > 60:
                        continue
                except Exception:
                    continue

                # Ambil strike price
                strike = _extract_strike(m, {})
                if strike:
                    logger.info(
                        f"[PrevWindow] ✅ {coin} resolved via direct search "
                        f"| strike=${strike:,.2f}"
                    )
                    return strike

    except Exception as e:
        logger.debug(f"[PrevWindow] Direct search error: {e}")

    return None


def _extract_strike(m: dict, ev: dict) -> Optional[float]:
    """
    Extract final/strike price dari market dict.

    Priority:
    1. groupItemThreshold (field resmi Polymarket untuk strike price)
    2. outcomes dengan $ (parse angka BTC dari outcome label)
    3. question/title
    """
    import re

    # 1. groupItemThreshold
    try:
        val = float(m.get("groupItemThreshold", 0) or 0)
        if 5_000 < val < 500_000:
            return val
    except Exception:
        pass

    def safe_parse_float(text: str) -> Optional[float]:
        """Extract harga BTC ($XX,XXX.XX) dari teks."""
        if not text:
            return None
        # Pattern dengan $ dan koma
        for pat in [
            r'\$([0-9]{1,3}(?:,[0-9]{3})+\.[0-9]+)',
            r'\$([0-9]{1,3}(?:,[0-9]{3})+)',
            r'\$([0-9]{5,6}\.[0-9]+)',
        ]:
            matches = re.findall(pat, text)
            for m_str in matches:
                try:
                    val = float(m_str.replace(',', ''))
                    if 5_000 < val < 500_000:
                        return val
                except Exception:
                    pass
        return None

    # 2. outcomes
    def parse_list(val):
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return [val]
        return []

    for outcome in parse_list(m.get("outcomes", [])):
        strike = safe_parse_float(str(outcome))
        if strike:
            return strike

    # 3. question / title
    for field in ["question", "title", "description"]:
        text = m.get(field, "") or ev.get(field, "") or ev.get("title", "")
        if text:
            strike = safe_parse_float(str(text))
            if strike:
                return strike

    return None


class PrevWindowResolver:
    """
    Manager untuk fetch & cache resolved price window sebelumnya.

    Cara pakai di bot_late.py:
        resolver = PrevWindowResolver()

        # Di awal setiap window baru (elapsed ~0s):
        resolver.on_new_window(coin, current_window_id)

        # Di loop utama, panggil get_beat() untuk dapat beat price:
        beat = resolver.get_beat(coin)
        if beat:
            windows[coin].beat_price = beat
    """

    def __init__(
        self,
        wait_before_fetch: float = 35.0,   # Tunggu N detik sebelum fetch (beri waktu Polymarket finalize)
        max_fetch_attempts: int  = 5,       # Max retry fetch per window
        fetch_interval:     float = 15.0,   # Interval antar retry
    ):
        self.wait_before_fetch  = wait_before_fetch
        self.max_fetch_attempts = max_fetch_attempts
        self.fetch_interval     = fetch_interval

        # State per coin
        self._state: Dict[str, dict] = {}

    def on_new_window(self, coin: str, current_window_id: str) -> None:
        """
        Dipanggil saat window baru dimulai.
        Reset state dan siapkan fetch window sebelumnya.
        """
        coin = coin.upper()
        prev_start, prev_end, prev_window_id = get_prev_window_timestamps()

        self._state[coin] = {
            "current_window_id": current_window_id,
            "prev_window_id":    prev_window_id,
            "prev_start_ts":     prev_start,
            "prev_end_ts":       prev_end,
            "beat_price":        None,
            "fetch_attempts":    0,
            "last_fetch_ts":     0.0,
            "window_start_ts":   time.time(),
            "resolved":          False,
        }

        logger.info(
            f"[PrevWindow] {coin} new window: {current_window_id} "
            f"| fetching prev={prev_window_id}"
        )

    def should_fetch(self, coin: str) -> bool:
        """Apakah sudah waktunya fetch resolved price."""
        coin = coin.upper()
        s = self._state.get(coin)
        if not s:
            return False
        if s["resolved"]:
            return False
        if s["fetch_attempts"] >= self.max_fetch_attempts:
            return False

        elapsed_since_window_start = time.time() - s["window_start_ts"]
        if elapsed_since_window_start < self.wait_before_fetch:
            return False

        elapsed_since_last_fetch = time.time() - s["last_fetch_ts"]
        if elapsed_since_last_fetch < self.fetch_interval:
            return False

        return True

    def try_fetch(self, coin: str) -> Optional[float]:
        """
        Coba fetch resolved price window sebelumnya.
        Returns beat price jika berhasil, None jika belum tersedia.
        """
        coin = coin.upper()
        s = self._state.get(coin)
        if not s:
            return None

        s["fetch_attempts"] += 1
        s["last_fetch_ts"]   = time.time()

        attempt = s["fetch_attempts"]
        prev_id = s["prev_window_id"]
        logger.info(
            f"[PrevWindow] {coin} fetching prev window {prev_id} "
            f"(attempt {attempt}/{self.max_fetch_attempts})"
        )

        # Cek cache dulu
        cached = _resolved_cache.get(f"{coin}:{prev_id}")
        if cached:
            price, ts = cached
            s["beat_price"] = price
            s["resolved"]   = True
            logger.info(
                f"[PrevWindow] {coin} ✅ FROM CACHE: ${price:,.2f} "
                f"(cached {time.time()-ts:.0f}s ago)"
            )
            return price

        # Fetch dari Gamma API via slug
        price = fetch_resolved_price_from_gamma(
            coin=coin,
            prev_start_ts=s["prev_start_ts"],
            prev_end_ts=s["prev_end_ts"],
        )

        # Fallback: direct market search
        if not price:
            price = fetch_resolved_price_direct(
                coin=coin,
                prev_start_ts=s["prev_start_ts"],
            )

        if price:
            s["beat_price"] = price
            s["resolved"]   = True
            # Simpan ke cache
            _resolved_cache[f"{coin}:{prev_id}"] = (price, time.time())
            logger.info(
                f"[PrevWindow] {coin} ✅ RESOLVED: ${price:,.2f} "
                f"for window {prev_id} → beat for {s['current_window_id']}"
            )
            return price
        else:
            remaining_attempts = self.max_fetch_attempts - attempt
            if remaining_attempts > 0:
                logger.info(
                    f"[PrevWindow] {coin} ⏳ Not resolved yet "
                    f"(attempt {attempt}, retry in {self.fetch_interval:.0f}s, "
                    f"{remaining_attempts} left)"
                )
            else:
                logger.warning(
                    f"[PrevWindow] {coin} ❌ Max attempts reached for {prev_id} "
                    f"— will fallback to Chainlink"
                )
            return None

    def get_beat(self, coin: str) -> Optional[float]:
        """Ambil beat price yang sudah di-resolve. None jika belum ada."""
        coin = coin.upper()
        s = self._state.get(coin)
        if not s:
            return None
        return s.get("beat_price")

    def is_resolved(self, coin: str) -> bool:
        """Apakah beat price sudah berhasil di-resolve."""
        coin = coin.upper()
        s = self._state.get(coin)
        return bool(s and s.get("resolved"))

    def get_status(self, coin: str) -> str:
        """Status string untuk display/logging."""
        coin = coin.upper()
        s = self._state.get(coin)
        if not s:
            return "NO_STATE"
        if s["resolved"]:
            return f"✅ ${s['beat_price']:,.2f}"
        attempts = s["fetch_attempts"]
        elapsed  = time.time() - s["window_start_ts"]
        wait     = max(0, self.wait_before_fetch - elapsed)
        if wait > 0:
            return f"⏳ wait {wait:.0f}s"
        return f"🔄 fetching ({attempts}/{self.max_fetch_attempts})"
