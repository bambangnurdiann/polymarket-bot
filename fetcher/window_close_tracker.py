"""
fetcher/window_close_tracker.py  (v2.1 — fixes)
=================================================
FIX:
  - get_status() tambah key 'capturing' agar dashboard tidak KeyError
  - RPC list diupdate: hapus polygon-rpc.com (butuh API key sekarang)
  - Fallback ke Ankr dan public RPCs lainnya
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

CACHE_PATH     = "logs/window_close_cache.json"
WINDOW_SECONDS = 300

# RPC yang masih bebas tanpa API key (April 2026)
PUBLIC_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://matic-mainnet.chainstacklabs.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
]


class ChainlinkRound:
    __slots__ = ("round_id", "price", "updated_at", "fetched_at")

    def __init__(self, round_id: int, price: float, updated_at: int, fetched_at: float):
        self.round_id   = round_id
        self.price      = price
        self.updated_at = updated_at  # on-chain timestamp — KUNCI akurasi
        self.fetched_at = fetched_at

    def __repr__(self):
        dt = datetime.fromtimestamp(self.updated_at, tz=timezone.utc).strftime("%H:%M:%S")
        return f"Round#{self.round_id} ${self.price:,.2f} @{dt}UTC"


class WindowCloseTracker:
    """
    Beat price = Chainlink round dengan updatedAt terbesar yang <= window_end.
    Sama persis dengan logika Polymarket.
    """

    def __init__(self, cl_monitor=None):
        self.cl_monitor = cl_monitor
        os.makedirs("logs", exist_ok=True)

        # coin -> [ChainlinkRound, ...]
        self._rounds: Dict[str, List[ChainlinkRound]] = {}

        # coin -> {window_id, close_price, round_id, updated_at, committed_at}
        self._cache: Dict[str, dict] = {}

        # coin -> window_id yang sudah di-commit (hindari double commit)
        self._committed_windows: Dict[str, str] = {}

        self._load_cache()

    # ── Persistence ───────────────────────────────────────────

    def _load_cache(self) -> None:
        if not os.path.exists(CACHE_PATH):
            return
        try:
            with open(CACHE_PATH) as f:
                self._cache = json.load(f)
            for coin, d in self._cache.items():
                self._committed_windows[coin] = d.get("window_id", "")
            logger.info(
                "[WCT] Cache loaded: "
                + ", ".join(
                    f"{c}={d.get('window_id')} ${d.get('close_price', 0):,.2f}"
                    for c, d in self._cache.items()
                )
            )
        except Exception as e:
            logger.debug(f"[WCT] Load cache error: {e}")
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            logger.debug(f"[WCT] Save cache error: {e}")

    # ── Feed round data ───────────────────────────────────────

    def on_chainlink_update(self, coin: str, round_id: int, price: float, updated_at: int) -> None:
        """Feed data round dari ChainlinkMonitor. Panggil setiap ada update."""
        coin = coin.upper()

        if coin not in self._rounds:
            self._rounds[coin] = []

        rounds = self._rounds[coin]

        # Skip duplicate
        if rounds and rounds[-1].round_id == round_id:
            return

        snap = ChainlinkRound(
            round_id=round_id,
            price=price,
            updated_at=updated_at,
            fetched_at=time.time(),
        )
        rounds.append(snap)

        # Bersihkan data > 15 menit (hemat memori)
        cutoff = time.time() - 900
        self._rounds[coin] = [r for r in rounds if r.fetched_at >= cutoff]

        logger.debug(f"[WCT] [{coin}] {snap}")

        # Coba commit window yang baru saja tutup
        self._try_commit_window(coin)

    def _feed_price_only(self, coin: str, price: float) -> None:
        """
        Fallback jika tidak ada akses round_id.
        Pakai unix timestamp sekarang sebagai proxy updated_at.
        """
        coin = coin.upper()
        now  = int(time.time())

        if coin not in self._rounds:
            self._rounds[coin] = []

        rounds = self._rounds[coin]

        # Skip kalau harga tidak berubah
        if rounds and abs(rounds[-1].price - price) < 0.01:
            return

        pseudo_id = rounds[-1].round_id + 1 if rounds else 1
        snap = ChainlinkRound(
            round_id=pseudo_id,
            price=price,
            updated_at=now,
            fetched_at=now,
        )
        rounds.append(snap)
        self._try_commit_window(coin)

    def _try_commit_window(self, coin: str) -> None:
        """
        Cari round terbaik untuk window yang baru tutup dan commit.

        Logika Polymarket:
          prev_window_end = current_window_start
          best_round = max(r for r in rounds if r.updated_at <= prev_window_end)
        """
        now         = time.time()
        win_start   = (now // WINDOW_SECONDS) * WINDOW_SECONDS
        prev_end    = win_start          # = batas tutup window sebelumnya
        prev_start  = prev_end - WINDOW_SECONDS

        dt_prev     = datetime.fromtimestamp(prev_start, tz=timezone.utc)
        prev_win_id = dt_prev.strftime("%Y%m%d-%H%M")

        # Sudah di-commit?
        if self._committed_windows.get(coin) == prev_win_id:
            return

        rounds = self._rounds.get(coin, [])
        if not rounds:
            return

        # Cari round terbaik: updated_at terbesar yang <= prev_end
        candidate = None
        for r in reversed(rounds):
            if r.updated_at <= prev_end:
                candidate = r
                break

        if not candidate:
            return

        # Commit
        self._cache[coin] = {
            "window_id":    prev_win_id,
            "close_price":  candidate.price,
            "round_id":     candidate.round_id,
            "updated_at":   candidate.updated_at,
            "committed_at": now,
        }
        self._committed_windows[coin] = prev_win_id
        self._save_cache()

        ut_str = datetime.fromtimestamp(
            candidate.updated_at, tz=timezone.utc
        ).strftime("%H:%M:%S")

        logger.info(
            f"[WCT] [{coin}] ✅ Committed window {prev_win_id}: "
            f"${candidate.price:,.2f} "
            f"(round #{candidate.round_id}, CL_updatedAt={ut_str} UTC)"
        )

    # ── Tick shortcut ─────────────────────────────────────────

    def tick(self, cl_price: float, coin: str = "BTC") -> Optional[float]:
        """
        Shortcut feed dari loop bot.
        Prioritas: ambil round data dari cl_monitor, fallback price only.
        """
        coin = coin.upper()

        if self.cl_monitor:
            snap = self.cl_monitor.prices.get(coin)
            if snap and snap.updated_at > 0:
                self.on_chainlink_update(
                    coin=coin,
                    round_id=snap.round_id,
                    price=snap.price,
                    updated_at=snap.updated_at,
                )
            elif cl_price and cl_price > 0:
                self._feed_price_only(coin, cl_price)
        elif cl_price and cl_price > 0:
            self._feed_price_only(coin, cl_price)

        return self.get_beat_for_current_window(coin)

    # ── Query ─────────────────────────────────────────────────

    def get_beat_for_current_window(self, coin: str = "BTC") -> Optional[float]:
        """Beat price untuk window saat ini = close price window sebelumnya."""
        coin      = coin.upper()
        now       = time.time()
        win_start = (now // WINDOW_SECONDS) * WINDOW_SECONDS
        win_id    = datetime.fromtimestamp(win_start, tz=timezone.utc).strftime("%Y%m%d-%H%M")

        cached = self._cache.get(coin)
        if not cached:
            return None

        cached_win  = cached.get("window_id", "")
        close_price = cached.get("close_price", 0)

        if not close_price or close_price <= 0:
            return None

        # Cache harus dari window SEBELUMNYA
        if cached_win == win_id:
            return None

        # Tidak boleh lebih dari 10 menit
        age = now - cached.get("committed_at", 0)
        if age > 600:
            logger.warning(f"[WCT] [{coin}] Cache umur {age:.0f}s — ada gap window?")

        return close_price

    def get_status(self, coin: str = "BTC") -> dict:
        """
        Status untuk dashboard.
        SEMUA key selalu ada agar tidak KeyError.
        """
        coin      = coin.upper()
        beat      = self.get_beat_for_current_window(coin)
        cached    = self._cache.get(coin, {})
        rounds    = self._rounds.get(coin, [])

        now       = time.time()
        win_start = (now // WINDOW_SECONDS) * WINDOW_SECONDS
        win_end   = win_start + WINDOW_SECONDS
        remaining = win_end - now

        # Apakah sedang dalam fase capture (menunggu window tutup)
        # Tidak relevan lagi di v2 karena kita track per round,
        # tapi tetap diisi agar tidak KeyError di dashboard lama
        capturing = remaining <= 15  # 15 detik terakhir = "kritis"

        latest_round = rounds[-1] if rounds else None
        round_info = ""
        if latest_round:
            age = now - latest_round.fetched_at
            round_info = (
                f"#{latest_round.round_id} "
                f"${latest_round.price:,.2f} "
                f"({age:.0f}s ago)"
            )

        ut = cached.get("updated_at", 0)
        ut_str = (
            datetime.fromtimestamp(ut, tz=timezone.utc).strftime("%H:%M:%S")
            if ut else "?"
        )

        return {
            "coin":            coin,
            "beat_price":      beat,
            "beat_from":       cached.get("window_id", "N/A"),
            "beat_round_id":   cached.get("round_id", 0),
            "beat_updated_at": ut,
            "beat_updated_str": ut_str,
            "committed_at":    cached.get("committed_at", 0),
            "round_count":     len(rounds),
            "latest_round":    round_info,
            "remaining":       remaining,
            "capturing":       capturing,        # ← FIX: key ini selalu ada
            "capture_count":   len(rounds),      # ← FIX: key ini selalu ada
            "pending_window":  cached.get("window_id", "N/A"),  # ← FIX
        }

    def force_set_beat(self, coin: str, price: float, window_id: str = None) -> None:
        """Manual override untuk recovery / testing."""
        coin = coin.upper()
        if not window_id:
            now        = time.time()
            prev_end   = (now // WINDOW_SECONDS) * WINDOW_SECONDS
            prev_start = prev_end - WINDOW_SECONDS
            window_id  = datetime.fromtimestamp(prev_start, tz=timezone.utc).strftime("%Y%m%d-%H%M")

        self._cache[coin] = {
            "window_id":    window_id,
            "close_price":  price,
            "round_id":     0,
            "updated_at":   int(time.time()),
            "committed_at": time.time(),
        }
        self._committed_windows[coin] = window_id
        self._save_cache()
        logger.info(f"[WCT] [{coin}] Force set: window={window_id} ${price:,.2f}")

    def summary(self, coin: str = "BTC") -> str:
        s    = self.get_status(coin)
        beat = s["beat_price"]
        if beat:
            age    = time.time() - s["committed_at"]
            ut_str = s["beat_updated_str"]
            return (
                f"WCT:{coin} beat=${beat:,.2f} "
                f"from={s['beat_from']} "
                f"round=#{s['beat_round_id']} "
                f"CL_updated={ut_str}UTC "
                f"({age:.0f}s ago)"
            )
        return f"WCT:{coin} beat=N/A (belum ada history)"