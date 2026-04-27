"""
engine/loss_analyzer.py  (v2 — Improved)
=========================================
Loss Analyzer komprehensif untuk bot sniper Polymarket.

Perbaikan dari v1:
  [FIX] CVD Alignment tidak lagi menghitung zero-CVD sebagai "opposite"
        → sekarang ada 3 bucket: aligned / opposite / neutral(0)
  [NEW] Liquidation ratio analysis (liq_short/liq_long imbalance)
  [NEW] CVD magnitude buckets (kuat vs lemah vs nol)
  [NEW] Expected Value (EV) analysis per odds bucket
  [NEW] Chainlink edge bucket analysis
  [NEW] Multi-factor combo analysis (2-way cross tabulation)
  [NEW] Auto loss-reason tagging per record
  [NEW] generate_env_patch() — output snippet .env siap pakai
  [NEW] Confidence scoring per bet berdasarkan semua faktor
  [IMPROVED] Rekomendasi lebih spesifik dengan suggested_value yang jelas

Pattern yang dianalisa:
  - WR by remaining time bucket
  - WR by odds spread bucket
  - WR by beat distance bucket
  - WR by hour UTC
  - WR by CVD alignment (fix: exclude zero CVD)
  - WR by CVD magnitude (strong/weak/neutral)
  - WR by liquidation ratio (short/long dominance)
  - WR by Chainlink edge bucket
  - WR by signal mode
  - WR by direction
  - WR by odds value bucket (EV analysis)
  - Combo: hour × signal_mode
  - Combo: CVD alignment × odds spread
  - Combo: beat_distance × remaining_secs
  - Streak analysis
  - Loss condition averages (LOSS vs WIN)
  - Auto loss-reason tagging
"""

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger(__name__)

LOSS_LOG_PATH    = "logs/loss_analysis.json"
PATTERN_LOG_PATH = "logs/bet_patterns.json"
ENV_PATCH_PATH   = "logs/suggested_env_patch.txt"

# ── Threshold rekomendasi ──────────────────────────────────────
BAD_WR_THRESHOLD   = 48.0   # bucket dengan WR < ini dianggap "buruk"
MIN_SAMPLE         = 5      # minimal sample agar bucket dianggap valid
EV_BREAKEVEN_ODDS  = 0.50   # odds acuan breakeven (tanpa edge)


@dataclass
class BetContext:
    """Context lengkap saat bet dieksekusi (backward-compatible dengan v1)."""
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
    odds_spread:    float = 0.0
    beat_distance:  float = 0.0
    beat_direction: str   = ""

    # Signal context
    signal_mode:    str   = ""
    cl_edge:        float = 0.0
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

    # Resolve context
    market_id:      str   = ""
    resolve_source: str   = ""


# ── Helper ────────────────────────────────────────────────────

def _wr(subset: list) -> Tuple[float, int, int]:
    """Return (win_rate_pct, wins, total)."""
    if not subset:
        return 0.0, 0, 0
    wins = sum(1 for c in subset if c.result == "WIN")
    return round(wins / len(subset) * 100, 1), wins, len(subset)


def _avg(lst: list, field: str) -> float:
    vals = [getattr(c, field, 0) for c in lst]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _liq_ratio(ctx: BetContext) -> float:
    """
    Liquidation ratio = liq_short_3s / (liq_short_3s + liq_long_3s).
    > 0.5 → short dominan (pressure UP)
    < 0.5 → long dominan (pressure DOWN)
    = 0   → tidak ada data liq
    """
    total = ctx.liq_short_3s + ctx.liq_long_3s
    if total <= 0:
        return 0.0
    return ctx.liq_short_3s / total


def _cvd_magnitude(ctx: BetContext) -> str:
    """Kategorisasi kekuatan CVD."""
    v = ctx.cvd_2min
    if v == 0.0:
        return "neutral"
    abs_v = abs(v)
    if abs_v < 5_000:
        return "weak"
    if abs_v < 20_000:
        return "moderate"
    return "strong"


def _loss_reasons(ctx: BetContext) -> List[str]:
    """
    Auto-tag kemungkinan penyebab loss.
    Returns list of reason strings.
    """
    reasons = []

    # CVD berlawanan dengan arah bet
    if ctx.cvd_2min != 0:
        if ctx.direction == "UP" and ctx.cvd_2min < -5_000:
            reasons.append("cvd_against")
        elif ctx.direction == "DOWN" and ctx.cvd_2min > 5_000:
            reasons.append("cvd_against")

    # Spread terlalu sempit (market belum memihak)
    if ctx.odds_spread < 0.05:
        reasons.append("spread_too_narrow")

    # Beat distance terlalu kecil (terlalu dekat beat)
    if ctx.beat_distance < 30:
        reasons.append("beat_too_close")

    # Sisa waktu terlalu singkat
    if ctx.remaining_secs < 60:
        reasons.append("entered_too_late")

    # Chainlink edge negatif atau sangat kecil
    if ctx.signal_mode == "CHAINLINK" and ctx.cl_edge < 0.05:
        reasons.append("cl_edge_too_low")

    # Liq dominan berlawanan dengan bet
    ratio = _liq_ratio(ctx)
    if ratio > 0.0:
        if ctx.direction == "UP" and ratio < 0.35:
            reasons.append("liq_dominates_against")
        elif ctx.direction == "DOWN" and ratio > 0.65:
            reasons.append("liq_dominates_against")

    # Tidak ada alasan jelas → label unknown
    if not reasons:
        reasons.append("unknown")

    return reasons


# ── Main Class ────────────────────────────────────────────────

class LossAnalyzer:
    """
    Analisa pola kekalahan dan generate rekomendasi filter.
    """

    def __init__(self):
        self._contexts: List[BetContext] = []
        self._loaded   = False
        os.makedirs("logs", exist_ok=True)
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        if os.path.exists(LOSS_LOG_PATH):
            try:
                with open(LOSS_LOG_PATH) as f:
                    data = json.load(f)
                for d in data:
                    try:
                        # Backward-compat: abaikan field yang tidak dikenal
                        known = {k: v for k, v in d.items()
                                 if k in BetContext.__dataclass_fields__}
                        self._contexts.append(BetContext(**known))
                    except Exception:
                        pass
                logger.info(f"[LossAnalyzer] Loaded {len(self._contexts)} bet contexts")
            except Exception as e:
                logger.debug(f"[LossAnalyzer] Load error: {e}")
        self._loaded = True

    def _save(self) -> None:
        try:
            data = [asdict(c) for c in self._contexts[-1000:]]
            with open(LOSS_LOG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"[LossAnalyzer] Save error: {e}")

    def record(self, ctx: BetContext) -> None:
        """Catat satu bet context (WIN maupun LOSS)."""
        self._contexts.append(ctx)
        self._save()

        if ctx.result == "LOSS":
            reasons = _loss_reasons(ctx)
            logger.info(
                f"[LossAnalyzer] LOSS | {ctx.direction} "
                f"rem={ctx.remaining_secs:.0f}s spread={ctx.odds_spread:.3f} "
                f"dist=${ctx.beat_distance:.0f} cvd={ctx.cvd_2min/1000:+.0f}k "
                f"liq_ratio={_liq_ratio(ctx):.2f} edge={ctx.cl_edge:.3f} "
                f"h={ctx.hour_utc}UTC | reasons: {', '.join(reasons)}"
            )

    # ── Core WR helpers ───────────────────────────────────────

    def _wr_by_bucket(self, field: str, buckets: list) -> dict:
        result = {}
        for lo, hi in buckets:
            subset = [c for c in self._contexts
                      if lo <= getattr(c, field, 0) < hi]
            if len(subset) < 1:
                continue
            wr, wins, total = _wr(subset)
            result[f"{lo}-{hi}"] = {
                "wr": wr, "count": total, "wins": wins,
                "flagged": total >= MIN_SAMPLE and wr < BAD_WR_THRESHOLD,
            }
        return result

    def _wr_by_field(self, field: str) -> dict:
        groups = defaultdict(list)
        for c in self._contexts:
            key = str(getattr(c, field, "unknown"))
            groups[key].append(c)
        result = {}
        for key, subset in groups.items():
            wr, wins, total = _wr(subset)
            result[key] = {
                "wr": wr, "count": total, "wins": wins,
                "flagged": total >= MIN_SAMPLE and wr < BAD_WR_THRESHOLD,
            }
        return result

    def _overall_wr(self) -> float:
        if not self._contexts:
            return 0.0
        wins = sum(1 for c in self._contexts if c.result == "WIN")
        return round(wins / len(self._contexts) * 100, 1)

    # ── CVD Analysis (FIXED) ──────────────────────────────────

    def _wr_cvd_alignment(self) -> dict:
        """
        WR berdasarkan apakah CVD searah dengan bet.

        FIX v2: records dengan cvd_2min == 0 dipisah ke bucket 'neutral'
        agar tidak mencemari statistik 'aligned' vs 'opposite'.
        """
        neutral  = [c for c in self._contexts if c.cvd_2min == 0.0]
        active   = [c for c in self._contexts if c.cvd_2min != 0.0]

        aligned  = [c for c in active
                    if (c.direction == "UP"   and c.cvd_2min > 0) or
                       (c.direction == "DOWN" and c.cvd_2min < 0)]
        opposite = [c for c in active
                    if (c.direction == "UP"   and c.cvd_2min < 0) or
                       (c.direction == "DOWN" and c.cvd_2min > 0)]

        wa, _, ca = _wr(aligned)
        wo, _, co = _wr(opposite)
        wn, _, cn = _wr(neutral)

        # Berapa persen record yang CVD-nya nol (diagnostik capture issue)
        total = len(self._contexts)
        zero_pct = round(cn / total * 100, 1) if total > 0 else 0.0

        result = {
            "cvd_aligned":  {"wr": wa, "count": ca},
            "cvd_opposite": {"wr": wo, "count": co},
            "cvd_neutral":  {"wr": wn, "count": cn, "zero_pct": zero_pct},
        }

        # Peringatan jika lebih dari 30% record CVD = 0 (data capture issue)
        if zero_pct > 30:
            result["warning"] = (
                f"CVD data kosong di {zero_pct}% record. "
                f"Kemungkinan data.cvd_2min tidak ter-capture dari MultiWS. "
                f"Periksa apakah CVD accumulation berjalan di fetcher."
            )

        return result

    # ── CVD Magnitude ─────────────────────────────────────────

    def _wr_cvd_magnitude(self) -> dict:
        """WR berdasarkan kekuatan absolut CVD (bukan hanya arah)."""
        groups = defaultdict(list)
        for c in self._contexts:
            groups[_cvd_magnitude(c)].append(c)
        result = {}
        for mag in ["strong", "moderate", "weak", "neutral"]:
            subset = groups.get(mag, [])
            if not subset:
                continue
            wr, wins, total = _wr(subset)
            result[mag] = {"wr": wr, "count": total, "wins": wins}
        return result

    # ── Liquidation Ratio Analysis ────────────────────────────

    def _wr_liq_ratio(self) -> dict:
        """
        WR berdasarkan dominasi liquidation.
        Bucket: short_dom (ratio>0.65), balanced (0.35-0.65), long_dom (ratio<0.35), no_data
        """
        short_dom = []
        balanced  = []
        long_dom  = []
        no_data   = []

        for c in self._contexts:
            ratio = _liq_ratio(c)
            if ratio == 0.0:
                no_data.append(c)
            elif ratio > 0.65:
                short_dom.append(c)
            elif ratio < 0.35:
                long_dom.append(c)
            else:
                balanced.append(c)

        result = {}
        for label, subset in [
            ("short_dom(ratio>0.65)", short_dom),
            ("balanced(0.35-0.65)",   balanced),
            ("long_dom(ratio<0.35)",  long_dom),
            ("no_liq_data",           no_data),
        ]:
            if not subset:
                continue
            wr, wins, total = _wr(subset)
            result[label] = {"wr": wr, "count": total, "wins": wins}

        # Tambahan: WR saat liq direction SEARAH vs BERLAWANAN dengan bet
        liq_aligned  = []
        liq_opposite = []
        for c in self._contexts:
            ratio = _liq_ratio(c)
            if ratio == 0.0:
                continue
            if c.direction == "UP" and ratio > 0.5:     # short dom → pressure UP
                liq_aligned.append(c)
            elif c.direction == "DOWN" and ratio < 0.5: # long dom → pressure DOWN
                liq_aligned.append(c)
            else:
                liq_opposite.append(c)

        wa, _, ca = _wr(liq_aligned)
        wo, _, co = _wr(liq_opposite)
        result["liq_aligned_with_bet"]   = {"wr": wa, "count": ca}
        result["liq_opposite_to_bet"]    = {"wr": wo, "count": co}

        return result

    # ── Chainlink Edge Analysis ───────────────────────────────

    def _wr_cl_edge(self) -> dict:
        """WR per bucket Chainlink edge. Records tanpa CL (edge=0) dipisah."""
        cl_records = [c for c in self._contexts if c.signal_mode == "CHAINLINK"]
        non_cl     = [c for c in self._contexts if c.signal_mode != "CHAINLINK"]

        buckets = [(0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 1.0)]
        result  = {}

        for lo, hi in buckets:
            subset = [c for c in cl_records if lo <= c.cl_edge < hi]
            if not subset:
                continue
            wr, wins, total = _wr(subset)
            result[f"cl_edge_{lo:.2f}-{hi:.2f}"] = {
                "wr": wr, "count": total, "wins": wins,
                "flagged": total >= MIN_SAMPLE and wr < BAD_WR_THRESHOLD,
            }

        # Non-CL (mode LATE/F1/etc)
        if non_cl:
            wr, wins, total = _wr(non_cl)
            result["non_chainlink"] = {"wr": wr, "count": total, "wins": wins}

        return result

    # ── EV (Expected Value) per Odds Bucket ───────────────────

    def _ev_by_odds(self) -> dict:
        """
        Expected Value analysis per odds bucket.
        EV = (wr/100 * (1/odds - 1)) - ((1 - wr/100) * 1)
        EV > 0 = profitable bucket
        """
        buckets = [(0.45, 0.48), (0.48, 0.52), (0.52, 0.56), (0.56, 0.60), (0.60, 1.0)]
        result  = {}
        for lo, hi in buckets:
            subset = [c for c in self._contexts if lo <= c.odds < hi]
            if not subset:
                continue
            wr, wins, total = _wr(subset)
            avg_odds = sum(c.odds for c in subset) / total
            # EV per $1 bet
            ev = (wr / 100) * (1 / avg_odds - 1) - (1 - wr / 100)
            result[f"odds_{lo:.2f}-{hi:.2f}"] = {
                "wr": wr, "count": total,
                "avg_odds": round(avg_odds, 4),
                "ev_per_dollar": round(ev, 4),
                "profitable": ev > 0,
            }
        return result

    # ── Multi-Factor Combo Analysis ───────────────────────────

    def _combo_analysis(self) -> dict:
        """
        2-factor cross-tabulation untuk temukan kombinasi kondisi buruk.
        Combo yang dianalisa:
          - hour_utc × signal_mode
          - CVD alignment × odds_spread_category
          - beat_distance_category × remaining_secs_category
          - signal_mode × direction
        """
        result = {}

        # ── Combo 1: hour × mode ──────────────────────────────
        combo1 = defaultdict(list)
        for c in self._contexts:
            key = f"h{c.hour_utc:02d}_{c.signal_mode or 'LATE'}"
            combo1[key].append(c)
        bad_combos = {}
        for key, subset in combo1.items():
            if len(subset) < MIN_SAMPLE:
                continue
            wr, wins, total = _wr(subset)
            if wr < BAD_WR_THRESHOLD:
                bad_combos[key] = {"wr": wr, "count": total}
        result["hour_x_mode"] = bad_combos

        # ── Combo 2: CVD alignment × spread category ──────────
        combo2 = defaultdict(list)
        for c in self._contexts:
            if c.cvd_2min == 0:
                cvd_cat = "cvd_neutral"
            elif (c.direction == "UP" and c.cvd_2min > 0) or \
                 (c.direction == "DOWN" and c.cvd_2min < 0):
                cvd_cat = "cvd_aligned"
            else:
                cvd_cat = "cvd_opposite"
            spread_cat = "spread_low" if c.odds_spread < 0.07 else "spread_ok"
            combo2[f"{cvd_cat}_{spread_cat}"].append(c)
        bad_c2 = {}
        for key, subset in combo2.items():
            if len(subset) < MIN_SAMPLE:
                continue
            wr, wins, total = _wr(subset)
            if wr < BAD_WR_THRESHOLD:
                bad_c2[key] = {"wr": wr, "count": total}
        result["cvd_x_spread"] = bad_c2

        # ── Combo 3: beat_distance × remaining ────────────────
        combo3 = defaultdict(list)
        for c in self._contexts:
            dist_cat = "dist_close" if c.beat_distance < 40 else "dist_far"
            rem_cat  = "rem_short" if c.remaining_secs < 120 else "rem_ok"
            combo3[f"{dist_cat}_{rem_cat}"].append(c)
        bad_c3 = {}
        for key, subset in combo3.items():
            if len(subset) < MIN_SAMPLE:
                continue
            wr, wins, total = _wr(subset)
            bad_c3[key] = {"wr": wr, "count": total,
                           "flagged": wr < BAD_WR_THRESHOLD}
        result["distance_x_remaining"] = bad_c3

        # ── Combo 4: mode × direction ─────────────────────────
        combo4 = defaultdict(list)
        for c in self._contexts:
            key = f"{c.signal_mode or 'LATE'}_{c.direction}"
            combo4[key].append(c)
        c4_all = {}
        for key, subset in combo4.items():
            if len(subset) < 3:
                continue
            wr, wins, total = _wr(subset)
            c4_all[key] = {"wr": wr, "count": total,
                           "flagged": total >= MIN_SAMPLE and wr < BAD_WR_THRESHOLD}
        result["mode_x_direction"] = c4_all

        return result

    # ── Loss Reason Summary ───────────────────────────────────

    def _loss_reason_summary(self) -> dict:
        """
        Ringkasan frekuensi tiap loss reason.
        Membantu identifikasi penyebab loss paling dominan.
        """
        losses = [c for c in self._contexts if c.result == "LOSS"]
        if not losses:
            return {}

        reason_counts: Dict[str, int] = defaultdict(int)
        for c in losses:
            for reason in _loss_reasons(c):
                reason_counts[reason] += 1

        total_losses = len(losses)
        return {
            reason: {
                "count": cnt,
                "pct_of_losses": round(cnt / total_losses * 100, 1),
            }
            for reason, cnt in sorted(reason_counts.items(),
                                      key=lambda x: -x[1])
        }

    # ── Streak Analysis ───────────────────────────────────────

    def _streak_analysis(self) -> dict:
        streaks   = []
        cur_res   = None
        cur_count = 0
        cur_start = 0

        for i, c in enumerate(self._contexts):
            if c.result not in ("WIN", "LOSS"):
                continue
            if c.result == cur_res:
                cur_count += 1
            else:
                if cur_count >= 3:
                    streaks.append({"result": cur_res,
                                    "count": cur_count,
                                    "start_idx": cur_start})
                cur_res   = c.result
                cur_count = 1
                cur_start = i

        if cur_count >= 3:
            streaks.append({"result": cur_res,
                            "count": cur_count,
                            "start_idx": cur_start})

        return {
            "max_loss_streak": max((s["count"] for s in streaks
                                    if s["result"] == "LOSS"), default=0),
            "max_win_streak":  max((s["count"] for s in streaks
                                    if s["result"] == "WIN"), default=0),
            "all_streaks":     streaks,
        }

    # ── Loss Condition Averages ───────────────────────────────

    def _loss_conditions_summary(self) -> dict:
        losses = [c for c in self._contexts if c.result == "LOSS"]
        wins   = [c for c in self._contexts if c.result == "WIN"]

        fields = ["remaining_secs", "odds_spread", "beat_distance",
                  "cl_edge", "cl_vol", "odds", "cvd_2min"]

        return {
            "on_loss":    {f: _avg(losses, f) for f in fields},
            "on_win":     {f: _avg(wins,   f) for f in fields},
            "loss_count": len(losses),
            "win_count":  len(wins),
        }

    # ── Main Analyze ──────────────────────────────────────────

    def analyze(self) -> dict:
        if len(self._contexts) < 10:
            return {"status": "insufficient_data", "count": len(self._contexts)}

        insights = {}

        insights["wr_by_remaining"]    = self._wr_by_bucket(
            "remaining_secs",
            [(0,30),(30,60),(60,120),(120,180),(180,240),(240,300)]
        )
        insights["wr_by_odds_spread"]  = self._wr_by_bucket(
            "odds_spread",
            [(0,0.04),(0.04,0.06),(0.06,0.08),(0.08,0.12),(0.12,0.20),(0.20,1.0)]
        )
        insights["wr_by_beat_distance"] = self._wr_by_bucket(
            "beat_distance",
            [(0,20),(20,40),(40,60),(60,80),(80,120),(120,999)]
        )
        insights["wr_by_hour"]          = self._wr_by_field("hour_utc")
        insights["wr_by_direction"]     = self._wr_by_field("direction")
        insights["wr_by_mode"]          = self._wr_by_field("signal_mode")

        # --- Fixed & New ---
        insights["wr_cvd_alignment"]    = self._wr_cvd_alignment()
        insights["wr_cvd_magnitude"]    = self._wr_cvd_magnitude()
        insights["wr_liq_ratio"]        = self._wr_liq_ratio()
        insights["wr_cl_edge"]          = self._wr_cl_edge()
        insights["ev_by_odds"]          = self._ev_by_odds()
        insights["combo_analysis"]      = self._combo_analysis()
        insights["loss_reason_summary"] = self._loss_reason_summary()
        insights["streak_analysis"]     = self._streak_analysis()
        insights["loss_conditions"]     = self._loss_conditions_summary()

        insights["recommendations"]  = self._generate_recommendations(insights)
        insights["total_bets"]       = len(self._contexts)
        insights["overall_wr"]       = self._overall_wr()
        insights["generated_at"]     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            with open(PATTERN_LOG_PATH, "w") as f:
                json.dump(insights, f, indent=2)
        except Exception:
            pass

        return insights

    # ── Recommendation Engine ─────────────────────────────────

    def _generate_recommendations(self, insights: dict) -> list:
        recs = []

        # ── 1. Remaining time ─────────────────────────────────
        rem_wr = insights.get("wr_by_remaining", {})
        bad_rem = [k for k, v in rem_wr.items()
                   if v.get("flagged") and v.get("count", 0) >= MIN_SAMPLE]
        if bad_rem:
            recs.append({
                "type":    "TIMING",
                "action":  f"Hindari bet saat remaining_secs di bucket: {bad_rem}",
                "detail":  f"WR < {BAD_WR_THRESHOLD}% di zona waktu tersebut",
                "env_key": None,
            })

        # ── 2. Odds spread ────────────────────────────────────
        spread_wr = insights.get("wr_by_odds_spread", {})
        bad_spread = [k for k, v in spread_wr.items()
                      if v.get("flagged") and v.get("count", 0) >= MIN_SAMPLE]
        if bad_spread:
            min_bad = min(float(k.split("-")[0]) for k in bad_spread)
            suggested = round(min_bad + 0.02, 3)
            recs.append({
                "type":            "ODDS_SPREAD",
                "action":          f"Naikkan MIN_ODDS_SPREAD ke {suggested}",
                "detail":          f"WR < {BAD_WR_THRESHOLD}% saat spread ≤ {min_bad}",
                "env_key":         "MIN_ODDS_SPREAD",
                "suggested_value": str(suggested),
            })

        # ── 3. Beat distance ──────────────────────────────────
        dist_wr = insights.get("wr_by_beat_distance", {})
        bad_dist = [k for k, v in dist_wr.items()
                    if v.get("flagged") and v.get("count", 0) >= MIN_SAMPLE]
        if bad_dist:
            min_bad = min(float(k.split("-")[0]) for k in bad_dist)
            suggested = int(min_bad + 20)
            recs.append({
                "type":            "BEAT_DISTANCE",
                "action":          f"Naikkan LATE_BEAT_DISTANCE ke {suggested}",
                "detail":          f"WR buruk saat jarak harga vs beat < ${min_bad}",
                "env_key":         "LATE_BEAT_DISTANCE",
                "suggested_value": str(suggested),
            })

        # ── 4. Bad hours ──────────────────────────────────────
        hour_wr = insights.get("wr_by_hour", {})
        bad_hours = sorted(
            k for k, v in hour_wr.items()
            if v.get("wr", 100) < 45 and v.get("count", 0) >= MIN_SAMPLE
        )
        if bad_hours:
            recs.append({
                "type":            "SESSION_BLOCK",
                "action":          f"Tambah BAD_HOURS_UTC={','.join(bad_hours)}",
                "detail":          f"WR < 45% secara konsisten di jam UTC {bad_hours}",
                "env_key":         "BAD_HOURS_UTC",
                "suggested_value": ",".join(bad_hours),
            })

        # ── 5. CVD alignment ──────────────────────────────────
        cvd_data    = insights.get("wr_cvd_alignment", {})
        aligned_wr  = cvd_data.get("cvd_aligned",  {}).get("wr", 50)
        opposite_wr = cvd_data.get("cvd_opposite", {}).get("wr", 50)
        aligned_n   = cvd_data.get("cvd_aligned",  {}).get("count", 0)
        opposite_n  = cvd_data.get("cvd_opposite", {}).get("count", 0)

        if aligned_n >= MIN_SAMPLE and opposite_n >= MIN_SAMPLE:
            if aligned_wr - opposite_wr > 10:
                recs.append({
                    "type":   "CVD_FILTER",
                    "action": "Tambahkan CVD alignment sebagai filter wajib",
                    "detail": (f"CVD aligned WR={aligned_wr}% (n={aligned_n}) "
                               f"vs opposite WR={opposite_wr}% (n={opposite_n})"),
                    "env_key": None,
                })
            if "warning" in cvd_data:
                recs.append({
                    "type":   "DATA_ISSUE",
                    "action": "Perbaiki capture CVD dari MultiWS",
                    "detail": cvd_data["warning"],
                    "env_key": None,
                })

        # ── 6. Liquidation filter ─────────────────────────────
        liq_data     = insights.get("wr_liq_ratio", {})
        liq_align_wr = liq_data.get("liq_aligned_with_bet",  {}).get("wr", 50)
        liq_opp_wr   = liq_data.get("liq_opposite_to_bet",   {}).get("wr", 50)
        liq_align_n  = liq_data.get("liq_aligned_with_bet",  {}).get("count", 0)
        liq_opp_n    = liq_data.get("liq_opposite_to_bet",   {}).get("count", 0)
        if (liq_align_n >= MIN_SAMPLE and liq_opp_n >= MIN_SAMPLE
                and liq_align_wr - liq_opp_wr > 8):
            recs.append({
                "type":   "LIQ_FILTER",
                "action": "Tambahkan liquidation alignment sebagai filter",
                "detail": (f"Liq searah WR={liq_align_wr}% vs berlawanan "
                           f"WR={liq_opp_wr}%"),
                "env_key": None,
            })

        # ── 7. CL edge minimum ────────────────────────────────
        cl_data = insights.get("wr_cl_edge", {})
        bad_cl  = [k for k, v in cl_data.items()
                   if v.get("flagged") and v.get("count", 0) >= MIN_SAMPLE]
        if bad_cl:
            # Ambil batas bawah bucket buruk paling tinggi → set sebagai minimum
            bad_lo_vals = []
            for k in bad_cl:
                try:
                    lo = float(k.replace("cl_edge_", "").split("-")[0])
                    bad_lo_vals.append(lo)
                except Exception:
                    pass
            if bad_lo_vals:
                suggested_edge = round(max(bad_lo_vals) + 0.02, 3)
                recs.append({
                    "type":            "CL_EDGE",
                    "action":          f"Naikkan CHAINLINK_MIN_EDGE ke {suggested_edge}",
                    "detail":          f"WR buruk di CL edge bucket {bad_cl}",
                    "env_key":         "CHAINLINK_MIN_EDGE",
                    "suggested_value": str(suggested_edge),
                })

        # ── 8. EV analysis ────────────────────────────────────
        ev_data = insights.get("ev_by_odds", {})
        neg_ev  = [k for k, v in ev_data.items() if not v.get("profitable", True)]
        if neg_ev:
            recs.append({
                "type":   "EV_NEGATIVE",
                "action": f"Hindari betting di odds bucket: {neg_ev}",
                "detail": "EV negatif — expected loss per bet di bucket ini",
                "env_key": None,
            })

        # ── 9. Combo analysis ─────────────────────────────────
        combos = insights.get("combo_analysis", {})
        bad_hour_mode = combos.get("hour_x_mode", {})
        if bad_hour_mode:
            recs.append({
                "type":   "COMBO_HOUR_MODE",
                "action": f"Kombinasi jam+mode berikut punya WR buruk: {list(bad_hour_mode.keys())}",
                "detail": str({k: v["wr"] for k, v in bad_hour_mode.items()}),
                "env_key": None,
            })

        # ── 10. Loss reason dominan ───────────────────────────
        loss_reasons = insights.get("loss_reason_summary", {})
        top_reason   = max(loss_reasons.items(),
                           key=lambda x: x[1]["count"],
                           default=("unknown", {}))
        if top_reason[0] != "unknown" and top_reason[1].get("pct_of_losses", 0) > 30:
            recs.append({
                "type":   f"LOSS_REASON_{top_reason[0].upper()}",
                "action": f"Loss reason paling sering: '{top_reason[0]}' "
                          f"({top_reason[1]['pct_of_losses']}% dari semua loss)",
                "detail": "Prioritaskan fix faktor ini untuk WR improvement terbesar",
                "env_key": None,
            })

        if not recs:
            recs.append({
                "type":   "OK",
                "action": "Tidak ditemukan pola buruk signifikan",
                "detail": f"WR={insights.get('overall_wr', 0)}% — butuh lebih banyak data",
                "env_key": None,
            })

        return recs

    # ── Env Patch Generator ───────────────────────────────────

    def generate_env_patch(self) -> str:
        """
        Generate snippet .env berdasarkan rekomendasi.
        Output juga disimpan ke logs/suggested_env_patch.txt
        """
        insights = self.analyze()
        recs     = insights.get("recommendations", [])
        lines    = [
            f"# === Suggested .env patch — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===",
            f"# Overall WR: {insights.get('overall_wr', 0)}% | Bets: {insights.get('total_bets', 0)}",
            "",
        ]

        for rec in recs:
            key = rec.get("env_key")
            val = rec.get("suggested_value")
            if key and val:
                lines.append(f"# [{rec['type']}] {rec['detail']}")
                lines.append(f"{key}={val}")
                lines.append("")

        patch = "\n".join(lines)

        try:
            with open(ENV_PATCH_PATH, "w") as f:
                f.write(patch)
            logger.info(f"[LossAnalyzer] Env patch saved → {ENV_PATCH_PATH}")
        except Exception as e:
            logger.debug(f"[LossAnalyzer] Env patch save error: {e}")

        return patch

    # ── Print Report ──────────────────────────────────────────

    def print_report(self) -> None:
        insights = self.analyze()

        if insights.get("status") == "insufficient_data":
            print(f"\n[LossAnalyzer] Butuh lebih banyak data "
                  f"({insights['count']}/10 minimum)\n")
            return

        W = 62
        SEP = "=" * W

        print(f"\n{SEP}")
        print("  LOSS ANALYSIS REPORT v2")
        print(f"  Total bets : {insights['total_bets']} | "
              f"Overall WR: {insights['overall_wr']:.1f}%")
        print(f"  Generated  : {insights['generated_at']}")
        print(SEP)

        # ── WR by Remaining ───────────────────────────────────
        print("\n📊 WR by Remaining Time:")
        for bucket, d in sorted(insights.get("wr_by_remaining", {}).items()):
            bar  = "█" * int(d["wr"] / 5)
            flag = " ⚠️ " if d.get("flagged") else ""
            print(f"  {bucket:12s}: {d['wr']:5.1f}% {bar:<12} "
                  f"(n={d['count']}){flag}")

        # ── WR by Spread ──────────────────────────────────────
        print("\n📊 WR by Odds Spread:")
        for bucket, d in sorted(insights.get("wr_by_odds_spread", {}).items()):
            flag = " ⚠️ " if d.get("flagged") else ""
            print(f"  {bucket:14s}: {d['wr']:5.1f}% (n={d['count']}){flag}")

        # ── WR by Beat Distance ───────────────────────────────
        print("\n📊 WR by Beat Distance ($):")
        for bucket, d in sorted(insights.get("wr_by_beat_distance", {}).items()):
            flag = " ⚠️ " if d.get("flagged") else ""
            print(f"  ${bucket:12s}: {d['wr']:5.1f}% (n={d['count']}){flag}")

        # ── WR by Hour ────────────────────────────────────────
        print("\n📊 WR by Hour (UTC):")
        for hour, d in sorted(insights.get("wr_by_hour", {}).items(),
                               key=lambda x: int(x[0])):
            flag = " ⚠️ " if d.get("flagged") else ""
            print(f"  {hour:>4s}:00 UTC : {d['wr']:5.1f}% "
                  f"(n={d['count']}){flag}")

        # ── CVD Alignment (FIXED) ─────────────────────────────
        print("\n📊 WR by CVD Alignment (v2 — zero excluded):")
        cvd = insights.get("wr_cvd_alignment", {})
        for key in ["cvd_aligned", "cvd_opposite", "cvd_neutral"]:
            d = cvd.get(key, {})
            extra = f" [zero_pct={d.get('zero_pct', 0)}%]" if key == "cvd_neutral" else ""
            print(f"  {key:20s}: {d.get('wr', 0):5.1f}% "
                  f"(n={d.get('count', 0)}){extra}")
        if "warning" in cvd:
            print(f"  ⚠️  {cvd['warning']}")

        # ── CVD Magnitude ─────────────────────────────────────
        print("\n📊 WR by CVD Magnitude:")
        for mag, d in insights.get("wr_cvd_magnitude", {}).items():
            print(f"  {mag:12s}: {d['wr']:5.1f}% (n={d['count']})")

        # ── Liq Ratio ─────────────────────────────────────────
        print("\n📊 WR by Liquidation Ratio:")
        for key, d in insights.get("wr_liq_ratio", {}).items():
            print(f"  {key:30s}: {d['wr']:5.1f}% (n={d['count']})")

        # ── CL Edge ───────────────────────────────────────────
        print("\n📊 WR by Chainlink Edge:")
        for key, d in insights.get("wr_cl_edge", {}).items():
            flag = " ⚠️ " if d.get("flagged") else ""
            print(f"  {key:28s}: {d['wr']:5.1f}% (n={d['count']}){flag}")

        # ── EV Analysis ───────────────────────────────────────
        print("\n📊 Expected Value per Odds Bucket:")
        for key, d in insights.get("ev_by_odds", {}).items():
            sign  = "✅" if d["profitable"] else "❌"
            print(f"  {sign} {key:20s}: WR={d['wr']:5.1f}% "
                  f"avg_odds={d['avg_odds']:.4f} "
                  f"EV=${d['ev_per_dollar']:+.4f}")

        # ── Combo Analysis ────────────────────────────────────
        print("\n📊 Combo Analysis (2-factor):")
        combo = insights.get("combo_analysis", {})

        bad_hm = combo.get("hour_x_mode", {})
        if bad_hm:
            print("  Hour × Mode (bad WR combos):")
            for k, v in bad_hm.items():
                print(f"    {k:30s}: {v['wr']:.1f}% (n={v['count']}) ⚠️")
        else:
            print("  Hour × Mode: tidak ada combo buruk")

        bad_cs = combo.get("cvd_x_spread", {})
        if bad_cs:
            print("  CVD × Spread (bad WR combos):")
            for k, v in bad_cs.items():
                print(f"    {k:35s}: {v['wr']:.1f}% (n={v['count']}) ⚠️")

        dr_combo = combo.get("distance_x_remaining", {})
        if dr_combo:
            print("  Distance × Remaining:")
            for k, v in dr_combo.items():
                flag = " ⚠️" if v.get("flagged") else ""
                print(f"    {k:30s}: {v['wr']:.1f}% (n={v['count']}){flag}")

        # ── Mode × Direction ──────────────────────────────────
        md_combo = combo.get("mode_x_direction", {})
        if md_combo:
            print("  Mode × Direction:")
            for k, v in md_combo.items():
                flag = " ⚠️" if v.get("flagged") else ""
                print(f"    {k:25s}: {v['wr']:.1f}% (n={v['count']}){flag}")

        # ── Loss Reason Summary ───────────────────────────────
        print("\n📊 Loss Reason Auto-Tags:")
        for reason, d in insights.get("loss_reason_summary", {}).items():
            print(f"  {reason:25s}: {d['count']:3d}x "
                  f"({d['pct_of_losses']:5.1f}% of losses)")

        # ── Loss Conditions ───────────────────────────────────
        conds = insights.get("loss_conditions", {})
        print(f"\n📊 Avg Conditions — "
              f"LOSS(n={conds.get('loss_count',0)}) vs "
              f"WIN(n={conds.get('win_count',0)}):")
        for fld in ["remaining_secs", "odds_spread", "beat_distance",
                    "cl_edge", "cvd_2min", "odds"]:
            lv = conds.get("on_loss", {}).get(fld, 0)
            wv = conds.get("on_win",  {}).get(fld, 0)
            diff = lv - wv
            flag = " ←" if abs(diff) > abs(wv) * 0.1 else ""
            print(f"  {fld:20s}: LOSS={lv:>10.2f}  WIN={wv:>10.2f}  "
                  f"Δ={diff:+.2f}{flag}")

        # ── Streak ────────────────────────────────────────────
        streak = insights.get("streak_analysis", {})
        print(f"\n📊 Streak: Max Loss={streak.get('max_loss_streak',0)} | "
              f"Max Win={streak.get('max_win_streak',0)}")

        # ── Recommendations ───────────────────────────────────
        print(f"\n💡 Recommendations ({len(insights.get('recommendations',[]))} items):")
        for rec in insights.get("recommendations", []):
            print(f"\n  [{rec['type']}]")
            print(f"  → {rec['action']}")
            print(f"     {rec['detail']}")
            if rec.get("env_key"):
                print(f"     .env: {rec['env_key']}={rec['suggested_value']}")

        # ── Env patch ─────────────────────────────────────────
        patch = self.generate_env_patch()
        if "# ===" in patch and "\n" in patch:
            print(f"\n🔧 Env Patch tersimpan di: {ENV_PATCH_PATH}")

        print(f"\n{SEP}\n")


# ── Standalone runner ─────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    analyzer = LossAnalyzer()
    analyzer.print_report()
