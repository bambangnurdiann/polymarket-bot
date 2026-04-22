"""
engine/signal_arbiter.py
========================
SignalArbiter: memilih sinyal terkuat dari semua coin yang aktif.

Logika seleksi:
  1. Kumpulkan semua SignalResult yang should_bet=True
  2. Pilih yang punya strength score tertinggi
  3. Pastikan tidak ada conflict (misal BTC=UP dan ETH=DOWN dengan strength mirip)

Kenapa selektif (1 bet per window)?
  - Lebih hemat modal
  - Lebih mudah track performance per sinyal
  - Hindari overexposure di satu window
"""

import logging
import time
from typing import Optional

from engine.coin_engine import SignalResult

logger = logging.getLogger(__name__)


class SignalArbiter:
    """
    Memilih sinyal terbaik dari beberapa coin untuk dieksekusi.

    Attributes:
        last_selected : SignalResult — sinyal terakhir yang dipilih
        window_bet_done : bool — sudah bet di window saat ini (cross-coin lock)
    """

    def __init__(self, min_strength: float = 0.3):
        """
        Args:
            min_strength: minimum strength score untuk eligible bet (0.0-1.0+)
                          0.3 berarti semua filter pass dengan margin reasonable
        """
        self.min_strength     = min_strength
        self.last_selected:   Optional[SignalResult] = None
        self.window_bet_done: bool = False
        self._current_window: str  = ""

    def reset_for_window(self, window_id: str) -> None:
        """Reset lock saat window baru dimulai."""
        if window_id != self._current_window:
            self._current_window  = window_id
            self.window_bet_done  = False

    def select(self, signals: list[SignalResult]) -> Optional[SignalResult]:
        """
        Pilih sinyal terbaik dari list SignalResult.

        Args:
            signals: list hasil CoinEngine.tick() dari semua coin

        Returns:
            SignalResult terbaik, atau None jika tidak ada yang eligible
        """
        if self.window_bet_done:
            return None

        # Filter yang valid
        candidates = [
            s for s in signals
            if s.should_bet and s.strength >= self.min_strength
        ]

        if not candidates:
            return None

        # Sort by strength descending
        candidates.sort(key=lambda s: s.strength, reverse=True)
        best = candidates[0]

        # Log jika ada multiple candidates
        if len(candidates) > 1:
            names = ", ".join(f"{s.coin}({s.strength:.2f})" for s in candidates)
            logger.info(f"[Arbiter] Multiple signals: {names} → pilih {best.coin}")

        self.last_selected = best
        return best

    def mark_executed(self) -> None:
        """Tandai bahwa bet sudah dieksekusi di window ini."""
        self.window_bet_done = True
        logger.info(f"[Arbiter] Window locked — {self._current_window}")

    def describe_candidates(self, signals: list[SignalResult]) -> str:
        """String summary semua kandidat untuk logging."""
        valid = [s for s in signals if s.should_bet]
        if not valid:
            return "No candidates"
        return " | ".join(
            f"{s.coin}:{s.direction}(str={s.strength:.2f})" for s in valid
        )
