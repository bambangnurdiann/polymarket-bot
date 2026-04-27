"""
engine/coin_engine.py
=====================
CoinEngine — versi fixed + improved.

FIX KRITIS:
  - tick() sekarang set beat dari Chainlink terlebih dahulu
  - Hyperliquid hanya sebagai fallback jika CL tidak tersedia
  - Log peringatan jika beat tidak reliable

IMPROVE (dari versi sebelumnya):
  - Beat distance threshold lebih ketat
  - Entry zone dipersempit
  - Odds spread wajib >= 0.05
  - Confidence gate
  - Bad hour filter built-in
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

# ── Konfigurasi per coin ──────────────────────────────────────
COIN_CONFIG = {
    "BTC": {
        "beat_distance":       60,
        "beat_distance_soft":  40,
        "liq_recent":          20_000,
        "liq_sustained":       60_000,
        "cvd_threshold":       30_000,
        "min_odds_spread":     0.05,
        "entry_min":           230,
        "entry_max":           270,
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

BAD_HOURS_DEFAULT = {2, 4, 7}


@dataclass
class SignalResult:
    """Hasil evaluasi filter untuk satu coin di satu window."""
    coin:             str
    timestamp:        float
    direction:        str
    should_bet:       bool
    strength:         float
    reason:           str
    beat_price:       float = 0.0
    current_price:    float = 0.0
    diff:             float = 0.0
    odds_up:          float = 0.5
    odds_down:        float = 0.5
    filter_details:   dict  = field(default_factory=dict)
    chainlink_signal: Optional[object] = None
    mode:             str   = "LATE"
    confidence:       float = 0.0
    beat_source:      str   = "UNKNOWN"    # NEW: tracking sumber beat
    beat_reliable:    bool  = False        # NEW: apakah beat akurat

    @property
    def odds(self) -> float:
        return self.odds_up if self.direction == "UP" else self.odds_down


class CoinEngine:
    """
    State machine untuk satu coin.

    FIX KRITIS: Beat price sekarang dari Chainlink, bukan Hyperliquid.
    """

    def __init__(
        self,
        symbol:             str,
        entry_min:          float = 230,
        entry_max:          float = 270,
        min_odds:           float = 0.45,
        chainlink_monitor=None,
        cl_min_edge:        float = 0.10,
        cl_min_remaining:   float = 60,
        cl_max_remaining:   float = 240,
        min_strength:       float = 0.4,
        bad_hours:          set   = None,
    ):
        self.symbol       = symbol.upper()
        self.entry_min    = entry_min
        self.entry_max    = entry_max
        self.min_odds     = min_odds
        self.min_strength = min_strength
        self.bad_hours    = bad_hours if bad_hours is not None else BAD_HOURS_DEFAULT

        cfg = COIN_CONFIG.get(self.symbol, DEFAULT_CONFIG)
        self.config = {**DEFAULT_CONFIG, **cfg}

        if entry_min == 230:
            self.entry_min = self.config.get("entry_min", 230)
        if entry_max == 270:
            self.entry_max = self.config.get("entry_max", 270)

        self.cl_monitor  = chainlink_monitor
        self.cl_min_edge = cl_min_edge
        self.cl_min_rem  = cl_min_remaining
        self.cl_max_rem  = cl_max_remaining

        self.candle          = CandleTracker()
        self.bet_this_window = False
        self.last_result:    Optional[SignalResult] = None
        self.odds_up:        float = 0.5
        self.odds_down:      float = 0.5
        self._last_window:   str   = ""

    def update_odds(self, odds_up: float, odds_down: float) -> None:
        self.odds_up   = odds_up
        self.odds_down = odds_down

    def tick(self, data: CoinDataStore) -> "SignalResult":
        """
        FIX KRITIS: Beat price sekarang diambil dari Chainlink terlebih dahulu.
        Hyperliquid hanya sebagai fallback jika Chainlink tidak tersedia.
        """
        self.candle.update()

        if self.candle.window_id != self._last_window:
            self.bet_this_window = False
            self._last_window    = self.candle.window_id

        btc_price = data.price
        beat      = self.candle.beat_price

        # ── FIX: Set beat dari Chainlink terlebih dahulu ──────
        # Chainlink = harga yang dipakai Polymarket sebagai "price to beat"
        if self.cl_monitor:
            cl_price = self.cl_monitor.get_price(self.symbol)
            if cl_price and cl_price > 0:
                if not self.candle.is_beat_reliable:
                    set_ok = self.candle.set_beat_from_chainlink(cl_price)
                    if set_ok:
                        beat = self.candle.beat_price
                        logger.debug(
                            f"[{self.symbol}] Beat set dari Chainlink: "
                            f"${cl_price:,.2f} (t={self.candle.elapsed:.0f}s)"
                        )

        # ── Fallback: Hyperliquid hanya jika Chainlink tidak ada ──
        if self.candle.beat_price is None and btc_price and self.candle.elapsed < 5:
            self.candle.set_beat_from_hyperliquid(btc_price)
            beat = self.candle.beat_price
            logger.warning(
                f"[{self.symbol}] Beat fallback ke Hyperliquid: "
                f"${btc_price:,.2f} — kemungkinan beda dari Polymarket!"
            )

        # ── Log peringatan jika beat tidak reliable ───────────
        warn = self.candle.beat_warning
        if warn:
            logger.warning(f"[{self.symbol}] ⚠️ {warn}")

        # F0: Chainlink Mispricing
        if self.cl_monitor and beat and beat > 0:
            cl_result = self._check_chainlink_f0(beat)
            if cl_result:
                # Inject beat source info ke result
                cl_result.beat_source   = self.candle.beat_source
                cl_result.beat_reliable = self.candle.is_beat_reliable
                self.last_result = cl_result
                return cl_result

        # F1–F5: Late Bot filters
        result = self._evaluate(data, btc_price, beat)
        # Inject beat source info ke result
        result.beat_source   = self.candle.beat_source
        result.beat_reliable = self.candle.is_beat_reliable
        self.last_result = result
        return result

    def _check_chainlink_f0(self, beat: float) -> Optional["SignalResult"]:
        """F0: Chainlink Mispricing."""
        if self.bet_this_window:
            return None

        remaining = self.candle.remaining
        if not (self.cl_min_rem <= remaining <= self.cl_max_rem):
            return None

        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        if hour in self.bad_hours:
            return None

        cl = self.cl_monitor
        best_signal    = None
        best_direction = None

        for direction in ["UP", "DOWN"]:
            odds   = self.odds_up if direction == "UP" else self.odds_down
            spread = abs(self.odds_up - self.odds_down)
            if odds < self.min_odds:
                continue
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

        if signal.confidence < 0.55:
            return None

        strength = min(2.0, signal.confidence * (1 + signal.edge * 5))
        if strength < self.min_strength:
            return None

        vol_info = cl.get_vol_info(self.symbol)
        details  = {
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
    ) -> "SignalResult":
        """Jalankan filter F1–F5."""

        def skip(reason: str, details: dict = {}) -> "SignalResult":
            return SignalResult(
                coin=self.symbol, timestamp=time.time(),
                direction="", should_bet=False,
                strength=0.0, reason=reason,
                beat_price=beat or 0, current_price=btc_price or 0,
                odds_up=self.odds_up, odds_down=self.odds_down,
                filter_details=details,
            )

        details = {
            "f1": ("WAIT", ""), "f2": ("WAIT", ""),
            "f3": ("WAIT", ""), "f4": ("WAIT", ""),
            "f5": ("WAIT", ""),
        }

        if self.bet_this_window:
            return skip("Sudah bet di window ini", details)

        # Bad hour check
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        if hour in self.bad_hours:
            return skip(f"Bad hour UTC {hour:02d}:00 (WR < 45%)", details)

        # F1: Entry zone
        elapsed   = self.candle.elapsed
        remaining = self.candle.remaining
        f1_ok     = self.entry_min <= elapsed <= self.entry_max
        if not f1_ok:
            reason = (
                f"F1: Terlalu awal t={elapsed:.0f}s"
                if elapsed < self.entry_min
                else f"F1: Terlalu telat t={elapsed:.0f}s"
            )
            details["f1"] = ("FAIL", reason)
            return skip(reason, details)
        details["f1"] = ("PASS", f"t={elapsed:.0f}s ✓ ({self.entry_min}-{self.entry_max}s)")

        if not beat or beat <= 0:
            return skip("Beat price belum ada", details)
        if not btc_price or btc_price <= 0:
            return skip("Harga tidak tersedia", details)
        if data.price_stale:
            return skip("Harga stale (>10s)", details)

        # F2: Beat distance
        diff      = btc_price - beat
        abs_diff  = abs(diff)
        direction = "UP" if diff > 0 else "DOWN"

        cfg_dist      = self.config["beat_distance"]
        cfg_dist_soft = self.config["beat_distance_soft"]

        if abs_diff < cfg_dist_soft:
            reason = f"F2: ${abs_diff:.3f} < soft ${cfg_dist_soft} (terlalu dekat)"
            details["f2"] = ("FAIL", reason)
            return skip(reason, details)
        elif abs_diff < cfg_dist:
            details["f2"] = ("SOFT", f"|dist|={abs_diff:.3f} ⚠ → {direction}")
        else:
            details["f2"] = ("PASS", f"|dist|={abs_diff:.3f} ✓ → {direction}")

        # F3: Liquidation
        f3_ok, f3_msg = data.check_liq(
            direction,
            self.config["liq_recent"],
            self.config["liq_sustained"],
        )
        details["f3"] = ("PASS" if f3_ok else "FAIL", f3_msg)
        if not f3_ok:
            return skip(f"F3: {f3_msg}", details)

        # F4: CVD
        f4_ok, f4_msg = data.check_cvd(direction, self.config["cvd_threshold"])
        details["f4"] = ("PASS" if f4_ok else "FAIL", f4_msg)
        if not f4_ok:
            return skip(f"F4: {f4_msg}", details)

        # F5: Odds
        odds         = self.odds_up if direction == "UP" else self.odds_down
        spread       = abs(self.odds_up - self.odds_down)
        f5_odds_ok   = odds >= self.min_odds
        f5_spread_ok = spread >= self.config["min_odds_spread"]

        if not f5_odds_ok:
            reason = f"F5: odds {direction}={odds:.3f} < min {self.min_odds}"
            details["f5"] = ("FAIL", reason)
            return skip(reason, details)
        if not f5_spread_ok:
            reason = f"F5: spread={spread:.3f} < {self.config['min_odds_spread']}"
            details["f5"] = ("FAIL", reason)
            return skip(reason, details)
        details["f5"] = ("PASS", f"{direction}={odds:.4f} spread={spread:.3f}")

        # Strength & confidence
        strength   = self._calc_strength(data, direction, abs_diff, cfg_dist)
        if strength < self.min_strength:
            return skip(f"Strength terlalu rendah: {strength:.2f}", details)

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
        data:      CoinDataStore,
        direction: str,
        abs_diff:  float,
        cfg_dist:  float,
    ) -> float:
        base      = data.signal_strength(direction)
        dist_ratio = abs_diff / max(cfg_dist, 1.0)
        dist_bonus = min(0.3, (dist_ratio - 1.0) * 0.2) if dist_ratio > 1.0 else 0.0
        soft_dist  = self.config["beat_distance_soft"]
        in_soft    = abs_diff < cfg_dist
        soft_pen   = -0.15 if in_soft else 0.0
        spread     = abs(self.odds_up - self.odds_down)
        spread_bon = min(0.2, spread * 1.5)
        return max(0.0, base + dist_bonus + soft_pen + spread_bon)

    def _calc_confidence(
        self,
        abs_diff: float,
        cfg_dist: float,
        f3_ok:    bool,
        f4_ok:    bool,
        spread:   float,
        odds:     float,
    ) -> float:
        dist_margin   = min(1.0, abs_diff / (cfg_dist * 2))
        min_spread    = self.config["min_odds_spread"]
        spread_margin = min(1.0, spread / (min_spread * 3))
        odds_quality  = min(1.0, (odds - self.min_odds) / 0.15)
        conf = (dist_margin * 0.5) + (spread_margin * 0.3) + (odds_quality * 0.2)
        return round(min(1.0, conf), 3)

    def mark_bet_done(self) -> None:
        self.bet_this_window = True