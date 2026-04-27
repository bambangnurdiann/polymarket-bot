"""
engine/strategy_v2.py
=====================
Strategy Engine v2 — Perbaikan fundamental dari analisa kelemahan bot.

PERUBAHAN UTAMA vs coin_engine.py:
  1. Chainlink Round Boundary Detection (edge utama)
  2. Dynamic Entry Window berdasarkan fase window
  3. Adaptive CVD threshold (waktu sepi vs ramai)
  4. Polymarket Odds Momentum (cek arah perubahan odds, bukan hanya nilai)
  5. Beat Price Convergence Guard (jangan bet jika harga konvergen ke beat)
  6. Multi-timeframe CVD confluence (1m + 2m harus aligned)
  7. Expected Value filter berbasis odds aktual vs fair odds

KONSEP KUNCI:
  Window 5 menit dibagi 3 fase:
    - EARLY   (t=0-90s)   : Chainlink arb zone — hanya bet jika ada round baru
    - MIDDLE  (t=90-210s) : Trend confirmation zone — bet jika CVD + liq kuat
    - LATE    (t=210-270s): Momentum zone — bet hanya jika sinyal sangat kuat
    - DANGER  (t=270s+)   : Skip — terlalu dekat resolve, volatility tinggi

KENAPA INI LEBIH BAIK:
  - EARLY zone dengan round baru punya edge nyata (5-10s sebelum odds adjust)
  - MIDDLE zone punya lebih banyak data CVD untuk konfirmasi
  - Threshold adaptif mengurangi false positive di jam sepi
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict

logger = logging.getLogger(__name__)


# ── Konfigurasi per fase window ───────────────────────────────
PHASE_CONFIG = {
    "EARLY": {
        "t_start": 0, "t_end": 90,
        "require_cl_round": True,   # WAJIB ada Chainlink round baru
        "cl_min_edge": 0.08,        # edge lebih rendah karena timing lebih baik
        "cvd_threshold": 15_000,    # threshold lebih rendah (masih early)
        "liq_recent": 10_000,
        "liq_sustained": 30_000,
        "beat_distance_min": 40,    # jarak minimum dari beat
        "min_confidence": 0.60,
    },
    "MIDDLE": {
        "t_start": 90, "t_end": 210,
        "require_cl_round": False,  # tidak wajib, tapi bonus jika ada
        "cl_min_edge": 0.10,
        "cvd_threshold": 25_000,    # threshold standar
        "liq_recent": 18_000,
        "liq_sustained": 55_000,
        "beat_distance_min": 55,
        "min_confidence": 0.55,
    },
    "LATE": {
        "t_start": 210, "t_end": 265,
        "require_cl_round": False,
        "cl_min_edge": 0.13,        # edge lebih tinggi karena timing lebih berisiko
        "cvd_threshold": 35_000,    # threshold lebih ketat
        "liq_recent": 22_000,
        "liq_sustained": 65_000,
        "beat_distance_min": 65,    # jarak harus lebih jauh
        "min_confidence": 0.65,     # confidence lebih tinggi
    },
    "DANGER": {
        "t_start": 265, "t_end": 300,
        "skip": True,               # selalu skip
    }
}

# Jam UTC yang terbukti WR < 45% — blok otomatis
BAD_HOURS_DEFAULT = {2, 4, 7}

# Volatility tinggi = threshold CVD dinaikkan otomatis
HIGH_VOL_MULTIPLIER = 1.4
LOW_VOL_MULTIPLIER  = 0.8


@dataclass
class PhaseSignal:
    """Hasil evaluasi signal untuk satu fase."""
    phase:          str
    coin:           str
    direction:      str
    should_bet:     bool
    strength:       float
    confidence:     float
    reason:         str
    edge:           float = 0.0
    beat_distance:  float = 0.0
    cvd_aligned:    bool  = False
    liq_aligned:    bool  = False
    cl_round_new:   bool  = False
    odds_momentum:  str   = ""   # "RISING", "FALLING", "STABLE"
    filter_details: dict  = field(default_factory=dict)
    mode:           str   = "LATE_V2"
    chainlink_signal: Optional[object] = None

    @property
    def odds(self) -> float:
        return self.edge  # placeholder, diisi dari luar


class StrategyV2:
    """
    Strategy engine v2 dengan fase-based entry dan Chainlink round detection.

    Cara pakai:
        engine = StrategyV2("BTC", chainlink_monitor=cl_monitor)
        signal = engine.evaluate(data, odds_up, odds_down, candle)
        if signal.should_bet:
            execute_bet(signal.coin, signal.direction, ...)
    """

    def __init__(
        self,
        symbol:             str,
        chainlink_monitor=None,
        min_odds:           float = 0.45,
        bad_hours:          set   = None,
        # Override config per coin (opsional)
        beat_distance_btc:  float = 60,
    ):
        self.symbol           = symbol.upper()
        self.cl_monitor       = chainlink_monitor
        self.min_odds         = min_odds
        self.bad_hours        = bad_hours or BAD_HOURS_DEFAULT
        self.beat_distance_btc = beat_distance_btc

        # State tracking
        self.bet_this_window  = False
        self._last_window:    str   = ""
        self._last_cl_round:  int   = 0    # round_id terakhir yang terdeteksi
        self._cl_round_ts:    float = 0.0  # kapan round baru pertama kali terdeteksi
        self._odds_history:   list  = []   # [(ts, odds_up, odds_down), ...]
        self._vol_estimate:   float = 0.001

    # ── Main entry point ──────────────────────────────────────

    def evaluate(
        self,
        data,           # CoinDataStore
        odds_up:   float,
        odds_down: float,
        candle,         # CandleTracker
    ) -> Optional[PhaseSignal]:
        """
        Evaluasi sinyal untuk window saat ini.
        Returns PhaseSignal atau None jika kondisi tidak terpenuhi.
        """
        # Update state
        candle.update()
        self._update_odds_history(odds_up, odds_down)

        # Reset window
        if candle.window_id != self._last_window:
            self.bet_this_window = False
            self._last_window    = candle.window_id
            self._odds_history   = []

        if self.bet_this_window:
            return None

        elapsed = candle.elapsed
        price   = data.get_price()
        beat    = candle.beat_price

        if not price or not beat or beat <= 0:
            return None

        # Bad hour check
        hour = datetime.now(timezone.utc).hour
        if hour in self.bad_hours:
            return None

        # Tentukan fase
        phase_name, phase_cfg = self._get_phase(elapsed)
        if phase_cfg.get("skip"):
            return None

        # Spread check (global, semua fase)
        spread = abs(odds_up - odds_down)
        if spread < 0.04:
            return None

        # Direction dari beat distance
        diff      = price - beat
        abs_diff  = abs(diff)
        direction = "UP" if diff > 0 else "DOWN"

        # Beat distance minimum per fase
        beat_min = self._get_beat_distance_min(phase_cfg)
        if abs_diff < beat_min:
            return None

        # Odds untuk direction ini
        odds = odds_up if direction == "UP" else odds_down
        if odds < self.min_odds:
            return None

        # ── Chainlink round boundary check ────────────────────
        cl_round_new, cl_delta, cl_signal = self._check_chainlink(
            direction, beat, candle.remaining, odds, odds_up, odds_down,
            phase_cfg
        )

        # Jika fase EARLY dan tidak ada round baru, skip
        if phase_name == "EARLY" and phase_cfg.get("require_cl_round") and not cl_round_new:
            return None

        # ── CVD check ─────────────────────────────────────────
        cvd_threshold = self._adaptive_cvd_threshold(phase_cfg["cvd_threshold"])
        cvd_ok, cvd_msg = self._check_cvd(data, direction, cvd_threshold)

        # ── Multi-timeframe CVD confluence ────────────────────
        cvd_confluence = self._check_cvd_confluence(data, direction)

        # ── Liquidation check ──────────────────────────────────
        liq_ok, liq_msg = data.check_liq(
            direction,
            phase_cfg["liq_recent"],
            phase_cfg["liq_sustained"],
        )

        # ── Odds momentum ─────────────────────────────────────
        odds_mom = self._calc_odds_momentum(direction)

        # ── Beat convergence guard ────────────────────────────
        converging = self._is_converging_to_beat(data, direction, beat)
        if converging:
            return None

        # ── Scoring ───────────────────────────────────────────
        score, confidence, details = self._score(
            phase_name=phase_name,
            cl_round_new=cl_round_new,
            cl_signal=cl_signal,
            cvd_ok=cvd_ok,
            cvd_confluence=cvd_confluence,
            liq_ok=liq_ok,
            odds_mom=odds_mom,
            abs_diff=abs_diff,
            beat_min=beat_min,
            spread=spread,
            odds=odds,
            elapsed=elapsed,
            details={
                "cvd": cvd_msg,
                "liq": liq_msg,
                "cl_round_new": cl_round_new,
                "cl_delta": f"${cl_delta:+.2f}" if cl_delta else "N/A",
                "odds_momentum": odds_mom,
                "beat_dist": f"${abs_diff:.2f}",
                "spread": f"{spread:.3f}",
                "phase": phase_name,
            }
        )

        # Confidence gate per fase
        if confidence < phase_cfg["min_confidence"]:
            return None

        # EV check — pastikan odds layak
        if not self._ev_positive(odds, confidence):
            return None

        should_bet = cvd_ok and liq_ok and score >= 0.4

        # Fase EARLY: cukup dengan CL round + cvd_ok (liq bisa skip)
        if phase_name == "EARLY" and cl_round_new and cl_signal:
            should_bet = cvd_ok and score >= 0.35

        return PhaseSignal(
            phase=phase_name,
            coin=self.symbol,
            direction=direction,
            should_bet=should_bet,
            strength=score,
            confidence=confidence,
            reason=self._build_reason(phase_name, cl_round_new, cvd_ok, liq_ok, score),
            edge=cl_signal.edge if cl_signal else 0.0,
            beat_distance=abs_diff,
            cvd_aligned=cvd_ok,
            liq_aligned=liq_ok,
            cl_round_new=cl_round_new,
            odds_momentum=odds_mom,
            filter_details=details,
            mode="EARLY_CL" if (phase_name == "EARLY" and cl_round_new) else "LATE_V2",
            chainlink_signal=cl_signal,
        )

    # ── Chainlink round boundary ──────────────────────────────

    def _check_chainlink(
        self,
        direction:  str,
        beat:       float,
        remaining:  float,
        odds:       float,
        odds_up:    float,
        odds_down:  float,
        phase_cfg:  dict,
    ) -> Tuple[bool, float, Optional[object]]:
        """
        Deteksi Chainlink round baru dan hitung edge.

        Returns:
            (is_new_round, price_delta, mispricing_signal)
        """
        if not self.cl_monitor:
            return False, 0.0, None

        snap     = self.cl_monitor.prices.get(self.symbol)
        prev     = self.cl_monitor.prev_prices.get(self.symbol)
        is_new   = self.cl_monitor.new_round.get(self.symbol, False)

        if not snap:
            return False, 0.0, None

        # Hitung delta dari round sebelumnya
        delta = 0.0
        if prev and snap.round_id != prev.round_id:
            delta = snap.price - prev.price
            # Cek apakah round baru sesuai arah sinyal
            if direction == "UP" and delta < 0:
                return False, delta, None   # round baru tapi berlawanan arah
            if direction == "DOWN" and delta > 0:
                return False, delta, None

        min_edge = phase_cfg.get("cl_min_edge", 0.10)
        signal   = self.cl_monitor.detect_mispricing(
            coin=self.symbol,
            direction=direction,
            beat_price=beat,
            remaining=remaining,
            current_odds=odds,
            min_edge=min_edge,
            odds_up=odds_up,
            odds_down=odds_down,
            use_momentum=True,
            use_time_decay=True,
            min_odds_spread=0.04,
        )

        return is_new, delta, signal

    # ── CVD checks ────────────────────────────────────────────

    def _check_cvd(self, data, direction: str, threshold: float) -> Tuple[bool, str]:
        """CVD 2min check dengan adaptive threshold."""
        return data.check_cvd(direction, threshold)

    def _check_cvd_confluence(self, data, direction: str) -> bool:
        """
        Multi-timeframe CVD confluence:
        CVD 1min dan 2min harus aligned dengan arah sinyal.
        """
        cvd_1m = data.cvd_1min
        cvd_2m = data.cvd_2min

        if cvd_1m == 0 or cvd_2m == 0:
            return True  # Data tidak ada, tidak blokir

        if direction == "UP":
            return cvd_1m > 0 and cvd_2m > 0
        else:
            return cvd_1m < 0 and cvd_2m < 0

    def _adaptive_cvd_threshold(self, base: float) -> float:
        """
        Adaptive threshold: turunkan di jam sepi (volume rendah),
        naikkan di jam ramai (volume tinggi, lebih noise).
        """
        hour = datetime.now(timezone.utc).hour
        # Jam sepi Asia/weekend: 2-8 UTC
        if 2 <= hour <= 8:
            return base * LOW_VOL_MULTIPLIER
        # Jam ramai US: 13-21 UTC
        if 13 <= hour <= 21:
            return base * HIGH_VOL_MULTIPLIER
        return base

    # ── Odds momentum ─────────────────────────────────────────

    def _update_odds_history(self, odds_up: float, odds_down: float) -> None:
        now = time.time()
        self._odds_history.append((now, odds_up, odds_down))
        # Keep 60 detik terakhir
        cutoff = now - 60
        self._odds_history = [(t, u, d) for t, u, d in self._odds_history if t >= cutoff]

    def _calc_odds_momentum(self, direction: str) -> str:
        """
        Cek apakah odds untuk direction ini sedang naik atau turun.
        RISING = market semakin yakin = bagus untuk bet arah ini.
        FALLING = market mulai ragu = peringatan.
        """
        if len(self._odds_history) < 4:
            return "STABLE"

        idx = 1 if direction == "UP" else 2  # odds_up atau odds_down

        # Bandingkan 30 detik pertama vs 30 detik terakhir
        now = time.time()
        recent = [h[idx] for h in self._odds_history if h[0] >= now - 20]
        older  = [h[idx] for h in self._odds_history if h[0] < now - 20]

        if not recent or not older:
            return "STABLE"

        avg_recent = sum(recent) / len(recent)
        avg_older  = sum(older)  / len(older)
        delta      = avg_recent - avg_older

        if delta > 0.01:
            return "RISING"
        elif delta < -0.01:
            return "FALLING"
        return "STABLE"

    # ── Beat convergence guard ────────────────────────────────

    def _is_converging_to_beat(self, data, direction: str, beat: float) -> bool:
        """
        Jangan bet jika harga bergerak MENDEKATI beat (bukan menjauhi).
        Ini menandakan momentum lemah dan kemungkinan reversal.

        Cek: apakah harga 1 menit terakhir bergerak searah beat?
        """
        # Gunakan CVD 1min sebagai proxy momentum jangka pendek
        cvd_1m = data.cvd_1min

        if abs(cvd_1m) < 5_000:
            return False  # CVD lemah, tidak bisa baca convergence

        price = data.get_price()
        if not price or not beat:
            return False

        diff = price - beat

        # Jika arah bet UP tapi CVD 1min negatif (jual), kemungkinan konvergen
        if direction == "UP" and diff > 0 and cvd_1m < -8_000:
            return True
        # Jika arah bet DOWN tapi CVD 1min positif (beli), kemungkinan konvergen
        if direction == "DOWN" and diff < 0 and cvd_1m > 8_000:
            return True

        return False

    # ── Phase detection ───────────────────────────────────────

    def _get_phase(self, elapsed: float) -> Tuple[str, dict]:
        for name, cfg in PHASE_CONFIG.items():
            if cfg.get("skip") and elapsed >= cfg["t_start"]:
                return name, cfg
            if cfg.get("t_start", 0) <= elapsed < cfg.get("t_end", 999):
                return name, cfg
        return "DANGER", {"skip": True}

    def _get_beat_distance_min(self, phase_cfg: dict) -> float:
        base = phase_cfg.get("beat_distance_min", 55)
        # BTC-specific adjustment
        if self.symbol == "BTC":
            return max(base, self.beat_distance_btc)
        return base

    # ── EV check ──────────────────────────────────────────────

    def _ev_positive(self, odds: float, confidence: float) -> bool:
        """
        Pastikan Expected Value positif.
        EV = (confidence * payout) - (1 - confidence)
        Di mana payout = 1/odds - 1
        """
        if odds <= 0 or odds >= 1:
            return False
        payout = (1 / odds) - 1
        ev = (confidence * payout) - (1 - confidence)
        return ev > -0.02  # Toleransi kecil untuk uncertainty

    # ── Scoring ───────────────────────────────────────────────

    def _score(
        self,
        phase_name:      str,
        cl_round_new:    bool,
        cl_signal,
        cvd_ok:          bool,
        cvd_confluence:  bool,
        liq_ok:          bool,
        odds_mom:        str,
        abs_diff:        float,
        beat_min:        float,
        spread:          float,
        odds:            float,
        elapsed:         float,
        details:         dict,
    ) -> Tuple[float, float, dict]:
        """
        Hitung strength score dan confidence.

        Score komponen (total max ~2.0):
          CL round new    : +0.4 (edge terbesar)
          CL edge tinggi  : +0.3
          CVD ok          : +0.3
          CVD confluence  : +0.15
          Liq ok          : +0.25
          Odds momentum   : +0.1 (RISING) / -0.1 (FALLING)
          Beat distance   : 0 - 0.2 bonus
          Spread          : 0 - 0.15 bonus
        """
        score = 0.0

        # CL round boundary (bonus terbesar)
        if cl_round_new:
            score += 0.4
        if cl_signal:
            edge_bonus = min(0.3, cl_signal.edge * 2.0)
            score += edge_bonus

        # CVD
        if cvd_ok:
            score += 0.30
        if cvd_confluence:
            score += 0.15

        # Liquidation
        if liq_ok:
            score += 0.25

        # Odds momentum
        if odds_mom == "RISING":
            score += 0.10
        elif odds_mom == "FALLING":
            score -= 0.10

        # Beat distance bonus
        dist_ratio = abs_diff / max(beat_min, 1)
        dist_bonus = min(0.20, (dist_ratio - 1.0) * 0.15) if dist_ratio > 1 else 0
        score += dist_bonus

        # Spread bonus
        spread_bonus = min(0.15, (spread - 0.04) * 1.5)
        score += spread_bonus

        # Confidence = weighted average dari filter yang pass
        filters_pass = sum([
            cvd_ok,
            cvd_confluence,
            liq_ok,
            cl_round_new,
            odds_mom == "RISING",
        ])
        total_filters = 5
        base_conf = filters_pass / total_filters

        # Boost dari CL edge jika ada
        cl_boost = min(0.2, cl_signal.edge * 1.5) if cl_signal else 0
        confidence = min(0.95, base_conf * 0.7 + cl_boost + (spread - 0.04) * 0.5)

        return round(score, 3), round(confidence, 3), details

    def _build_reason(
        self, phase: str, cl_new: bool, cvd_ok: bool, liq_ok: bool, score: float
    ) -> str:
        parts = [f"[{phase}]"]
        if cl_new:
            parts.append("CL-ROUND✓")
        if cvd_ok:
            parts.append("CVD✓")
        if liq_ok:
            parts.append("LIQ✓")
        parts.append(f"str={score:.2f}")
        return " | ".join(parts)

    def mark_bet_done(self) -> None:
        self.bet_this_window = True
