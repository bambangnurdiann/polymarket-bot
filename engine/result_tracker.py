"""
engine/result_tracker.py
========================
Tracker hasil bet dan statistik PnL bot sniper.

Menyimpan semua bet ke CSV dan menghitung:
  - Win rate
  - Total profit/loss
  - Streak (berapa kali menang/kalah berturut-turut)
"""

import csv
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

RESULTS_CSV = "logs/sniper_live_results.csv"
CSV_HEADERS = [
    "timestamp", "window_id", "direction", "bet_amount",
    "odds", "beat_price", "close_price", "result",
    "payout", "pnl", "running_pnl",
    # Extended context fields
    "remaining_secs", "odds_spread", "beat_distance",
    "signal_mode", "cl_edge", "cvd_2min",
    "liq_short_3s", "liq_long_3s", "hour_utc",
]


@dataclass
class BetRecord:
    """Satu record bet dengan context lengkap."""
    timestamp:   str
    window_id:   str
    direction:   str
    bet_amount:  float
    odds:        float
    beat_price:  float
    close_price: Optional[float] = None
    result:      Optional[str]   = None
    payout:      float = 0.0
    pnl:         float = 0.0
    running_pnl: float = 0.0
    # Extended context (untuk loss analysis)
    remaining_secs: float = 0.0
    odds_spread:    float = 0.0
    beat_distance:  float = 0.0
    signal_mode:    str   = ""
    cl_edge:        float = 0.0
    cl_fair_odds:   float = 0.0
    cl_vol:         float = 0.0
    cvd_2min:       float = 0.0
    liq_short_3s:   float = 0.0
    liq_long_3s:    float = 0.0
    liq_short_30s:  float = 0.0
    liq_long_30s:   float = 0.0
    hour_utc:       int   = 0
    coin:           str   = "BTC"


class ResultTracker:
    """
    Tracker hasil bet dengan persistensi CSV.

    Attributes:
        total_bets   : int   — Total bet sejak bot start
        wins         : int   — Total menang
        losses       : int   — Total kalah
        running_pnl  : float — PnL kumulatif
        current_bet  : BetRecord — Bet yang sedang aktif (belum resolved)
    """

    def __init__(self, csv_path: str = RESULTS_CSV):
        self.csv_path = csv_path
        self.total_bets:  int   = 0
        self.wins:        int   = 0
        self.losses:      int   = 0
        self.running_pnl: float = 0.0
        self.current_bet: Optional[BetRecord] = None
        self._records: list[BetRecord] = []

        os.makedirs("logs", exist_ok=True)
        self._load_existing()

    def _load_existing(self) -> None:
        """Load data dari CSV yang sudah ada."""
        if not os.path.exists(self.csv_path):
            self._init_csv()
            return

        try:
            with open(self.csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    result = row.get("result", "")
                    if result == "WIN":
                        self.wins += 1
                        self.total_bets += 1
                    elif result == "LOSS":
                        self.losses += 1
                        self.total_bets += 1
                    pnl_str = row.get("running_pnl", "0")
                    try:
                        self.running_pnl = float(pnl_str)
                    except:
                        pass
            logger.info(f"[ResultTracker] Loaded {self.total_bets} records dari {self.csv_path}")
        except Exception as e:
            logger.warning(f"[ResultTracker] Gagal load CSV: {e}")
            self._init_csv()

    def _init_csv(self) -> None:
        """Buat CSV baru dengan header."""
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

    def record_bet(
        self,
        window_id:     str,
        direction:     str,
        bet_amount:    float,
        odds:          float,
        beat_price:    float,
        # Extended context
        remaining_secs: float = 0.0,
        odds_spread:    float = 0.0,
        beat_distance:  float = 0.0,
        signal_mode:    str   = "",
        cl_edge:        float = 0.0,
        cl_fair_odds:   float = 0.0,
        cl_vol:         float = 0.0,
        cvd_2min:       float = 0.0,
        liq_short_3s:   float = 0.0,
        liq_long_3s:    float = 0.0,
        liq_short_30s:  float = 0.0,
        liq_long_30s:   float = 0.0,
        coin:           str   = "BTC",
    ) -> BetRecord:
        """Catat bet baru dengan context lengkap."""
        from datetime import timezone
        now_utc = datetime.now(timezone.utc)
        rec = BetRecord(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            window_id=window_id,
            direction=direction,
            bet_amount=bet_amount,
            odds=odds,
            beat_price=beat_price,
            result="PENDING",
            remaining_secs=remaining_secs,
            odds_spread=odds_spread,
            beat_distance=beat_distance,
            signal_mode=signal_mode,
            cl_edge=cl_edge,
            cl_fair_odds=cl_fair_odds,
            cl_vol=cl_vol,
            cvd_2min=cvd_2min,
            liq_short_3s=liq_short_3s,
            liq_long_3s=liq_long_3s,
            liq_short_30s=liq_short_30s,
            liq_long_30s=liq_long_30s,
            hour_utc=now_utc.hour,
            coin=coin,
        )
        self.current_bet = rec
        self._records.append(rec)
        logger.info(f"[ResultTracker] Bet recorded: {direction} ${bet_amount:.2f} @ {odds:.2f} | beat={beat_price:.2f}")
        return rec

    def resolve_bet(
        self,
        window_id:   str,
        close_price: float,
    ) -> Optional[BetRecord]:
        """
        Resolve bet berdasarkan harga close.
        Bandingkan close_price dengan beat_price untuk tentukan WIN/LOSS.
        """
        # Cari bet yang pending untuk window ini
        rec = None
        for r in reversed(self._records):
            if r.window_id == window_id and r.result == "PENDING":
                rec = r
                break

        if rec is None:
            return None

        rec.close_price = close_price

        # Tentukan hasil
        if rec.direction == "UP":
            won = close_price > rec.beat_price
        else:  # DOWN
            won = close_price < rec.beat_price

        if won:
            rec.result = "WIN"
            # Payout = bet_amount / odds (kalau odds adalah harga token, 0-1)
            # Di Polymarket: jika beli 5 USDC worth di odds 0.65 → payout = 5/0.65 jika menang
            # Tapi lebih simpel: payout = bet_amount * (1/odds)
            rec.payout  = rec.bet_amount * (1.0 / rec.odds) if rec.odds > 0 else 0
            rec.pnl     = rec.payout - rec.bet_amount
            self.wins += 1
        else:
            rec.result = "LOSS"
            rec.payout = 0.0
            rec.pnl    = -rec.bet_amount
            self.losses += 1

        self.total_bets  += 1
        self.running_pnl += rec.pnl
        rec.running_pnl   = self.running_pnl

        if rec == self.current_bet:
            self.current_bet = None

        self._append_csv(rec)
        logger.info(
            f"[ResultTracker] Resolved: {rec.direction} → {rec.result} "
            f"| close={close_price:.2f} beat={rec.beat_price:.2f} "
            f"| PnL=${rec.pnl:+.2f} | Total=${self.running_pnl:+.2f}"
        )
        return rec

    def _append_csv(self, rec: BetRecord) -> None:
        """Append satu record ke CSV."""
        try:
            with open(self.csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    rec.timestamp, rec.window_id, rec.direction,
                    f"{rec.bet_amount:.2f}", f"{rec.odds:.4f}",
                    f"{rec.beat_price:.2f}",
                    f"{rec.close_price:.2f}" if rec.close_price else "",
                    rec.result,
                    f"{rec.payout:.2f}", f"{rec.pnl:+.2f}", f"{rec.running_pnl:+.2f}",
                    f"{rec.remaining_secs:.0f}", f"{rec.odds_spread:.4f}",
                    f"{rec.beat_distance:.2f}", rec.signal_mode,
                    f"{rec.cl_edge:.4f}", f"{rec.cvd_2min:.0f}",
                    f"{rec.liq_short_3s:.0f}", f"{rec.liq_long_3s:.0f}",
                    str(rec.hour_utc),
                ])
        except Exception as e:
            logger.error(f"[ResultTracker] Gagal write CSV: {e}")

    @property
    def win_rate(self) -> float:
        """Win rate dalam persen (0-100)."""
        if self.total_bets == 0:
            return 0.0
        return (self.wins / self.total_bets) * 100

    @property
    def current_streak(self) -> tuple[str, int]:
        """
        Streak saat ini.
        Returns: ("W", 3) untuk 3 menang berturut-turut, atau ("L", 2) untuk 2 kalah.
        """
        streak_type = None
        count = 0
        for rec in reversed(self._records):
            if rec.result not in ("WIN", "LOSS"):
                continue
            if streak_type is None:
                streak_type = "W" if rec.result == "WIN" else "L"
                count = 1
            elif (rec.result == "WIN" and streak_type == "W") or \
                 (rec.result == "LOSS" and streak_type == "L"):
                count += 1
            else:
                break
        return (streak_type or "-", count)

    def summary(self) -> str:
        """Summary singkat untuk dashboard."""
        streak_type, streak_count = self.current_streak
        return (
            f"Bets:{self.total_bets} | "
            f"W:{self.wins} L:{self.losses} | "
            f"WR:{self.win_rate:.1f}% | "
            f"PnL:${self.running_pnl:+.2f} | "
            f"Streak:{streak_type}{streak_count}"
        )
