"""
engine/result_tracker.py
========================
Tracker hasil bet dan statistik PnL bot sniper.

FIXES:
  - BUG #3: resolve_source ditambahkan ke CSV_HEADERS
  - beat_source & beat_reliable ditambahkan ke BetRecord dan CSV
    untuk tracking apakah beat price akurat saat bet dilakukan
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
    "remaining_secs", "odds_spread", "beat_distance",
    "signal_mode", "cl_edge", "cvd_2min",
    "liq_short_3s", "liq_long_3s", "hour_utc",
    "resolve_source",
    "beat_source",     # NEW: sumber beat price (CHAINLINK/HYPERLIQUID)
    "beat_reliable",   # NEW: apakah beat reliable saat bet
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
    market_id:      str   = ""
    resolve_source: str   = ""
    beat_source:    str   = "UNKNOWN"   # NEW
    beat_reliable:  bool  = False       # NEW


class ResultTracker:
    """Tracker hasil bet dengan persistensi CSV."""

    def __init__(self, csv_path: str = RESULTS_CSV):
        self.csv_path    = csv_path
        self.total_bets  = 0
        self.wins        = 0
        self.losses      = 0
        self.running_pnl = 0.0
        self.current_bet: Optional[BetRecord] = None
        self._records:    list = []

        os.makedirs("logs", exist_ok=True)
        self._load_existing()

    def _load_existing(self) -> None:
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
                    try:
                        self.running_pnl = float(row.get("running_pnl", "0"))
                    except Exception:
                        pass
            logger.info(
                f"[ResultTracker] Loaded {self.total_bets} records dari {self.csv_path}"
            )
        except Exception as e:
            logger.warning(f"[ResultTracker] Gagal load CSV: {e}")
            self._init_csv()

    def _init_csv(self) -> None:
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

    def record_bet(
        self,
        window_id:      str,
        direction:      str,
        bet_amount:     float,
        odds:           float,
        beat_price:     float,
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
        market_id:      str   = "",
        beat_source:    str   = "UNKNOWN",   # NEW
        beat_reliable:  bool  = False,       # NEW
    ) -> BetRecord:
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
            market_id=market_id,
            beat_source=beat_source,
            beat_reliable=beat_reliable,
        )
        self.current_bet = rec
        self._records.append(rec)
        logger.info(
            f"[ResultTracker] Bet recorded: {direction} ${bet_amount:.2f} @ {odds:.2f} "
            f"| beat={beat_price:.2f} [{beat_source}{'✓' if beat_reliable else '⚠'}]"
        )
        return rec

    @staticmethod
    def query_polymarket_result(market_id: str) -> Optional[str]:
        """Query hasil resmi dari Polymarket API."""
        if not market_id:
            return None
        try:
            import requests
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"conditionId": market_id},
                timeout=5,
            )
            if resp.status_code != 200:
                return None
            data    = resp.json()
            markets = data if isinstance(data, list) else [data]
            if not markets:
                return None
            m = markets[0]

            if not m.get("closed", False):
                return None

            winner = m.get("winner", "")
            if winner:
                w = str(winner).strip().upper()
                if w in ("UP", "HIGHER", "YES"):
                    return "UP"
                if w in ("DOWN", "LOWER", "NO"):
                    return "DOWN"

            import json as _json
            prices_raw   = m.get("outcomePrices", "[]")
            outcomes_raw = m.get("outcomes", "[]")
            prices   = _json.loads(prices_raw)   if isinstance(prices_raw, str)   else prices_raw
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

            for i, price in enumerate(prices):
                if float(price) >= 0.99 and i < len(outcomes):
                    out = str(outcomes[i]).strip().upper()
                    if out in ("UP", "HIGHER", "YES"):
                        return "UP"
                    if out in ("DOWN", "LOWER", "NO"):
                        return "DOWN"

        except Exception as e:
            logger.debug(f"[ResultTracker] query_polymarket_result error: {e}")
        return None

    def resolve_bet(
        self,
        window_id:   str,
        close_price: float,
        market_id:   str = "",
    ) -> Optional[BetRecord]:
        """
        Resolve bet.
        Prioritas: 1. Polymarket API, 2. Chainlink close_price vs beat_price.

        PENTING: close_price HARUS dari Chainlink agar akurat.
        """
        rec = None
        for r in reversed(self._records):
            if r.window_id == window_id and r.result == "PENDING":
                rec = r
                break
        if rec is None:
            return None

        rec.close_price = close_price

        # Cara 1: Query Polymarket API
        official_result = None
        mid = market_id or getattr(rec, "market_id", "")
        if mid:
            official_result = self.query_polymarket_result(mid)

        if official_result:
            won    = (official_result == rec.direction)
            source = "POLYMARKET_API"
        else:
            # Cara 2: Hitung dari close_price vs beat_price
            if rec.direction == "UP":
                won = close_price > rec.beat_price
            else:
                won = close_price < rec.beat_price
            source = "CHAINLINK_CALC"

            # Warning jika beat tidak reliable
            if not rec.beat_reliable:
                logger.warning(
                    f"[ResultTracker] ⚠️ Resolve dengan beat TIDAK RELIABLE "
                    f"(source={rec.beat_source}) — hasil mungkin salah! "
                    f"beat={rec.beat_price:.2f} close={close_price:.2f} "
                    f"direction={rec.direction} → {'WIN' if won else 'LOSS'}"
                )

            logger.warning(
                f"[ResultTracker] Polymarket API belum resolve → fallback hitung sendiri "
                f"({rec.direction}: close={close_price:.2f} vs beat={rec.beat_price:.2f})"
            )

        if won:
            rec.result = "WIN"
            rec.payout = rec.bet_amount / rec.odds if rec.odds > 0 else 0.0
            rec.pnl    = rec.payout - rec.bet_amount
            self.wins += 1
        else:
            rec.result = "LOSS"
            rec.payout = 0.0
            rec.pnl    = -rec.bet_amount
            self.losses += 1

        self.total_bets  += 1
        self.running_pnl += rec.pnl
        rec.running_pnl   = self.running_pnl
        rec.resolve_source = source

        if rec == self.current_bet:
            self.current_bet = None

        self._append_csv(rec)
        logger.info(
            f"[ResultTracker] Resolved [{source}]: {rec.direction} → {rec.result} "
            f"| close={close_price:.2f} beat={rec.beat_price:.2f} [{rec.beat_source}] "
            f"| PnL=${rec.pnl:+.2f} | Total=${self.running_pnl:+.2f}"
        )
        return rec

    def _append_csv(self, rec: BetRecord) -> None:
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
                    rec.resolve_source,
                    rec.beat_source,
                    str(rec.beat_reliable),
                ])
        except Exception as e:
            logger.error(f"[ResultTracker] Gagal write CSV: {e}")

    @property
    def win_rate(self) -> float:
        if self.total_bets == 0:
            return 0.0
        return (self.wins / self.total_bets) * 100

    @property
    def current_streak(self) -> tuple:
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
        streak_type, streak_count = self.current_streak
        return (
            f"Bets:{self.total_bets} | "
            f"W:{self.wins} L:{self.losses} | "
            f"WR:{self.win_rate:.1f}% | "
            f"PnL:${self.running_pnl:+.2f} | "
            f"Streak:{streak_type}{streak_count}"
        )