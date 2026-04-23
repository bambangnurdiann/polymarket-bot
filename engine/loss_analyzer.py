"""
engine/loss_analyzer.py
=======================
Loss Analyzer — Analisa pola kekalahan untuk improve strategi.

Setiap kali bot kalah, analyzer ini:
1. Catat semua context saat bet: CVD, liq, odds spread, sisa waktu, dll
2. Temukan pola: apakah kalah lebih sering di kondisi tertentu?
3. Generate rekomendasi filter otomatis
4. Simpan ke JSON untuk analisa jangka panjang

Pattern yang dianalisa:
  - WR berdasarkan remaining time saat bet
  - WR berdasarkan odds spread
  - WR berdasarkan CVD direction
  - WR berdasarkan beat distance
  - WR berdasarkan jam (UTC)
  - Streak analysis
"""

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

LOSS_LOG_PATH = "logs/loss_analysis.json"
PATTERN_LOG_PATH = "logs/bet_patterns.json"


@dataclass
class BetContext:
    """Context lengkap saat bet dieksekusi."""
    timestamp:      str
    window_id:      str
    direction:      str
    result:         str        # WIN atau LOSS
    bet_amount:     float
    odds:           float
    beat_price:     float
    close_price:    float
    pnl:            float

    # Market context
    remaining_secs: float = 0.0
    odds_spread:    float = 0.0   # abs(odds_up - odds_down)
    beat_distance:  float = 0.0   # abs(price - beat)
    beat_direction: str   = ""    # "UP" atau "DOWN" (arah harga vs beat)

    # Signal context
    signal_mode:    str   = ""    # "CHAINLINK" atau "LATE"
    cl_edge:        float = 0.0   # Chainlink edge saat bet
    cl_fair_odds:   float = 0.0
    cl_vol:         float = 0.0

    # Hyperliquid context
    cvd_2min:       float = 0.0
    liq_short_3s:   float = 0.0
    liq_long_3s:    float = 0.0
    liq_short_30s:  float = 0.0
    liq_long_30s:   float = 0.0

    # Time context
    hour_utc:       int   = 0
    minute_utc:     int   = 0


class LossAnalyzer:
    """
    Analisa pola kekalahan dan generate rekomendasi filter.
    """

    def __init__(self):
        self._contexts: List[BetContext] = []
        self._loaded   = False
        os.makedirs("logs", exist_ok=True)
        self._load()

    def _load(self) -> None:
        """Load existing loss data."""
        if os.path.exists(LOSS_LOG_PATH):
            try:
                with open(LOSS_LOG_PATH) as f:
                    data = json.load(f)
                for d in data:
                    try:
                        self._contexts.append(BetContext(**d))
                    except Exception:
                        pass
                logger.info(f"[LossAnalyzer] Loaded {len(self._contexts)} bet contexts")
            except Exception as e:
                logger.debug(f"[LossAnalyzer] Load error: {e}")
        self._loaded = True

    def _save(self) -> None:
        """Simpan semua context ke JSON."""
        try:
            data = [asdict(c) for c in self._contexts[-500:]]  # max 500 records
            with open(LOSS_LOG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"[LossAnalyzer] Save error: {e}")

    def record(self, ctx: BetContext) -> None:
        """Catat satu bet context."""
        self._contexts.append(ctx)
        self._save()

        # Log loss dengan detail
        if ctx.result == "LOSS":
            logger.info(
                f"[LossAnalyzer] LOSS recorded | "
                f"{ctx.direction} rem={ctx.remaining_secs:.0f}s "
                f"spread={ctx.odds_spread:.3f} dist=${ctx.beat_distance:.1f} "
                f"cvd=${ctx.cvd_2min/1000:+.0f}k edge={ctx.cl_edge:.3f} "
                f"hour={ctx.hour_utc}UTC"
            )

    # ── Pattern Analysis ──────────────────────────────────────

    def analyze(self) -> dict:
        """
        Analisa semua bet dan temukan pola.
        Returns dict dengan insights dan rekomendasi.
        """
        if len(self._contexts) < 10:
            return {"status": "insufficient_data", "count": len(self._contexts)}

        insights = {}

        # 1. WR by remaining time bucket
        insights["wr_by_remaining"] = self._wr_by_bucket(
            "remaining_secs",
            buckets=[(0,30), (30,60), (60,120), (120,180), (180,240), (240,300)]
        )

        # 2. WR by odds spread bucket
        insights["wr_by_odds_spread"] = self._wr_by_bucket(
            "odds_spread",
            buckets=[(0,0.05), (0.05,0.10), (0.10,0.15), (0.15,0.20), (0.20,1.0)]
        )

        # 3. WR by beat distance
        insights["wr_by_beat_distance"] = self._wr_by_bucket(
            "beat_distance",
            buckets=[(0,20), (20,40), (40,60), (60,100), (100,999)]
        )

        # 4. WR by hour UTC
        insights["wr_by_hour"] = self._wr_by_field("hour_utc")

        # 5. WR by CVD direction alignment
        insights["wr_cvd_aligned"]     = self._wr_cvd_alignment()

        # 6. WR by signal mode
        insights["wr_by_mode"]         = self._wr_by_field("signal_mode")

        # 7. WR by direction
        insights["wr_by_direction"]    = self._wr_by_field("direction")

        # 8. Streak analysis
        insights["streak_analysis"]    = self._streak_analysis()

        # 9. Loss conditions summary
        insights["loss_conditions"]    = self._loss_conditions_summary()

        # Generate recommendations
        insights["recommendations"]    = self._generate_recommendations(insights)
        insights["total_bets"]         = len(self._contexts)
        insights["overall_wr"]         = self._overall_wr()
        insights["generated_at"]       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Save patterns
        try:
            with open(PATTERN_LOG_PATH, "w") as f:
                json.dump(insights, f, indent=2)
        except Exception:
            pass

        return insights

    def _overall_wr(self) -> float:
        if not self._contexts:
            return 0.0
        wins = sum(1 for c in self._contexts if c.result == "WIN")
        return wins / len(self._contexts) * 100

    def _wr_by_bucket(self, field: str, buckets: list) -> dict:
        """Hitung WR untuk tiap bucket nilai."""
        result = {}
        for lo, hi in buckets:
            subset = [c for c in self._contexts
                      if lo <= getattr(c, field, 0) < hi]
            if not subset:
                continue
            wins = sum(1 for c in subset if c.result == "WIN")
            wr   = wins / len(subset) * 100
            result[f"{lo}-{hi}"] = {
                "wr": round(wr, 1),
                "count": len(subset),
                "wins": wins,
            }
        return result

    def _wr_by_field(self, field: str) -> dict:
        """Hitung WR per nilai unik field."""
        groups = defaultdict(list)
        for c in self._contexts:
            key = str(getattr(c, field, "unknown"))
            groups[key].append(c)
        result = {}
        for key, subset in groups.items():
            wins = sum(1 for c in subset if c.result == "WIN")
            wr   = wins / len(subset) * 100
            result[key] = {"wr": round(wr, 1), "count": len(subset)}
        return result

    def _wr_cvd_alignment(self) -> dict:
        """WR berdasarkan apakah CVD searah dengan bet."""
        aligned = [c for c in self._contexts
                   if (c.direction == "UP" and c.cvd_2min > 0) or
                      (c.direction == "DOWN" and c.cvd_2min < 0)]
        opposite = [c for c in self._contexts if c not in aligned]

        def wr(subset):
            if not subset:
                return {"wr": 0, "count": 0}
            wins = sum(1 for c in subset if c.result == "WIN")
            return {"wr": round(wins/len(subset)*100, 1), "count": len(subset)}

        return {
            "cvd_aligned":  wr(aligned),
            "cvd_opposite": wr(opposite),
        }

    def _streak_analysis(self) -> dict:
        """Analisa streak panjang dan kondisinya."""
        streaks = []
        current_result = None
        current_count  = 0
        current_start  = 0

        for i, c in enumerate(self._contexts):
            if c.result not in ("WIN", "LOSS"):
                continue
            if c.result == current_result:
                current_count += 1
            else:
                if current_count >= 3:
                    streaks.append({
                        "result":    current_result,
                        "count":     current_count,
                        "start_idx": current_start,
                    })
                current_result = c.result
                current_count  = 1
                current_start  = i

        max_loss_streak = max(
            (s["count"] for s in streaks if s["result"] == "LOSS"), default=0
        )
        max_win_streak = max(
            (s["count"] for s in streaks if s["result"] == "WIN"), default=0
        )

        return {
            "max_loss_streak": max_loss_streak,
            "max_win_streak":  max_win_streak,
            "all_streaks":     streaks,
        }

    def _loss_conditions_summary(self) -> dict:
        """Rata-rata kondisi saat LOSS vs WIN."""
        losses = [c for c in self._contexts if c.result == "LOSS"]
        wins   = [c for c in self._contexts if c.result == "WIN"]

        def avg(lst, field):
            vals = [getattr(c, field, 0) for c in lst]
            return round(sum(vals) / len(vals), 4) if vals else 0

        fields = ["remaining_secs", "odds_spread", "beat_distance",
                  "cl_edge", "cl_vol", "odds"]

        return {
            "on_loss": {f: avg(losses, f) for f in fields},
            "on_win":  {f: avg(wins,   f) for f in fields},
            "loss_count": len(losses),
            "win_count":  len(wins),
        }

    def _generate_recommendations(self, insights: dict) -> list:
        """Generate rekomendasi filter berdasarkan pola."""
        recs = []

        # Cek WR by remaining time
        rem_wr = insights.get("wr_by_remaining", {})
        bad_buckets = [k for k, v in rem_wr.items()
                       if v.get("wr", 100) < 48 and v.get("count", 0) >= 5]
        if bad_buckets:
            recs.append({
                "type":   "timing",
                "action": f"Avoid betting at remaining_secs in {bad_buckets}",
                "detail": f"WR < 48% at these time buckets",
            })

        # Cek WR by odds spread
        spread_wr = insights.get("wr_by_odds_spread", {})
        bad_spread = [k for k, v in spread_wr.items()
                      if v.get("wr", 100) < 48 and v.get("count", 0) >= 5]
        if bad_spread:
            min_spread = min(float(k.split("-")[0]) for k in bad_spread)
            recs.append({
                "type":   "odds_spread",
                "action": f"Increase min_odds_spread to avoid {bad_spread}",
                "detail": f"Low WR when spread < {min_spread:.2f}",
                "suggested_value": min_spread + 0.02,
            })

        # Cek CVD alignment
        cvd_data = insights.get("wr_cvd_alignment", {})
        aligned_wr  = cvd_data.get("cvd_aligned", {}).get("wr", 50)
        opposite_wr = cvd_data.get("cvd_opposite", {}).get("wr", 50)
        if aligned_wr - opposite_wr > 10:
            recs.append({
                "type":   "cvd_filter",
                "action": "Add CVD alignment as required filter for F0",
                "detail": f"CVD aligned WR={aligned_wr:.1f}% vs opposite WR={opposite_wr:.1f}%",
            })

        # Cek bad hours
        hour_wr = insights.get("wr_by_hour", {})
        bad_hours = [k for k, v in hour_wr.items()
                     if v.get("wr", 100) < 45 and v.get("count", 0) >= 5]
        if bad_hours:
            recs.append({
                "type":   "session_block",
                "action": f"Add session block for UTC hours: {bad_hours}",
                "detail": f"Win rate consistently below 45% in these hours",
            })

        # Cek beat distance
        dist_wr = insights.get("wr_by_beat_distance", {})
        bad_dist = [k for k, v in dist_wr.items()
                    if v.get("wr", 100) < 48 and v.get("count", 0) >= 5]
        if bad_dist:
            recs.append({
                "type":   "beat_distance",
                "action": f"Increase LATE_BEAT_DISTANCE to filter {bad_dist}",
                "detail": f"Low WR when beat distance in {bad_dist}",
            })

        if not recs:
            recs.append({
                "type":   "ok",
                "action": "No significant patterns found yet",
                "detail": f"Need more data. Current WR={insights.get('overall_wr', 0):.1f}%",
            })

        return recs

    def print_report(self) -> None:
        """Print analisa ke terminal."""
        insights = self.analyze()

        if insights.get("status") == "insufficient_data":
            print(f"\n[LossAnalyzer] Butuh lebih banyak data ({insights['count']}/10 minimum)\n")
            return

        print("\n" + "="*60)
        print("  LOSS ANALYSIS REPORT")
        print(f"  Total bets: {insights['total_bets']} | Overall WR: {insights['overall_wr']:.1f}%")
        print("="*60)

        print("\n📊 WR by Remaining Time:")
        for bucket, data in sorted(insights.get("wr_by_remaining", {}).items()):
            bar = "█" * int(data["wr"] / 5)
            print(f"  {bucket:12s}: {data['wr']:5.1f}% {bar} (n={data['count']})")

        print("\n📊 WR by Odds Spread:")
        for bucket, data in sorted(insights.get("wr_by_odds_spread", {}).items()):
            print(f"  {bucket:12s}: {data['wr']:5.1f}% (n={data['count']})")

        print("\n📊 WR by CVD Alignment:")
        cvd = insights.get("wr_cvd_alignment", {})
        print(f"  CVD aligned : {cvd.get('cvd_aligned', {}).get('wr', 0):.1f}% (n={cvd.get('cvd_aligned', {}).get('count', 0)})")
        print(f"  CVD opposite: {cvd.get('cvd_opposite', {}).get('wr', 0):.1f}% (n={cvd.get('cvd_opposite', {}).get('count', 0)})")

        print("\n📊 WR by Hour (UTC):")
        for hour, data in sorted(insights.get("wr_by_hour", {}).items(), key=lambda x: int(x[0])):
            print(f"  {hour:>4s}:00 UTC: {data['wr']:5.1f}% (n={data['count']})")

        streak = insights.get("streak_analysis", {})
        print(f"\n📊 Max Loss Streak: {streak.get('max_loss_streak', 0)}")
        print(f"   Max Win Streak : {streak.get('max_win_streak', 0)}")

        conds = insights.get("loss_conditions", {})
        print(f"\n📊 Avg Conditions on LOSS vs WIN:")
        for field in ["remaining_secs", "odds_spread", "beat_distance", "cl_edge"]:
            loss_val = conds.get("on_loss", {}).get(field, 0)
            win_val  = conds.get("on_win",  {}).get(field, 0)
            print(f"  {field:20s}: LOSS={loss_val:.4f} WIN={win_val:.4f}")

        print("\n💡 Recommendations:")
        for rec in insights.get("recommendations", []):
            print(f"  [{rec['type'].upper()}] {rec['action']}")
            print(f"    → {rec['detail']}")

        print("="*60 + "\n")


# ── Standalone runner ─────────────────────────────────────────
if __name__ == "__main__":
    analyzer = LossAnalyzer()
    analyzer.print_report()
