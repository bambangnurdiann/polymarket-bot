"""
engine/circuit_breaker.py
=========================
Circuit Breaker — Proteksi loss streak dan drawdown.

Mencegah bot terus betting saat dalam kondisi losing streak
dengan menerapkan cooldown bertahap:

  Streak 3 → cooldown 1 window
  Streak 4 → cooldown 2 window
  Streak 5 → cooldown 3 window + alert
  Streak 6+ → HARD STOP, perlu manual resume via Telegram

Fitur:
  - Streak counter real-time
  - Drawdown limiter (max % dari starting balance)
  - Per-session loss limit
  - Auto resume setelah cooldown
  - Telegram alert saat triggered
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BreakerState:
    consecutive_losses: int = 0
    consecutive_wins:   int = 0
    cooldown_until:     float = 0.0   # unix timestamp
    cooldown_windows:   int = 0
    hard_stopped:       bool = False
    session_losses:     int = 0
    session_wins:       int = 0
    session_pnl:        float = 0.0
    last_result:        str = ""
    triggered_at:       float = 0.0
    trigger_reason:     str = ""


class CircuitBreaker:
    """
    Proteksi loss streak dengan cooldown bertahap.

    Config (semua bisa di-override via .env):
      MAX_CONSECUTIVE_LOSS : default 3 → cooldown dimulai
      HARD_STOP_LOSS       : default 5 → hard stop, perlu manual resume
      SESSION_MAX_LOSS     : default 8 → max loss per session
      COOLDOWN_WINDOW_MAP  : {streak: windows_cooldown}
    """

    # Berapa windows cooldown per streak level
    COOLDOWN_MAP = {
        3: 1,   # 3 loss → skip 1 window (~5 menit)
        4: 2,   # 4 loss → skip 2 windows
        5: 3,   # 5 loss → skip 3 windows
    }

    WINDOW_DURATION = 300  # 5 menit

    def __init__(
        self,
        max_streak:      int   = 3,
        hard_stop_streak: int  = 5,
        session_max_loss: int  = 8,
        max_drawdown_pct: float = 0.30,   # 30% dari saldo awal
        starting_balance: float = 0.0,
    ):
        self.max_streak       = max_streak
        self.hard_stop_streak = hard_stop_streak
        self.session_max_loss = session_max_loss
        self.max_drawdown_pct = max_drawdown_pct
        self.starting_balance = starting_balance

        self.state = BreakerState()
        self._tg_callback = None   # set via set_telegram_callback()

    def set_telegram_callback(self, callback) -> None:
        """Inject Telegram send function untuk alerting."""
        self._tg_callback = callback

    def _alert(self, msg: str) -> None:
        logger.warning(f"[CircuitBreaker] {msg}")
        if self._tg_callback:
            self._tg_callback(f"🔴 <b>Circuit Breaker</b>\n{msg}")

    def record_result(self, result: str, pnl: float) -> None:
        """
        Catat hasil bet. Panggil setelah setiap bet resolved.

        Args:
            result : "WIN" atau "LOSS"
            pnl    : profit/loss (negatif untuk loss)
        """
        s = self.state
        s.last_result  = result
        s.session_pnl += pnl

        if result == "WIN":
            s.consecutive_wins  += 1
            s.consecutive_losses = 0
            s.session_wins      += 1
            # Reset hard stop jika menang setelah pause
            if s.hard_stopped and s.consecutive_wins >= 2:
                s.hard_stopped   = False
                s.trigger_reason = ""
                self._alert("✅ Hard stop dilepas setelah 2 kemenangan berturut-turut")
        else:
            s.consecutive_losses += 1
            s.consecutive_wins    = 0
            s.session_losses     += 1
            self._evaluate_streak()

    def _evaluate_streak(self) -> None:
        """Evaluasi apakah perlu cooldown atau hard stop."""
        s = self.state
        streak = s.consecutive_losses

        # Hard stop
        if streak >= self.hard_stop_streak:
            s.hard_stopped   = True
            s.triggered_at   = time.time()
            s.trigger_reason = f"Hard stop: {streak} loss berturut-turut"
            self._alert(
                f"⛔ HARD STOP — {streak}x loss berturut-turut!\n"
                f"Bot TIDAK akan bet sampai kamu resume manual via /resume"
            )
            return

        # Session loss limit
        if s.session_losses >= self.session_max_loss:
            s.hard_stopped   = True
            s.trigger_reason = f"Session loss limit: {s.session_losses}"
            self._alert(
                f"⛔ Session limit tercapai — {s.session_losses} loss hari ini\n"
                f"Gunakan /resume untuk lanjut besok"
            )
            return

        # Cooldown bertahap
        if streak in self.COOLDOWN_MAP:
            windows = self.COOLDOWN_MAP[streak]
            cooldown_secs     = windows * self.WINDOW_DURATION
            s.cooldown_until  = time.time() + cooldown_secs
            s.cooldown_windows = windows
            s.triggered_at    = time.time()
            s.trigger_reason  = f"Cooldown: {streak}x loss"
            self._alert(
                f"⏸ Cooldown {windows} window(s) ({cooldown_secs//60:.0f} menit)\n"
                f"Loss streak: {streak}x\n"
                f"Resume otomatis setelah cooldown selesai"
            )

    def check_drawdown(self, current_balance: float) -> bool:
        """
        Cek apakah drawdown melebihi batas.
        Returns True jika drawdown OK, False jika harus stop.
        """
        if self.starting_balance <= 0:
            return True
        drawdown = (self.starting_balance - current_balance) / self.starting_balance
        if drawdown >= self.max_drawdown_pct:
            self.state.hard_stopped   = True
            self.state.trigger_reason = f"Max drawdown: {drawdown*100:.1f}%"
            self._alert(
                f"⛔ MAX DRAWDOWN TERCAPAI\n"
                f"Drawdown: {drawdown*100:.1f}% dari saldo awal ${self.starting_balance:.2f}\n"
                f"Saldo saat ini: ${current_balance:.2f}"
            )
            return False
        return True

    def can_bet(self) -> tuple:
        """
        Cek apakah bot boleh bet saat ini.

        Returns:
            (allowed: bool, reason: str)
        """
        s   = self.state
        now = time.time()

        # Hard stop
        if s.hard_stopped:
            return False, f"⛔ HARD STOP aktif ({s.trigger_reason}). Gunakan /resume"

        # Cooldown aktif
        if s.cooldown_until > now:
            remaining = s.cooldown_until - now
            return False, f"⏸ Cooldown {remaining:.0f}s tersisa (streak={s.consecutive_losses})"

        return True, "OK"

    def force_resume(self) -> None:
        """Manual resume dari Telegram /resume."""
        self.state.hard_stopped      = False
        self.state.cooldown_until    = 0.0
        self.state.consecutive_losses = 0
        self.state.trigger_reason    = ""
        self._alert("✅ Bot di-resume secara manual")

    def reset_session(self) -> None:
        """Reset counter untuk sesi baru."""
        self.state.session_losses = 0
        self.state.session_wins   = 0
        self.state.session_pnl    = 0.0

    @property
    def status_str(self) -> str:
        s   = self.state
        now = time.time()
        if s.hard_stopped:
            return f"HARD_STOP({s.trigger_reason})"
        if s.cooldown_until > now:
            return f"COOLDOWN({s.cooldown_until - now:.0f}s)"
        if s.consecutive_losses > 0:
            return f"STREAK_L{s.consecutive_losses}"
        if s.consecutive_wins > 0:
            return f"STREAK_W{s.consecutive_wins}"
        return "OK"