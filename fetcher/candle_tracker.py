"""
fetcher/candle_tracker.py
=========================
Tracker window 5 menit Polymarket BTC.

Polymarket BTC 5-menit membuka window baru setiap kelipatan 5 menit (UTC):
  00:00, 00:05, 00:10, ...

Tugas tracker ini:
  - Hitung window ID saat ini
  - Hitung sisa waktu dalam window (remaining_seconds)
  - Hitung beat_price (harga BTC saat window dimulai)
  - Expose state untuk dipakai bot_sniper.py
"""

import time
from datetime import datetime, timezone


class CandleTracker:
    """
    Tracker window 5 menit Polymarket BTC.
    
    Attributes:
        window_id      : str  — ID unik window saat ini (format: "YYYYMMDD-HHMM")
        window_start   : float — Unix timestamp saat window dimulai
        window_end     : float — Unix timestamp saat window berakhir
        remaining      : float — Detik tersisa dalam window
        elapsed        : float — Detik yang sudah berlalu dalam window
        beat_price     : float — Harga BTC saat window dimulai (set dari luar)
        is_new_window  : bool  — True jika window baru saja berganti
    """

    WINDOW_DURATION = 300  # 5 menit = 300 detik

    def __init__(self):
        self.window_id    = None
        self.window_start = None
        self.window_end   = None
        self.beat_price   = None
        self.is_new_window = False
        self._last_window_id = None
        self.update()

    def update(self) -> None:
        """Update state window berdasarkan waktu sekarang."""
        now = time.time()
        # Hitung awal window saat ini (floor ke kelipatan 300 detik)
        window_start = (now // self.WINDOW_DURATION) * self.WINDOW_DURATION
        window_end   = window_start + self.WINDOW_DURATION

        # Buat window ID dari timestamp UTC
        dt = datetime.fromtimestamp(window_start, tz=timezone.utc)
        window_id = dt.strftime("%Y%m%d-%H%M")

        # Deteksi pergantian window
        self.is_new_window = (window_id != self._last_window_id)
        if self.is_new_window:
            self._last_window_id = window_id
            self.beat_price = None  # Reset beat price di window baru

        self.window_id    = window_id
        self.window_start = window_start
        self.window_end   = window_end

    @property
    def remaining(self) -> float:
        """Detik tersisa dalam window saat ini."""
        return max(0.0, self.window_end - time.time())

    @property
    def elapsed(self) -> float:
        """Detik yang sudah berlalu dalam window saat ini."""
        return max(0.0, time.time() - self.window_start)

    @property
    def progress_pct(self) -> float:
        """Persentase window yang sudah berlalu (0.0 - 1.0)."""
        return min(1.0, self.elapsed / self.WINDOW_DURATION)

    def set_beat_price(self, price: float) -> None:
        """Set beat price untuk window saat ini (hanya sekali per window)."""
        if self.beat_price is None and price and price > 0:
            self.beat_price = price

    def get_market_name(self) -> str:
        """Nama market Polymarket untuk window saat ini."""
        dt = datetime.fromtimestamp(self.window_start, tz=timezone.utc)
        # Konversi ke ET (UTC-4 saat EDT, UTC-5 saat EST)
        # Simplifikasi: tampilkan UTC saja
        day = str(dt.day)  # tanpa leading zero, cross-platform
        end_dt = datetime.fromtimestamp(self.window_end, tz=timezone.utc)
        return f"BTC Up or Down - {dt.strftime('%b')} {day}, {dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')} UTC"

    def progress_bar(self, width: int = 30) -> str:
        """Render progress bar window."""
        filled = int(self.progress_pct * width)
        bar = "█" * filled + "░" * (width - filled)
        return f"[{bar}]"

    def __repr__(self) -> str:
        return (
            f"CandleTracker(window={self.window_id}, "
            f"remaining={self.remaining:.1f}s, "
            f"beat_price={self.beat_price})"
        )
