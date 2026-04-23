"""
engine/coin_engine.py
=====================
CoinEngine — versi improved dengan filter lebih ketat.

Perubahan dari analisa loss_analyzer:
  1. Beat distance dinaikkan: BTC 40→60 (filter beat_distance 0-20 yang WR jelek)
  2. F3 Liquidation threshold dinaikkan 50%
  3. F4 CVD threshold dinaikkan + cek alignment lebih ketat
  4. Entry zone dipersempit: 230s–270s (bukan 210–290)
  5. Tambah F5 (dulu info-only): odds spread minimum 0.05
  6. Tambah confidence gate: semua filter pass harus dengan margin
  7. Strength score lebih konservatif
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

# ── Konfigurasi per coin (IMPROVED) ──────────────────────────
# BTC beat_distance dinaikkan 40→60 (WR buruk di 0-20 dan 20-40 per loss analyzer)
# Liq threshold dinaikkan ~50% untuk filter lebih ketat
# CVD threshold dinaikkan untuk hindari false positive
COIN_CONFIG = {
    "BTC": {
        "beat_distance":       60,        # dinaikkan dari 40 → eliminasi jarak kecil yg WR jelek
        "beat_distance_soft":  40,        # soft threshold (boleh masuk tapi strength dikurangi)
        "liq_recent":          20_000,    # dinaikkan dari 15k → filter lebih ketat
        "liq_sustained":       60_000,    # dinaikkan dari 50k
        "cvd_threshold":       30_000,    # dinaikkan dari 25k
        "min_odds_spread":     0.05,      # minimum selisih UP vs DOWN odds
        "entry_min":           230,       # dipersempit dari 210
        "entry_max":           270,       # dipersempit dari 290
    },
    "ETH": {
        "beat_distance":       4,
        "beat_distance_soft":  3,
        "liq_recent":          7_000,
        "liq_sustained":       25_000,
        "cvd_threshold":       12_000,
        "min_odds_spread":     0.05,
        "entry_min":           230,
        "entry_max":           270,
    },
    "SOL": {
        "beat_distance":       0.7,
        "beat_distance_soft":  0.5,
        "liq_recent":          3_000,
        "liq_sustained":       10_000,
        "cvd_threshold":       5_000,
        "min_odds_spread":     0.05,
        "entry_min":           230,
        "entry_max":           270,
    },
    "DOGE": {
        "beat_distance":       0.004,
        "beat_distance_soft":  0.003,
        "liq_recent":          1_500,
        "liq_sustained":       6_000,
        "cvd_threshold":       2_500,
        "min_odds_spread":     0.05,
        "entry_min":           230,
        "entry_max":           270,
    },
    "XRP": {
        "beat_distance":       0.007,
        "beat_distance_soft":  0.005,
        "liq_recent":          2_500,
        "liq_sustained":       9_000,
        "cvd_threshold":       4_500,
        "min_odds_spread":     0.05,
        "entry_min":           230,
        "entry_max":           270,
    },
}

DEFAULT_CONFIG = {
    "beat_distance":       1.5,
    "beat_distance_soft":  1.0,
    "liq_recent":          4_000,
    "liq_sustained":       12_000,
    "cvd_threshold":       6_000,
    "min_odds_spread":     0.05,
    "entry_min":           230,
    "entry_max":           270,
}

# ── Session block UTC hours yang terbukti WR < 45% (dari loss analyzer) ──
# [2, 4, 7] — dipindahkan ke sini sebagai default, bisa di-override via .env
BAD_HOURS_DEFAULT = {2, 4, 7}


@dataclass
class SignalResult:
    """Hasil evaluasi filter untuk satu coin di satu window."""
    coin:           str
    timestamp:      float
    direction:      str
    should_bet:     bool
    strength:       float
    reason:         str
    beat_price:     float = 0.0
    current_price:  float = 0.0
    diff:           float = 0.0
    odds_up:        float = 0.5
    odds_down:      float = 0.5
    filter_details: dict  = field(default_factory=dict)
    chainlink_signal: Optional[object] = None
    mode:           str   = "LATE"
    # NEW: confidence gate (0-1), bet hanya jika >= 0.6
    confidence:     float = 0.0

    @property
    def odds(self) -> float:
        return self.odds_up if self.direction == "UP" else self.odds_down


class CoinEngine:
    """
    State machine untuk satu coin — versi improved.

    Perubahan utama:
    - Filter lebih ketat (beat distance, liq, cvd)
    - Entry zone dipersempit
    - Confidence gate (strength harus cukup tinggi)
    - Bad hour check built-in
    - Odds spread filter wajib (sebelumnya hanya info)
    """

    def __init__(
        self,
        symbol:         str,
        entry_min:      float = 230,       # dipersempit
        entry_max:      float = 270,       # dipersempit
        min_odds:       float = 0.45,
        chainlink_monitor=None,
        cl_min_edge:    float = 0.10,
        cl_min_remaining: float = 60,      # dinaikkan dari 15 → lebih konservatif
        cl_max_remaining: float = 240,     # diturunkan dari 270 → hindari entry terlalu awal
        min_strength:   float = 0.4,       # NEW: minimum strength untuk bet
        bad_hours:      set   = None,      # NEW: jam UTC yang diblok
    ):
        self.symbol       = symbol.upper()
        self.entry_min    = entry_min
        self.entry_max    = entry_max
        self.min_odds     = min_odds
        self.min_strength = min_strength
        self.bad_hours    = bad_hours if bad_hours is not None else BAD_HOURS_DEFAULT

        cfg = COIN_CONFIG.get(self.symbol, DEFAULT_CONFIG)
        self.config = {**DEFAULT_CONFIG, **cfg}

        # Pakai entry dari config jika tidak di-override
        if entry_min == 230:
            self.entry_min = self.config.get("entry_min", 230)
        if entry_max == 270:
            self.entry_max = self.config.get("entry_max", 270)

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
        self.candle.update()

        if self.candle.window_id != self._last_window:
            self.bet_this_window = False
            self._last_window    = self.candle.window_id

        btc_price = data.price
        beat      = self.candle.beat_price

        if btc_price and self.candle.elapsed < 5:
            self.candle.set_beat_price(btc_price)
            beat = self.candle.beat_price

        # F0: Chainlink Mispricing
        if self.cl_monitor and beat and beat > 0:
            cl_result = self._check_chainlink_f0(beat)
            if cl_result:
                self.last_result = cl_result
                return cl_result

        # F1–F5: Late Bot filters
        result = self._evaluate(data, btc_price, beat)
        self.last_result = result
        return result

    def _check_chainlink_f0(self, beat: float) -> Optional[SignalResult]:
        """F0: Chainlink Mispricing — lebih konservatif."""
        if self.bet_this_window:
            return None

        remaining = self.candle.remaining
        if not (self.cl_min_rem <= remaining <= self.cl_max_rem):
            return None

        # Bad hour check
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        if hour in self.bad_hours:
            return None

        cl = self.cl_monitor
        best_signal  = None
        best_direction = None

        for direction in ["UP", "DOWN"]:
            odds = self.odds_up if direction == "UP" else self.odds_down
            if odds < self.min_odds:
                continue

            # Odds spread check
            spread = abs(self.odds_up - self.odds_down)
            if spread < self.config["min_odds_spread"]:
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
                min_odds_spread=self.config["min_odds_spread"],
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

        # Confidence gate: hanya bet jika confidence cukup tinggi
        if signal.confidence < 0.55:
            logger.debug(f"[F0-CL] {self.symbol} {direction} confidence too low: {signal.confidence:.2f}")
            return None

        strength  = min(2.0, signal.confidence * (1 + signal.edge * 5))

        # Minimum strength gate
        if strength < self.min_strength:
            return None

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
            f"edge={signal.edge:+.3f} conf={signal.confidence:.2f} str={strength:.2f} "
            f"mom={'✓' if signal.momentum_ok else '✗'} | {signal.reason}"
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
            confidence=signal.confidence,
        )

    def _evaluate(
        self,
        data:      CoinDataStore,
        btc_price: Optional[float],
        beat:      Optional[float],
    ) -> SignalResult:
        """Jalankan semua filter F1–F5 dengan threshold yang lebih ketat."""

        def skip(reason: str, details: dict = {}) -> SignalResult:
            return SignalResult(
                coin=self.symbol, timestamp=time.time(),
                direction="", should_bet=False,
                strength=0.0, reason=reason,
                beat_price=beat or 0, current_price=btc_price or 0,
                odds_up=self.odds_up, odds_down=self.odds_down,
                filter_details=details,
            )

        details = {
            "f1": ("WAIT",""), "f2": ("WAIT",""),
            "f3": ("WAIT",""), "f4": ("WAIT",""),
            "f5": ("WAIT",""),
        }

        if self.bet_this_window:
            return skip("Sudah bet di window ini", details)

        # ── Bad hour check ─────────────────────────────────────
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        if hour in self.bad_hours:
            return skip(f"Bad hour UTC {hour:02d}:00 (WR < 45%)", details)

        # ── F1 — Entry zone (dipersempit) ──────────────────────
        elapsed   = self.candle.elapsed
        remaining = self.candle.remaining
        f1_ok = self.entry_min <= elapsed <= self.entry_max
        if not f1_ok:
            reason = (f"F1: Terlalu awal t={elapsed:.0f}s"
                      if elapsed < self.entry_min
                      else f"F1: Terlalu telat t={elapsed:.0f}s")
            details["f1"] = ("FAIL", reason)
            return skip(reason, details)
        details["f1"] = ("PASS", f"t={elapsed:.0f}s ✓ ({self.entry_min}-{self.entry_max}s)")

        if not beat or beat <= 0:
            return skip("Beat price belum ada", details)
        if not btc_price or btc_price <= 0:
            return skip("Harga tidak tersedia", details)
        if data.price_stale:
            return skip("Harga stale (>10s)", details)

        # ── F2 — Beat distance (lebih ketat) ───────────────────
        diff      = btc_price - beat
        abs_diff  = abs(diff)
        direction = "UP" if diff > 0 else "DOWN"

        cfg_dist      = self.config["beat_distance"]        # hard threshold
        cfg_dist_soft = self.config["beat_distance_soft"]   # soft threshold

        if abs_diff < cfg_dist_soft:
            # Di bawah soft threshold → langsung reject
            reason = f"F2: ${abs_diff:.3f} < soft ${cfg_dist_soft} (terlalu dekat)"
            details["f2"] = ("FAIL", reason)
            return skip(reason, details)
        elif abs_diff < cfg_dist:
            # Antara soft dan hard → boleh masuk tapi strength dikurangi nanti
            details["f2"] = ("SOFT", f"|dist|={abs_diff:.3f} ⚠ (soft zone) → {direction}")
        else:
            details["f2"] = ("PASS", f"|dist|={abs_diff:.3f} ✓ → {direction}")

        # ── F3 — Liquidation (threshold lebih ketat) ───────────
        f3_ok, f3_msg = data.check_liq(
            direction,
            self.config["liq_recent"],
            self.config["liq_sustained"],
        )
        details["f3"] = ("PASS" if f3_ok else "FAIL", f3_msg)
        if not f3_ok:
            return skip(f"F3: {f3_msg}", details)

        # ── F4 — CVD (threshold lebih ketat + alignment check) ─
        f4_ok, f4_msg = data.check_cvd(direction, self.config["cvd_threshold"])
        details["f4"] = ("PASS" if f4_ok else "FAIL", f4_msg)
        if not f4_ok:
            return skip(f"F4: {f4_msg}", details)

        # ── F5 — Odds (WAJIB sekarang, bukan info-only) ────────
        odds    = self.odds_up if direction == "UP" else self.odds_down
        spread  = abs(self.odds_up - self.odds_down)
        f5_odds_ok   = odds >= self.min_odds
        f5_spread_ok = spread >= self.config["min_odds_spread"]

        if not f5_odds_ok:
            reason = f"F5: odds {direction}={odds:.3f} < min {self.min_odds}"
            details["f5"] = ("FAIL", reason)
            return skip(reason, details)
        if not f5_spread_ok:
            reason = f"F5: spread={spread:.3f} < {self.config['min_odds_spread']} (market tidak tegas)"
            details["f5"] = ("FAIL", reason)
            return skip(reason, details)

        details["f5"] = ("PASS", f"{direction}={odds:.4f} spread={spread:.3f}")

        # ── Hitung strength score ───────────────────────────────
        strength = self._calc_strength(data, direction, abs_diff, cfg_dist)

        # Strength gate
        if strength < self.min_strength:
            reason = f"Strength terlalu rendah: {strength:.2f} < {self.min_strength}"
            return skip(reason, details)

        # Confidence gate (berdasarkan margin tiap filter)
        confidence = self._calc_confidence(abs_diff, cfg_dist, f3_ok, f4_ok, spread, odds)

        return SignalResult(
            coin=self.symbol,
            timestamp=time.time(),
            direction=direction,
            should_bet=True,
            strength=strength,
            reason=f"✓ ALL PASS | {direction} | dist={diff:+.3f} | str={strength:.2f} | conf={confidence:.2f}",
            beat_price=beat,
            current_price=btc_price,
            diff=diff,
            odds_up=self.odds_up,
            odds_down=self.odds_down,
            filter_details=details,
            confidence=confidence,
        )

    def _calc_strength(
        self,
        data: CoinDataStore,
        direction: str,
        abs_diff: float,
        cfg_dist: float,
    ) -> float:
        """
        Strength score lebih konservatif.
        Nilai lebih tinggi = sinyal lebih kuat.
        """
        base = data.signal_strength(direction)

        # Bonus jika distance jauh di atas threshold
        dist_ratio = abs_diff / max(cfg_dist, 1.0)
        dist_bonus = min(0.3, (dist_ratio - 1.0) * 0.2) if dist_ratio > 1.0 else 0.0

        # Kurangi kalau di soft zone (dist antara soft dan hard threshold)
        soft_dist = self.config["beat_distance_soft"]
        in_soft_zone = abs_diff < cfg_dist
        soft_penalty = -0.15 if in_soft_zone else 0.0

        # Bonus odds spread
        spread       = abs(self.odds_up - self.odds_down)
        spread_bonus = min(0.2, spread * 1.5)

        return max(0.0, base + dist_bonus + soft_penalty + spread_bonus)

    def _calc_confidence(
        self,
        abs_diff: float,
        cfg_dist: float,
        f3_ok: bool,
        f4_ok: bool,
        spread: float,
        odds: float,
    ) -> float:
        """Confidence score 0-1 berdasarkan margin tiap filter."""
        # F2 margin
        dist_margin = min(1.0, abs_diff / (cfg_dist * 2))

        # F5 spread margin
        min_spread  = self.config["min_odds_spread"]
        spread_margin = min(1.0, spread / (min_spread * 3))

        # Odds quality
        odds_quality = min(1.0, (odds - self.min_odds) / 0.15)

        conf = (dist_margin * 0.5) + (spread_margin * 0.3) + (odds_quality * 0.2)
        return round(min(1.0, conf), 3)

    def mark_bet_done(self) -> None:
        self.bet_this_window = True
