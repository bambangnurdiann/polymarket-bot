"""
fetcher/chainlink_monitor.py
============================
Chainlink oracle monitor dengan 4 improve utama + Round Boundary Tracking:

1. AUTO-CALIBRATION VOLATILITY
   Hitung volatility nyata dari historical Chainlink rounds.
2. MOMENTUM FILTER
   Cek apakah 3 round terakhir Chainlink bergerak searah sinyal.
3. TIME DECAY EDGE REQUIREMENT
   Edge minimum dinamis berdasarkan sisa waktu window.
4. ODDS SPREAD FILTER
   Skip kalau selisih UP dan DOWN odds < 4%.
5. ROUND BOUNDARY TRACKING (Patch Terintegrasi)
   Melacak kesegaran (freshness) data, arah pergerakan, dan kekuatan (strength) round baru.
"""

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CHAINLINK_FEEDS = {
    "BTC":  "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH":  "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL":  "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "DOGE": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444",
}

POLYGON_RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://rpc.ankr.com/polygon",
    "https://matic-mainnet.chainstacklabs.com",
]

CHAINLINK_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",         "type": "uint80"},
            {"name": "answer",          "type": "int256"},
            {"name": "startedAt",       "type": "uint256"},
            {"name": "updatedAt",       "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class ChainlinkSnapshot:
    price:      float
    round_id:   int
    updated_at: int
    fetched_at: float


@dataclass
class MispricingSignal:
    coin:            str
    direction:       str
    chainlink_price: float
    beat_price:      float
    current_odds:    float
    fair_odds:       float
    edge:            float
    remaining_secs:  float
    confidence:      float
    reason:          str
    momentum_ok:     bool  = True
    vol_calibrated:  float = 0.001


class ChainlinkMonitor:
    """
    Real-time Chainlink monitor dengan auto-calibration dan smart filters.
    """

    VOL_HISTORY_SIZE = 50

    def __init__(self, coins: list, poll_interval: float = 2.5):
        self.coins         = [c.upper() for c in coins if c.upper() in CHAINLINK_FEEDS]
        self.poll_interval = poll_interval
        self.prices:       Dict[str, Optional[ChainlinkSnapshot]] = {c: None for c in self.coins}
        self.prev_prices:  Dict[str, Optional[ChainlinkSnapshot]] = {c: None for c in self.coins}
        self.new_round:    Dict[str, bool]  = {c: False for c in self.coins}
        self.is_connected  = False
        self._contracts:   Dict = {}
        self._w3           = None
        self._task         = None
        self._running      = False
        self._decimals:    Dict[str, int]   = {}
        self._round_history: Dict[str, deque] = {c: deque(maxlen=self.VOL_HISTORY_SIZE) for c in self.coins}
        self._vol_calibrated: Dict[str, float] = {c: 0.001 for c in self.coins}
        self._vol_last_calc:  Dict[str, float] = {c: 0.0   for c in self.coins}
        
        # === Fitur Baru: Round boundary tracking ===
        self._round_event_ts:  Dict[str, float] = {c: 0.0  for c in self.coins}
        self._round_direction: Dict[str, str]   = {c: ""   for c in self.coins}
        self._consecutive_dir: Dict[str, int]   = {c: 0    for c in self.coins}
        self._round_delta:     Dict[str, float] = {c: 0.0  for c in self.coins}

    def _init_web3(self) -> bool:
        try:
            from web3 import Web3
            for rpc in POLYGON_RPC_URLS:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
                    if w3.is_connected():
                        self._w3 = w3
                        for coin in self.coins:
                            addr     = CHAINLINK_FEEDS[coin]
                            contract = w3.eth.contract(
                                address=Web3.to_checksum_address(addr),
                                abi=CHAINLINK_ABI,
                            )
                            self._contracts[coin]  = contract
                            self._decimals[coin]   = contract.functions.decimals().call()
                        self.is_connected = True
                        logger.info(f"[ChainlinkMonitor] Connected via {rpc} — coins: {self.coins}")
                        return True
                except Exception as e:
                    logger.debug(f"[ChainlinkMonitor] RPC {rpc} failed: {e}")
            return False
        except ImportError:
            logger.error("[ChainlinkMonitor] web3 tidak terinstall")
            return False

    async def start(self) -> None:
        self._running = True
        self._task    = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        ok   = await loop.run_in_executor(None, self._init_web3)
        if not ok:
            logger.error("[ChainlinkMonitor] Gagal init Web3")
            return
        while self._running:
            try:
                await loop.run_in_executor(None, self._fetch_all)
            except Exception as e:
                logger.debug(f"[ChainlinkMonitor] Poll error: {e}")
                self.is_connected = False
                await asyncio.sleep(5)
                await loop.run_in_executor(None, self._init_web3)
            await asyncio.sleep(self.poll_interval)

    def _fetch_all(self) -> None:
        for coin in self.coins:
            try:
                self._fetch_coin(coin)
            except Exception as e:
                logger.debug(f"[ChainlinkMonitor] Fetch {coin} error: {e}")

    def _fetch_coin(self, coin: str) -> None:
        contract = self._contracts.get(coin)
        if not contract:
            return
        data       = contract.functions.latestRoundData().call()
        round_id   = data[0]
        raw_price  = data[1]
        updated_at = data[3]
        decimals   = self._decimals.get(coin, 8)
        price      = raw_price / (10 ** decimals)

        snap = ChainlinkSnapshot(
            price=price, round_id=round_id,
            updated_at=updated_at, fetched_at=time.time(),
        )
        prev = self.prices.get(coin)
        
        # Mengecek apakah ada ronde (round) harga baru dari Chainlink
        if prev and prev.round_id != round_id:
            self.new_round[coin] = True
            delta = price - prev.price
            
            # === Fitur Baru: Update data Round Boundary ===
            direction = "UP" if delta > 0 else ("DOWN" if delta < 0 else "")
            
            if direction:
                # Jika arahnya sama dengan sebelumnya, tambah streak berturut-turut
                if direction == self._round_direction.get(coin, ""):
                    self._consecutive_dir[coin] = self._consecutive_dir.get(coin, 0) + 1
                else:
                    self._consecutive_dir[coin] = 1 # Reset ke 1 jika arah berubah
                
                # Simpan arah, perubahan harga, dan waktu saat ini
                self._round_direction[coin] = direction
                self._round_delta[coin]     = delta
                self._round_event_ts[coin]  = time.time()
            # ===============================================

            logger.info(f"[ChainlinkMonitor] NEW ROUND {coin}: ${prev.price:,.2f} → ${price:,.2f} (Δ{delta:+.2f})")
            self._round_history[coin].append((price, time.time()))
            
            if len(self._round_history[coin]) >= 10:
                self._recalculate_volatility(coin)
        else:
            self.new_round[coin] = False

        self.prev_prices[coin] = self.prices[coin]
        self.prices[coin]      = snap
        self.is_connected      = True

    # ── IMPROVE 1: Auto-calibration ──────────────────────────

    def _recalculate_volatility(self, coin: str) -> None:
        """Hitung volatility per menit dari historical round data."""
        history = list(self._round_history[coin])
        if len(history) < 5:
            return
        returns = []
        for i in range(1, len(history)):
            p_prev, t_prev = history[i-1]
            p_curr, t_curr = history[i]
            if p_prev <= 0:
                continue
            ret    = (p_curr - p_prev) / p_prev
            dt_min = max((t_curr - t_prev) / 60.0, 0.01)
            returns.append(ret / math.sqrt(dt_min))

        if len(returns) < 3:
            return
        mean = sum(returns) / len(returns)
        var  = sum((r - mean)**2 for r in returns) / len(returns)
        std  = math.sqrt(var)
        if std > 0:
            self._vol_calibrated[coin] = std
            self._vol_last_calc[coin]  = time.time()
            logger.info(f"[ChainlinkMonitor] Vol calibrated {coin}: {std:.6f}/min from {len(returns)} samples")

    def get_calibrated_vol(self, coin: str) -> float:
        """Ambil volatility terkalibrasi dengan floor dan ceiling."""
        vol = self._vol_calibrated.get(coin, 0.001)
        return max(0.0008, min(0.01, vol)) 

    # ── IMPROVE 2: Momentum filter ────────────────────────────

    def check_momentum(self, coin: str, direction: str, lookback: int = 3) -> tuple:
        """Cek apakah N round terakhir bergerak searah sinyal."""
        history = list(self._round_history[coin])
        if len(history) < lookback + 1:
            return True, "Not enough history (pass)"
        recent = history[-(lookback+1):]
        deltas = [recent[i+1][0] - recent[i][0] for i in range(len(recent)-1)]
        if not deltas:
            return True, "No deltas (pass)"
        up_count   = sum(1 for d in deltas if d > 0)
        down_count = sum(1 for d in deltas if d < 0)
        total      = len(deltas)
        if direction == "UP":
            ok     = up_count >= total * 0.4
            reason = f"Momentum: {up_count}/{total} rounds naik"
        else:
            ok     = down_count >= total * 0.4
            reason = f"Momentum: {down_count}/{total} rounds turun"
        return ok, reason

    # ── IMPROVE 3: Time decay edge ────────────────────────────

    def get_dynamic_min_edge(self, remaining: float, base_min_edge: float = 0.10) -> float:
        """Edge minimum dinamis berdasarkan sisa waktu."""
        if remaining > 240:
            return base_min_edge * 1.5
        elif remaining > 120:
            return base_min_edge * 1.2
        elif remaining > 60:
            return base_min_edge
        elif remaining > 30:
            return base_min_edge * 1.3
        else:
            return base_min_edge * 1.6

    # ── Fair odds calculation ─────────────────────────────────

    def calc_fair_odds(
        self,
        coin:        str,
        direction:   str,
        beat_price:  float,
        remaining:   float,
        vol_per_min: float = None,
    ) -> float:
        snap = self.prices.get(coin)
        if not snap or not beat_price or beat_price <= 0:
            return 0.5
        if vol_per_min is None:
            vol_per_min = self.get_calibrated_vol(coin)
        price = snap.price
        diff  = price - beat_price
        T_min = max(remaining / 60.0, 0.1)
        sigma = vol_per_min * math.sqrt(T_min) * beat_price
        if sigma <= 0:
            return 0.5
        d       = max(-4.0, min(4.0, diff / sigma))
        prob_up = self._norm_cdf(d)
        return prob_up if direction == "UP" else 1.0 - prob_up

    def _norm_cdf(self, x: float) -> float:
        if x < -6: return 0.0
        if x >  6: return 1.0
        k    = 1.0 / (1.0 + 0.2316419 * abs(x))
        poly = k * (0.319381530
               + k * (-0.356563782
               + k * (1.781477937
               + k * (-1.821255978
               + k * 1.330274429))))
        pdf    = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
        result = 1.0 - pdf * poly
        return result if x >= 0 else 1.0 - result

    # ── MAIN: detect_mispricing dengan semua filter ───────────

    def detect_mispricing(
        self,
        coin:            str,
        direction:       str,
        beat_price:      float,
        remaining:       float,
        current_odds:    float,
        min_edge:        float = 0.10,
        vol_per_min:     float = None,
        odds_up:         float = 0.5,
        odds_down:       float = 0.5,
        use_momentum:    bool  = True,
        use_time_decay:  bool  = True,
        min_odds_spread: float = 0.04,
    ) -> Optional[MispricingSignal]:
        
        snap = self.prices.get(coin)
        if not snap:
            return None

        # IMPROVE 4: Odds spread filter
        odds_spread = abs(odds_up - odds_down)
        if odds_spread < min_odds_spread:
            return None

        # IMPROVE 3: Dynamic edge requirement
        effective_min_edge = (
            self.get_dynamic_min_edge(remaining, min_edge)
            if use_time_decay else min_edge
        )

        fair_odds = self.calc_fair_odds(coin, direction, beat_price, remaining, vol_per_min)
        edge      = fair_odds - current_odds

        if edge < effective_min_edge:
            return None

        # IMPROVE 2: Momentum filter
        momentum_ok     = True
        momentum_reason = ""
        if use_momentum:
            momentum_ok, momentum_reason = self.check_momentum(coin, direction)
            if not momentum_ok:
                logger.debug(f"[ChainlinkMonitor] {coin} {direction} momentum FAIL: {momentum_reason}")
                return None

        # Confidence score
        age_seconds = time.time() - snap.fetched_at
        freshness   = max(0, 1.0 - age_seconds / 10.0)
        vol_cal     = self.get_calibrated_vol(coin)
        vol_bonus   = 0.1 if self._vol_last_calc.get(coin, 0) > 0 else 0
        confidence  = min(1.0, (edge / 0.25) * 0.6 + freshness * 0.3 + vol_bonus)

        price  = snap.price
        diff   = price - beat_price
        reason = (
            f"CL ${price:,.2f} beat ${beat_price:,.2f} (Δ{diff:+.2f}) | "
            f"fair={fair_odds:.3f} odds={current_odds:.3f} edge={edge:+.3f} | "
            f"vol={vol_cal:.5f} rem={remaining:.0f}s req={effective_min_edge:.3f}"
        )
        if momentum_reason:
            reason += f" | {momentum_reason}"

        return MispricingSignal(
            coin=coin, direction=direction,
            chainlink_price=price, beat_price=beat_price,
            current_odds=current_odds, fair_odds=fair_odds,
            edge=edge, remaining_secs=remaining,
            confidence=confidence, reason=reason,
            momentum_ok=momentum_ok, vol_calibrated=vol_cal,
        )

    # ── METODE BARU DARI PATCH: Round Boundary Info ───────────

    def get_round_age(self, coin: str) -> float:
        """Berapa detik sejak round baru terdeteksi. 999 jika belum ada."""
        ts = self._round_event_ts.get(coin, 0.0)
        if ts == 0:
            return 999.0
        return time.time() - ts

    def is_round_fresh(self, coin: str, max_age_secs: float = 15.0) -> bool:
        """True jika round baru terdeteksi kurang dari max_age_secs yang lalu."""
        return self.get_round_age(coin) <= max_age_secs

    def get_round_direction(self, coin: str) -> str:
        """Arah delta round terakhir: 'UP', 'DOWN', atau '' jika belum ada."""
        return self._round_direction.get(coin, "")

    def get_round_delta(self, coin: str) -> float:
        """Delta harga dari round terakhir dalam USD."""
        return self._round_delta.get(coin, 0.0)

    def get_consecutive_direction(self, coin: str) -> int:
        """Berapa round berturut-turut bergerak arah yang sama."""
        return self._consecutive_dir.get(coin, 0)

    def get_round_strength(self, coin: str) -> str:
        """
        Klasifikasi kekuatan round baru:
          STRONG  : |delta| > $200, consecutive >= 2
          MODERATE: |delta| > $100
          WEAK    : |delta| < $50
          NONE    : belum ada round baru
        """
        delta  = abs(self._round_delta.get(coin, 0))
        consec = self._consecutive_dir.get(coin, 0)
        
        if delta == 0:
            return "NONE"
        if delta > 200 and consec >= 2:
            return "STRONG"
        if delta > 100:
            return "MODERATE"
        if delta < 50:
            return "WEAK"
        return "MODERATE"

    # ── INFO & STATUS ─────────────────────────────────────────

    def get_price(self, coin: str) -> Optional[float]:
        snap = self.prices.get(coin)
        return snap.price if snap else None

    def get_price_age(self, coin: str) -> float:
        snap = self.prices.get(coin)
        return (time.time() - snap.fetched_at) if snap else 999.0

    def get_vol_info(self, coin: str) -> str:
        vol     = self._vol_calibrated.get(coin, 0.001)
        age     = time.time() - self._vol_last_calc.get(coin, 0)
        samples = len(self._round_history.get(coin, []))
        if self._vol_last_calc.get(coin, 0) > 0:
            return f"vol={vol:.5f}({samples}smp)"
        return f"vol=default"

    @property
    def status(self) -> str:
        return "OK" if self.is_connected else "ERR"