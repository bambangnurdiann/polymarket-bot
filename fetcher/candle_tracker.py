"""
fetcher/candle_tracker.py  (PATCHED v2)
========================================
PATCH v2:
  - Tambah set_beat_from_window_close() — GROUND TRUTH tertinggi
  - beat_source priority: WINDOW_CLOSE > POLYMARKET_API > CHAINLINK > HYPERLIQUID
  - WINDOW_CLOSE = final Chainlink price dari window sebelumnya
    (ini persis yang Polymarket pakai sebagai beat price)
  - beat_confirmed: True kalau dari WINDOW_CLOSE atau POLYMARKET_API
"""

import time
from datetime import datetime, timezone
from typing import Optional

# Priority map: makin besar angka makin trusted
BEAT_SOURCE_PRIORITY = {
    "UNKNOWN":       0,
    "HYPERLIQUID":   1,
    "CHAINLINK":     2,
    "POLYMARKET_API": 3,
    "WINDOW_CLOSE":  4,   # NEW: tertinggi
}


class CandleTracker:
    """
    Tracker window 5 menit Polymarket.

    PATCH v2: Tambah WINDOW_CLOSE sebagai sumber beat price paling akurat.
    WINDOW_CLOSE = final Chainlink price dari window sebelumnya,
    persis yang Polymarket pakai sebagai beat price resmi.
    """

    WINDOW_DURATION      = 300
    BEAT_RELIABLE_WINDOW = 30

    def __init__(self):
        self.window_id:         Optional[str]   = None
        self.window_start:      Optional[float] = None
        self.window_end:        Optional[float] = None
        self.beat_price:        Optional[float] = None
        self.beat_source:       str             = "UNKNOWN"
        self.beat_set_elapsed:  float           = 999.0
        self.beat_set_at:       float           = 0.0
        self.beat_confirmed:    bool            = False
        self.is_new_window:     bool            = False
        self._last_window_id:   Optional[str]   = None
        self.update()

    def update(self) -> None:
        now          = time.time()
        window_start = (now // self.WINDOW_DURATION) * self.WINDOW_DURATION
        window_end   = window_start + self.WINDOW_DURATION

        dt        = datetime.fromtimestamp(window_start, tz=timezone.utc)
        window_id = dt.strftime("%Y%m%d-%H%M")

        self.is_new_window = (window_id != self._last_window_id)
        if self.is_new_window:
            self._last_window_id  = window_id
            self.beat_price       = None
            self.beat_source      = "UNKNOWN"
            self.beat_set_elapsed = 999.0
            self.beat_set_at      = 0.0
            self.beat_confirmed   = False

        self.window_id    = window_id
        self.window_start = window_start
        self.window_end   = window_end

    @property
    def remaining(self) -> float:
        return max(0.0, self.window_end - time.time())

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.window_start)

    @property
    def progress_pct(self) -> float:
        return min(1.0, self.elapsed / self.WINDOW_DURATION)

    # ── Beat price management ─────────────────────────────────

    def _can_override(self, new_source: str) -> bool:
        """Cek apakah source baru boleh override source lama."""
        new_prio = BEAT_SOURCE_PRIORITY.get(new_source, 0)
        cur_prio = BEAT_SOURCE_PRIORITY.get(self.beat_source, 0)
        return new_prio >= cur_prio

    def set_beat_from_window_close(self, price: float) -> bool:
        """
        NEW (PATCH v2): Set beat dari final close price window sebelumnya.

        Ini adalah sumber PALING AKURAT karena:
        - Polymarket pakai Chainlink final price window sebelumnya sebagai beat
        - Tidak ada ambiguitas timing atau miss price
        - Selalu override sumber lain (kecuali kalau harga sama)

        Returns True jika ada perubahan.
        """
        if not price or price <= 0:
            return False

        changed = (
            self.beat_price is None
            or abs(price - self.beat_price) > 0.01
            or self.beat_source != "WINDOW_CLOSE"
        )

        self.beat_price       = price
        self.beat_source      = "WINDOW_CLOSE"
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        self.beat_confirmed   = True  # Ground truth → lock

        return changed

    def set_beat_from_api(self, price: float) -> bool:
        """
        Set beat dari Polymarket API strike_price.
        Priority kedua setelah WINDOW_CLOSE.
        """
        if not price or price <= 0:
            return False

        # Jangan override WINDOW_CLOSE
        if self.beat_source == "WINDOW_CLOSE":
            return False

        changed = (
            self.beat_price is None
            or abs(price - self.beat_price) > 0.5
        )

        self.beat_price       = price
        self.beat_source      = "POLYMARKET_API"
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        self.beat_confirmed   = True

        return changed

    def set_beat_from_chainlink(self, price: float) -> bool:
        """Set beat dari Chainlink realtime. Fallback jika WINDOW_CLOSE belum ada."""
        if not price or price <= 0:
            return False

        # Jangan override source yang lebih trusted
        if self.beat_source in ("WINDOW_CLOSE", "POLYMARKET_API"):
            return False

        self.beat_price       = price
        self.beat_source      = "CHAINLINK"
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    def set_beat_price(self, price: float, source: str = "HYPERLIQUID") -> bool:
        """Set beat dari Hyperliquid (last resort fallback)."""
        if not price or price <= 0:
            return False
        if self.beat_source in ("WINDOW_CLOSE", "POLYMARKET_API", "CHAINLINK"):
            return False
        if self.beat_confirmed:
            return False

        self.beat_price       = price
        self.beat_source      = source
        self.beat_set_elapsed = self.elapsed
        self.beat_set_at      = time.time()
        return True

    def set_beat_from_hyperliquid(self, price: float) -> bool:
        """Alias untuk set_beat_price dengan source HYPERLIQUID."""
        return self.set_beat_price(price, source="HYPERLIQUID")

    @property
    def is_beat_reliable(self) -> bool:
        if self.beat_price is None:
            return False
        # WINDOW_CLOSE dan POLYMARKET_API selalu reliable
        if self.beat_source in ("WINDOW_CLOSE", "POLYMARKET_API", "CHAINLINK"):
            return True
        # Hyperliquid hanya reliable kalau diset di 10 detik pertama
        if self.beat_source == "HYPERLIQUID" and self.beat_set_elapsed <= 10:
            return True
        return False

    @property
    def beat_warning(self) -> str:
        if self.beat_price is None:
            return "Beat price belum tersedia"
        if self.beat_source == "WINDOW_CLOSE":
            return ""  # Perfect accuracy
        if self.beat_source == "POLYMARKET_API":
            return ""
        if self.beat_source == "CHAINLINK":
            return ""
        if self.beat_source == "HYPERLIQUID":
            if self.beat_set_elapsed <= 10:
                return ""
            return (
                f"Beat dari Hyperliquid (bukan WINDOW_CLOSE), "
                f"set t={self.beat_set_elapsed:.0f}s — mungkin beda ±$50 dari Polymarket"
            )
        return "Source beat price tidak diketahui"

    def get_market_name(self) -> str:
        dt     = datetime.fromtimestamp(self.window_start, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(self.window_end, tz=timezone.utc)
        day    = str(dt.day)
        return (
            f"BTC Up or Down - {dt.strftime('%b')} {day}, "
            f"{dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')} UTC"
        )

    def progress_bar(self, width: int = 30) -> str:
        filled = int(self.progress_pct * width)
        return f"[{'█' * filled}{'░' * (width - filled)}]"

    def __repr__(self) -> str:
        src  = self.beat_source
        rel  = "✓" if self.is_beat_reliable else "⚠"
        lock = "🔒" if self.beat_confirmed else ""
        return (
            f"CandleTracker(window={self.window_id}, "
            f"elapsed={self.elapsed:.0f}s, "
            f"beat={self.beat_price} [{src}{rel}{lock}])"
        )
