"""
engine/coin_engine.py
=====================
CoinEngine: state machine dan filter logic untuk satu coin.

Setiap coin (BTC, ETH, SOL, DOGE) punya CoinEngine sendiri yang:
  1. Track candle window 5 menit (beat price, elapsed, remaining)
  2. Jalankan 5 filter Late Bot
  3. Return SignalResult yang berisi arah + strength score

SignalArbiter kemudian membandingkan semua CoinEngine dan
memilih sinyal terkuat untuk dieksekusi.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from fetcher.candle_tracker import CandleTracker
from fetcher.multi_ws import CoinDataStore

if TYPE_CHECKING:
    from fetcher.chainlink_monitor import ChainlinkMonitor, MispricingSignal

logger = logging.getLogger(__name__)

# Konfigurasi threshold per coin
# Bisa dikustomisasi — coin yang lebih volatile perlu threshold lebih tinggi
COIN_CONFIG = {
    "BTC": {
        "beat_distance":    40,       # $40
        "liq_recent":       15_000,   # $15k
        "liq_sustained":    50_000,   # $50k
        "cvd_threshold":    25_000,   # $25k
    },
    "ETH": {
        "beat_distance":    3,        # $3 (ETH harganya ~$2k-$3k)
        "liq_recent":       5_000,    # $5k (market lebih kecil dari BTC)
        "liq_sustained":    20_000,   # $20k
        "cvd_threshold":    10_000,   # $10k
    },
    "SOL": {
        "beat_distance":    0.5,      # $0.5 (SOL ~$100-$200)
        "liq_recent":       2_000,    # $2k
        "liq_sustained":    8_000,    # $8k
        "cvd_threshold":    4_000,    # $4k
    },
    "DOGE": {
        "beat_distance":    0.003,    # $0.003 (DOGE ~$0.1-$0.2)
        "liq_recent":       1_000,    # $1k
        "liq_sustained":    5_000,    # $5k
        "cvd_threshold":    2_000,    # $2k
    },
    "XRP": {
        "beat_distance":    0.005,    # $0.005
        "liq_recent":       2_000,
        "liq_sustained":    8_000,
        "cvd_threshold":    4_000,
    },
}

# Default config untuk coin yang tidak ada di COIN_CONFIG
DEFAULT_CONFIG = {
    "beat_distance":  1.0,
    "liq_recent":     3_000,
    "liq_sustained":  10_000,
    "cvd_threshold":  5_000,
}


@dataclass
class SignalResult:
    """Hasil evaluasi filter untuk satu coin di satu window."""
    coin:           str
    timestamp:      float
    direction:      str            # "UP", "DOWN", atau ""
    should_bet:     bool
    strength:       float          # 0.0+ (lebih tinggi = lebih kuat)
    reason:         str
    beat_price:     float = 0.0
    current_price:  float = 0.0
    diff:           float = 0.0
    odds_up:        float = 0.5
    odds_down:      float = 0.5
    filter_details: dict  = field(default_factory=dict)
    # F0: Chainlink mispricing signal (None jika tidak ada)
    chainlink_signal: Optional[object] = None
    # Mode: "CHAINLINK" jika dari F0, "LATE" jika dari F1-F4
    mode:           str   = "LATE"

    @property
    def odds(self) -> float:
        return self.odds_up if self.direction == "UP" else self.odds_down


class CoinEngine:
    """
    State machine untuk satu coin.

    Attributes:
        symbol       : str  — "BTC", "ETH", dll
        candle       : CandleTracker
        bet_this_win : bool — sudah bet di window ini
        last_result  : SignalResult — hasil filter terakhir
    """

    def __init__(
        self,
        symbol:         str,
        entry_min:      float = 210,
        entry_max:      float = 290,
        min_odds:       float = 0.45,
        chainlink_monitor=None,   # Optional[ChainlinkMonitor]
        cl_min_edge:    float = 0.08,   # minimum edge untuk F0
        cl_min_remaining: float = 15.0, # minimum sisa detik untuk F0
        cl_max_remaining: float = 270.0,# F0 aktif sampai detik ke-270
    ):
        self.symbol       = symbol.upper()
        self.entry_min    = entry_min
        self.entry_max    = entry_max
        self.min_odds     = min_odds
        self.config       = COIN_CONFIG.get(self.symbol, DEFAULT_CONFIG)
        self.cl_monitor   = chainlink_monitor
        self.cl_min_edge  = cl_min_edge
        self.cl_min_rem   = cl_min_remaining
        self.cl_max_rem   = cl_max_remaining

        self.candle          = CandleTracker()
        self.bet_this_window = False
        self.last_result:    Optional[SignalResult] = None
        self.odds_up:        float = 0.5
        self.odds_down:      float = 0.5
        self._last_window:   str   = ""

    def update_odds(self, odds_up: float, odds_down: float) -> None:
        self.odds_up   = odds_up
        self.odds_down = odds_down

    def tick(self, data: CoinDataStore) -> SignalResult:
        """
        Jalankan satu siklus evaluasi untuk coin ini.
        Cek F0 (Chainlink) dulu, lalu F1-F4 (Late Bot filters).
        """
        self.candle.update()

        if self.candle.window_id != self._last_window:
            self.bet_this_window = False
            self._last_window    = self.candle.window_id

        btc_price = data.price
        beat      = self.candle.beat_price

        if btc_price and self.candle.elapsed < 5:
            self.candle.set_beat_price(btc_price)
            beat = self.candle.beat_price

        # ── F0: Chainlink Mispricing (priority) ───────────────
        if self.cl_monitor and beat and beat > 0:
            cl_result = self._check_chainlink_f0(beat)
            if cl_result:
                self.last_result = cl_result
                return cl_result

        # ── F1-F4: Late Bot filters ───────────────────────────
        result = self._evaluate(data, btc_price, beat)
        self.last_result = result
        return result

    def _check_chainlink_f0(self, beat: float) -> Optional[SignalResult]:
        """
        F0: Chainlink Mispricing Filter dengan 4 improve aktif.
        """
        if self.bet_this_window:
            return None

        remaining = self.candle.remaining
        if not (self.cl_min_rem <= remaining <= self.cl_max_rem):
            return None

        cl = self.cl_monitor

        # Cek kedua arah, pilih yang edge-nya lebih besar
        best_signal  = None
        best_direction = None

        for direction in ["UP", "DOWN"]:
            odds = self.odds_up if direction == "UP" else self.odds_down
            if odds < self.min_odds:
                continue

            signal = cl.detect_mispricing(
                coin=self.symbol,
                direction=direction,
                beat_price=beat,
                remaining=remaining,
                current_odds=odds,
                min_edge=self.cl_min_edge,
                odds_up=self.odds_up,
                odds_down=self.odds_down,
                use_momentum=True,
                use_time_decay=True,
                min_odds_spread=0.04,
            )

            if signal:
                if best_signal is None or signal.edge > best_signal.edge:
                    best_signal    = signal
                    best_direction = direction

        if not best_signal:
            return None

        signal    = best_signal
        direction = best_direction
        odds      = self.odds_up if direction == "UP" else self.odds_down
        strength  = min(2.0, signal.confidence * (1 + signal.edge * 5))

        # Tambahkan info momentum dan vol ke details
        vol_info = cl.get_vol_info(self.symbol)
        details = {
            "f0": ("PASS", f"{signal.reason} | {vol_info}"),
            "f1": ("SKIP", "F0 override"),
            "f2": ("SKIP", "F0 override"),
            "f3": ("SKIP", "F0 override"),
            "f4": ("SKIP", "F0 override"),
            "f5": ("INFO", f"{direction}={odds:.4f} spread={abs(self.odds_up-self.odds_down):.3f}"),
        }

        logger.info(
            f"[F0-CL] {self.symbol} {direction} DETECTED | "
            f"edge={signal.edge:+.3f} conf={signal.confidence:.2f} "
            f"mom={'✓' if signal.momentum_ok else '✗'} | "
            f"{signal.reason}"
        )

        return SignalResult(
            coin=self.symbol,
            timestamp=time.time(),
            direction=direction,
            should_bet=True,
            strength=strength,
            reason=f"F0-CHAINLINK: {signal.reason}",
            beat_price=beat,
            current_price=signal.chainlink_price,
            diff=signal.chainlink_price - beat,
            odds_up=self.odds_up,
            odds_down=self.odds_down,
            filter_details=details,
            chainlink_signal=signal,
            mode="CHAINLINK",
        )

        return None

    def _evaluate(
        self,
        data:      CoinDataStore,
        btc_price: Optional[float],
        beat:      Optional[float],
    ) -> SignalResult:
        """Jalankan semua filter dan return SignalResult."""

        def skip(reason: str, details: dict = {}) -> SignalResult:
            return SignalResult(
                coin=self.symbol, timestamp=time.time(),
                direction="", should_bet=False,
                strength=0.0, reason=reason,
                beat_price=beat or 0, current_price=btc_price or 0,
                odds_up=self.odds_up, odds_down=self.odds_down,
                filter_details=details,
            )

        details = {"f1": ("WAIT",""), "f2": ("WAIT",""), "f3": ("WAIT",""), "f4": ("WAIT",""), "f5": ("INFO","")}

        if self.bet_this_window:
            return skip("Sudah bet di window ini", details)

        # F1 — Entry zone
        elapsed   = self.candle.elapsed
        remaining = self.candle.remaining
        f1_ok = self.entry_min <= elapsed <= self.entry_max
        if not f1_ok:
            if elapsed < self.entry_min:
                reason = f"F1: Terlalu awal t={elapsed:.0f}s"
            else:
                reason = f"F1: Terlalu telat t={elapsed:.0f}s"
            details["f1"] = ("FAIL", reason)
            return skip(reason, details)
        details["f1"] = ("PASS", f"t={elapsed:.0f}s ✓ sisa={remaining:.0f}s")

        if not beat or beat <= 0:
            return skip("Beat price belum ada", details)
        if not btc_price or btc_price <= 0:
            return skip("Harga tidak tersedia", details)
        if data.price_stale:
            return skip("Harga stale (>10s)", details)

        # F2 — Beat distance
        diff     = btc_price - beat
        abs_diff = abs(diff)
        direction = "UP" if diff > 0 else "DOWN"
        cfg_dist  = self.config["beat_distance"]

        if abs_diff < cfg_dist:
            reason = f"F2: ${abs_diff:.3f} < ${cfg_dist}"
            details["f2"] = ("FAIL", reason)
            return skip(reason, details)
        details["f2"] = ("PASS", f"|dist|={abs_diff:.3f} ✓ → {direction}")

        # F3 — Liquidation
        f3_ok, f3_msg = data.check_liq(direction, self.config["liq_recent"], self.config["liq_sustained"])
        details["f3"] = ("PASS" if f3_ok else "FAIL", f3_msg)
        if not f3_ok:
            return skip(f"F3: {f3_msg}", details)

        # F4 — CVD
        f4_ok, f4_msg = data.check_cvd(direction, self.config["cvd_threshold"])
        details["f4"] = ("PASS" if f4_ok else "FAIL", f4_msg)
        if not f4_ok:
            return skip(f"F4: {f4_msg}", details)

        # F5 — Odds (info only)
        odds = self.odds_up if direction == "UP" else self.odds_down
        details["f5"] = ("INFO", f"{direction}={odds:.4f}")

        # Hitung strength score
        strength = data.signal_strength(direction)

        return SignalResult(
            coin=self.symbol,
            timestamp=time.time(),
            direction=direction,
            should_bet=True,
            strength=strength,
            reason=f"✓ ALL PASS | {direction} | dist={diff:+.3f} | strength={strength:.2f}",
            beat_price=beat,
            current_price=btc_price,
            diff=diff,
            odds_up=self.odds_up,
            odds_down=self.odds_down,
            filter_details=details,
        )

    def mark_bet_done(self) -> None:
        """Tandai bahwa bet sudah dilakukan di window ini."""
        self.bet_this_window = True
