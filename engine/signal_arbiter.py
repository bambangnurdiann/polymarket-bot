"""
engine/signal_arbiter.py
========================
SignalArbiter — versi improved.

Perubahan:
  - min_strength default dinaikkan: 0.3 → 0.4
  - Tambah conflict detection: jika 2 coin berlawanan arah dengan strength mirip,
    keduanya di-skip (ambiguous market condition)
  - Confidence gate tambahan
"""

import logging
import time
from typing import Optional

from engine.coin_engine import SignalResult

logger = logging.getLogger(__name__)


class SignalArbiter:
    """
    Memilih sinyal terbaik dari beberapa coin.

    Perubahan:
    - min_strength default 0.4 (naik dari 0.2)
    - Conflict detection: skip jika ada sinyal UP dan DOWN dengan strength < 20% beda
    - Confidence gate: hanya bet jika confidence >= min_confidence
    """

    def __init__(
        self,
        min_strength:   float = 0.4,
        min_confidence: float = 0.45,
        conflict_margin: float = 0.20,  # jika 2 sinyal berlawanan & strength beda < 20%, skip
    ):
        self.min_strength    = min_strength
        self.min_confidence  = min_confidence
        self.conflict_margin = conflict_margin

        self.last_selected:   Optional[SignalResult] = None
        self.window_bet_done: bool = False
        self._current_window: str  = ""

    def reset_for_window(self, window_id: str) -> None:
        if window_id != self._current_window:
            self._current_window  = window_id
            self.window_bet_done  = False

    def select(self, signals: list) -> Optional[SignalResult]:
        """
        Pilih sinyal terbaik dengan conflict detection.
        """
        if self.window_bet_done:
            return None

        # Filter berdasarkan strength dan confidence
        candidates = [
            s for s in signals
            if (s.should_bet
                and s.strength >= self.min_strength
                and getattr(s, "confidence", 1.0) >= self.min_confidence)
        ]

        if not candidates:
            return None

        # Conflict detection: cek apakah ada sinyal berlawanan arah
        up_signals   = [s for s in candidates if s.direction == "UP"]
        down_signals = [s for s in candidates if s.direction == "DOWN"]

        if up_signals and down_signals:
            best_up   = max(up_signals,   key=lambda s: s.strength)
            best_down = max(down_signals, key=lambda s: s.strength)
            diff      = abs(best_up.strength - best_down.strength)
            if diff < self.conflict_margin:
                logger.info(
                    f"[Arbiter] CONFLICT: {best_up.coin}UP({best_up.strength:.2f}) vs "
                    f"{best_down.coin}DOWN({best_down.strength:.2f}) — diff={diff:.2f} < {self.conflict_margin} → SKIP"
                )
                return None

        candidates.sort(key=lambda s: s.strength, reverse=True)
        best = candidates[0]

        if len(candidates) > 1:
            others = ", ".join(f"{s.coin}({s.strength:.2f})" for s in candidates if s.coin != best.coin)
            logger.info(f"[Arbiter] Multiple: {others} → pilih {best.coin}")

        self.last_selected = best
        return best

    def mark_executed(self) -> None:
        self.window_bet_done = True
        logger.info(f"[Arbiter] Window locked — {self._current_window}")

    def describe_candidates(self, signals: list) -> str:
        valid = [s for s in signals if s.should_bet]
        if not valid:
            return "No candidates"
        return " | ".join(
            f"{s.coin}:{s.direction}(str={s.strength:.2f},conf={getattr(s,'confidence',0):.2f})"
            for s in valid
        )
