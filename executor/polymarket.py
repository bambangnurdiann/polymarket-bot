"""
executor/polymarket.py
======================
Eksekutor order ke Polymarket via CLOB (Central Limit Order Book) API.

Tanggung jawab:
  1. Autentikasi ke Polymarket API
  2. Fetch market aktif BTC 5-menit
  3. Fetch odds (harga token) UP/DOWN
  4. Submit order FOK (Fill-or-Kill)
  5. Claim posisi yang sudah menang (via relayer)
  6. Cek saldo akun

Polymarket menggunakan:
  - Polygon network untuk transaksi
  - CLOB API untuk order book
  - Proxy wallet (Safe) untuk menyimpan USDC
  - Relayer untuk gasless transactions
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Polymarket API endpoints
CLOB_BASE_URL  = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_BASE_URL  = "https://data-api.polymarket.com"

# Market tag untuk BTC 5-menit
BTC_5MIN_SLUG_PATTERN = "btc-up-or-down"


class PolymarketRelayer:
    """
    Handle gasless transactions via Polymarket Relayer.
    Dibutuhkan untuk claim posisi yang menang.
    """

    RELAYER_URL = "https://relayer.polymarket.com"

    def __init__(self, api_key: str, api_key_address: str, private_key: str, funder: str):
        self._api_key = api_key
        self._api_key_address = api_key_address
        self._funder = funder
        self._account = None

        try:
            from eth_account import Account
            self._account = Account.from_key(private_key)
        except Exception as e:
            logger.error(f"[Relayer] Failed init account: {e}")

    def is_available(self) -> bool:
        return bool(self._api_key and self._account and self._funder)

    def _get_headers(self) -> dict:
        return {
            "POLY_ADDRESS":    self._api_key_address,
            "POLY_API_KEY":    self._api_key,
            "Content-Type":    "application/json",
        }

    def redeem_positions(self, condition_id: str) -> bool:
        """
        Redeem (claim) posisi yang sudah menang.
        Returns True jika berhasil.
        """
        if not self.is_available():
            return False
        try:
            payload = {
                "conditionId": condition_id,
                "funder":      self._funder,
            }
            resp = requests.post(
                f"{self.RELAYER_URL}/redeem",
                json=payload,
                headers=self._get_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(f"[Relayer] Redeemed conditionId={condition_id[:16]}...")
                return True
            else:
                logger.warning(f"[Relayer] Redeem failed: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"[Relayer] Redeem error: {e}")
            return False


class PolymarketExecutor:
    """
    Eksekutor utama untuk semua operasi Polymarket.

    Attributes:
        dry_run   : bool  — Jika True, tidak benar-benar submit order
        balance   : float — Saldo USDC terakhir diketahui
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.balance: float = 0.0
        self._client = None
        self._relayer: Optional[PolymarketRelayer] = None
        self._initialized = False
        self._active_market_cache: dict = {}
        self._cache_time: float = 0.0
        self._odds_cache: dict = {}
        self._odds_cache_time: float = 0.0

        self._private_key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self._funder       = os.getenv("POLYMARKET_FUNDER", "")
        self._api_key      = os.getenv("POLYMARKET_API_KEY", "")
        self._api_secret   = os.getenv("POLYMARKET_API_SECRET", "")
        self._api_pass     = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        self._relayer_key  = os.getenv("RELAYER_API_KEY", "")
        self._relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS", "")

        self._init()

    def _init(self) -> None:
        """Inisialisasi CLOB client."""
        if not self._private_key:
            logger.warning("[Executor] POLYMARKET_PRIVATE_KEY tidak ditemukan di .env")
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_pass,
            )

            self._client = ClobClient(
                host=CLOB_BASE_URL,
                key=self._private_key,
                chain_id=137,  # Polygon
                creds=creds,
                funder=self._funder,
            )

            # Setup relayer
            if self._relayer_key and self._relayer_addr:
                self._relayer = PolymarketRelayer(
                    api_key=self._relayer_key,
                    api_key_address=self._relayer_addr,
                    private_key=self._private_key,
                    funder=self._funder,
                )

            self._initialized = True
            logger.info("[Executor] CLOB client initialized")

        except ImportError:
            logger.error("[Executor] py-clob-client tidak terinstall. Run: pip install py-clob-client")
        except Exception as e:
            logger.error(f"[Executor] Init error: {e}")

    def get_balance(self) -> float:
        """Ambil saldo USDC dari Polymarket."""
        if not self._initialized:
            return 0.0
        try:
            # py-clob-client versi terbaru tidak punya get_balance(),
            # gunakan get_balance_allowance(asset_type=COLLATERAL).
            if hasattr(self._client, "get_balance_allowance"):
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                resp = self._client.get_balance_allowance(params)

                # Bentuk response bisa beda antar versi:
                # {"balance": "..."} atau {"balance": {"decimal": "..."}}
                raw_balance = None
                if isinstance(resp, dict):
                    raw_balance = resp.get("balance")
                    if isinstance(raw_balance, dict):
                        raw_balance = (
                            raw_balance.get("decimal")
                            or raw_balance.get("value")
                            or raw_balance.get("balance")
                        )
                if raw_balance is not None:
                    self.balance = float(raw_balance)
                    return self.balance

                logger.warning(f"[Executor] Unexpected balance response shape: {resp}")
                return self.balance

            # Fallback untuk py-clob-client versi lama
            if hasattr(self._client, "get_balance"):
                balance = self._client.get_balance()
                self.balance = float(balance) if balance else 0.0
                return self.balance

            logger.error("[Executor] Client tidak mendukung get_balance/get_balance_allowance")
            return self.balance
        except Exception as e:
            logger.warning(f"[Executor] get_balance error: {e}")
            return self.balance

    # Slug prefix per coin untuk market 5-menit
    COIN_SLUG_PREFIX = {
        "BTC":  ["btc-updown-5m", "btc-up-or-down", "bitcoin-updown-5m"],
        "ETH":  ["eth-updown-5m", "eth-up-or-down", "ethereum-updown-5m"],
        "SOL":  ["sol-updown-5m", "sol-up-or-down", "solana-updown-5m"],
        "DOGE": ["doge-updown-5m", "doge-up-or-down", "dogecoin-updown-5m"],
        "XRP":  ["xrp-updown-5m", "xrp-up-or-down"],
    }

    COIN_QUESTION_KW = {
        "BTC":  ["bitcoin", "btc"],
        "ETH":  ["ethereum", "eth"],
        "SOL":  ["solana", "sol"],
        "DOGE": ["dogecoin", "doge"],
        "XRP":  ["xrp", "ripple"],
    }

    def get_active_btc_market(self, force_refresh: bool = False) -> Optional[dict]:
        """Alias backward compatibility."""
        return self.get_active_market("BTC", force_refresh)

    def get_active_market(self, coin: str = "BTC", force_refresh: bool = False) -> Optional[dict]:
        """
        Fetch market 5-menit yang aktif untuk coin tertentu.
        Mencoba beberapa metode fetch secara berurutan.
        Di-cache 30 detik per coin.
        """
        coin = coin.upper()
        now  = time.time()
        if not hasattr(self, "_market_cache"):
            self._market_cache:    dict = {}
            self._market_cache_ts: dict = {}

        if (not force_refresh and coin in self._market_cache
                and (now - self._market_cache_ts.get(coin, 0)) < 30):
            return self._market_cache[coin]

        result = (
            self._fetch_market_via_events(coin) or
            self._fetch_market_via_clob(coin) or
            self._fetch_market_via_search(coin)
        )

        if result:
            self._market_cache[coin]    = result
            self._market_cache_ts[coin] = now
            logger.info(f"[Executor] Market {coin} found: {result.get('question','')[:60]}")

        return result or self._market_cache.get(coin)

    def _fetch_market_via_events(self, coin: str) -> Optional[dict]:
        """Fetch via Gamma events API menggunakan slug prefix btc-updown-5m."""
        slug_prefix = f"{coin.lower()}-updown-5m"
        kw_list     = self.COIN_QUESTION_KW.get(coin, [coin.lower()])

        try:
            resp = requests.get(
                f"{GAMMA_BASE_URL}/events",
                params={
                    "active":     "true",
                    "limit":      "50",
                    "order":      "createdAt",
                    "ascending":  "false",
                },
                timeout=8,
            )
            if resp.status_code != 200:
                return None

            events = resp.json()
            if not isinstance(events, list):
                return None

            # Cari event dengan slug btc-updown-5m-* yang paling baru
            # dan acceptingOrders=true
            best = None
            for ev in events:
                ev_slug = ev.get("slug", "").lower()
                if not ev_slug.startswith(slug_prefix):
                    continue

                markets = ev.get("markets", [])
                if not markets:
                    continue

                m = markets[0]

                # Harus accepting orders
                if not m.get("acceptingOrders", False):
                    continue

                result = self._parse_market_dict(m, coin)
                if result:
                    # Overwrite question dengan title event yang lebih deskriptif
                    result["question"] = ev.get("title", result["question"])
                    best = result
                    break  # sudah sorted by createdAt desc, ambil yang pertama

            return best

        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_events({coin}): {e}")
        return None

    def _fetch_market_via_clob(self, coin: str) -> Optional[dict]:
        """Fetch via CLOB simplified markets API."""
        kw_list = self.COIN_QUESTION_KW.get(coin, [coin.lower()])
        try:
            resp = requests.get(
                f"{CLOB_BASE_URL}/markets",
                params={"active": "true", "closed": "false", "limit": "100"},
                timeout=5,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            for m in markets:
                q    = m.get("question", "").lower()
                slug = m.get("market_slug", "").lower()
                coin_match = any(k in q or k in slug for k in kw_list)
                time_match = ("5 min" in q or "5-min" in q or
                              "updown" in slug or "5m" in slug)
                dir_match  = "up" in q and "down" in q
                if coin_match and time_match and dir_match:
                    token_up = token_down = None
                    for t in m.get("tokens", []):
                        out = t.get("outcome", "").upper()
                        if out == "UP":
                            token_up   = t.get("token_id")
                        elif out == "DOWN":
                            token_down = t.get("token_id")
                    if token_up and token_down:
                        return {
                            "coin":          coin,
                            "market_id":     m.get("condition_id") or m.get("conditionId"),
                            "question":      m.get("question", ""),
                            "token_id_up":   token_up,
                            "token_id_down": token_down,
                            "end_date":      m.get("end_date_iso", ""),
                        }
        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_clob({coin}): {e}")
        return None

    def _fetch_market_via_search(self, coin: str) -> Optional[dict]:
        """Fetch via Gamma markets search sebagai last resort."""
        kw_list = self.COIN_QUESTION_KW.get(coin, [coin.lower()])
        queries = [f"{kw} up or down 5" for kw in kw_list[:1]]
        try:
            for q in queries:
                resp = requests.get(
                    f"{GAMMA_BASE_URL}/markets",
                    params={"search": q, "active": "true", "limit": "10"},
                    timeout=5,
                )
                if resp.status_code != 200:
                    continue
                for m in resp.json():
                    question = m.get("question", "").lower()
                    slug     = m.get("slug", "").lower()
                    coin_ok  = any(k in question or k in slug for k in kw_list)
                    dir_ok   = "up" in question and "down" in question
                    time_ok  = "5" in question or "5m" in slug
                    if coin_ok and dir_ok and time_ok:
                        result = self._parse_market_dict(m, coin)
                        if result:
                            return result
        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_search({coin}): {e}")
        return None

    def _extract_from_event(self, event: dict, coin: str) -> Optional[dict]:
        """Extract market data dari event object."""
        markets = event.get("markets", [])
        if not markets:
            return None
        # Ambil market pertama yang punya token UP dan DOWN
        for m in markets:
            result = self._parse_market_dict(m, coin)
            if result:
                result["question"] = event.get("title", result["question"])
                return result
        return None

    def _parse_market_dict(self, m: dict, coin: str) -> Optional[dict]:
        """
        Parse satu market dict dan extract token IDs.
        clobTokenIds dan outcomes bisa berupa JSON string atau list Python.
        """
        token_up = token_down = None

        # Helper: parse field yang mungkin JSON string atau sudah list
        def parse_field(val):
            if isinstance(val, list):
                return val
            if isinstance(val, str):
                try:
                    import json as _json
                    return _json.loads(val)
                except Exception:
                    return []
            return []

        clob_ids = parse_field(m.get("clobTokenIds", []))
        outcomes = parse_field(m.get("outcomes", []))

        # Map token IDs berdasarkan outcomes
        if len(clob_ids) >= 2 and len(outcomes) >= 2:
            for i, out in enumerate(outcomes):
                out_str = str(out).strip().upper()
                if out_str in ("UP", "HIGHER", "YES") and i < len(clob_ids):
                    token_up   = clob_ids[i]
                elif out_str in ("DOWN", "LOWER", "NO") and i < len(clob_ids):
                    token_down = clob_ids[i]
        elif len(clob_ids) >= 2:
            # Tidak ada outcomes info → asumsikan index 0=Up, 1=Down
            token_up   = clob_ids[0]
            token_down = clob_ids[1]

        # Fallback: tokens sebagai list of dict
        if not (token_up and token_down):
            tokens = parse_field(m.get("tokens", []))
            for t in tokens:
                if not isinstance(t, dict):
                    continue
                out = t.get("outcome", "").upper()
                if out in ("UP", "HIGHER", "YES"):
                    token_up   = t.get("token_id") or t.get("tokenId")
                elif out in ("DOWN", "LOWER", "NO"):
                    token_down = t.get("token_id") or t.get("tokenId")

        if not (token_up and token_down):
            return None

        return {
            "coin":          coin,
            "market_id":     m.get("conditionId") or m.get("condition_id") or m.get("id"),
            "question":      m.get("question", f"{coin} Up or Down"),
            "token_id_up":   token_up,
            "token_id_down": token_down,
            "end_date":      m.get("endDate") or m.get("endDateIso") or m.get("end_date_iso", ""),
        }

    def get_odds(self, market: dict) -> tuple[float, float]:
        """
        Ambil odds UP dan DOWN.
        Prioritas: Gamma outcomePrices → CLOB book → CLOB price endpoint
        """
        now = time.time()
        cache_key = market.get("market_id", "")
        if cache_key and (now - self._odds_cache_time) < 3:
            cached = self._odds_cache.get(cache_key)
            if cached:
                return cached

        # ── Cara 1: Gamma API outcomePrices (paling reliable) ─
        try:
            cond_id = market.get("market_id", "")
            resp = requests.get(
                f"{GAMMA_BASE_URL}/markets",
                params={"conditionId": cond_id},
                timeout=4,
            )
            if resp.status_code == 200:
                import json as _json
                data    = resp.json()
                markets = data if isinstance(data, list) else [data]

                # Cari exact match conditionId dulu, fallback ke index 0
                target = next((m for m in markets if m.get("conditionId") == cond_id), None)
                if not target and markets:
                    target = markets[0]

                if target:
                    raw      = target.get("outcomePrices", "[]")
                    prices   = _json.loads(raw) if isinstance(raw, str) else raw
                    out_raw  = target.get("outcomes", "[]")
                    outcomes = _json.loads(out_raw) if isinstance(out_raw, str) else out_raw

                    if len(prices) >= 2:
                        odds_up = odds_down = 0.5
                        if len(outcomes) >= 2:
                            for i, out in enumerate(outcomes):
                                out_u = str(out).upper()
                                if out_u in ("UP", "HIGHER", "YES") and i < len(prices):
                                    odds_up   = float(prices[i])
                                elif out_u in ("DOWN", "LOWER", "NO") and i < len(prices):
                                    odds_down = float(prices[i])
                        else:
                            odds_up   = float(prices[0])
                            odds_down = float(prices[1])

                        if 0.01 < odds_up < 0.99 and 0.01 < odds_down < 0.99:
                            result = (odds_up, odds_down)
                            self._odds_cache[cache_key] = result
                            self._odds_cache_time = now
                            return result
        except Exception as e:
            logger.debug(f"[Executor] get_odds gamma error: {e}")

        # ── Cara 2: CLOB midpoint price ───────────────────────
        try:
            token_up   = market["token_id_up"]
            token_down = market["token_id_down"]

            r1 = requests.get(
                f"{CLOB_BASE_URL}/midpoints",
                params={"token_ids": f"{token_up},{token_down}"},
                timeout=3,
            )
            if r1.status_code == 200:
                mids = r1.json()
                up   = float(mids.get(token_up, 0) or 0)
                down = float(mids.get(token_down, 0) or 0)
                if 0.01 < up < 0.99 and 0.01 < down < 0.99:
                    result = (up, down)
                    self._odds_cache[cache_key] = result
                    self._odds_cache_time = now
                    return result
        except Exception as e:
            logger.debug(f"[Executor] get_odds midpoints error: {e}")

        # ── Cara 3: CLOB order book ───────────────────────────
        try:
            token_up   = market["token_id_up"]
            token_down = market["token_id_down"]

            def best_ask(token_id: str) -> float:
                r = requests.get(
                    f"{CLOB_BASE_URL}/book",
                    params={"token_id": token_id},
                    timeout=3,
                )
                if r.status_code == 200:
                    book = r.json()
                    asks = book.get("asks", [])
                    if asks:
                        p = float(asks[0].get("price", 0))
                        if 0.01 < p < 0.99:
                            return p
                return 0.5

            odds_up   = best_ask(token_up)
            odds_down = best_ask(token_down)
            result    = (odds_up, odds_down)
            self._odds_cache[cache_key] = result
            self._odds_cache_time = now
            return result

        except Exception as e:
            logger.debug(f"[Executor] get_odds book error: {e}")

        return (0.5, 0.5)

    def place_order(
        self,
        token_id:   str,
        amount:     float,
        side:       str,   # "BUY"
        price:      float, # odds (0-1)
        direction:  str,   # "UP" atau "DOWN" untuk logging
    ) -> bool:
        """
        Submit order FOK (Fill-or-Kill) ke Polymarket.

        Args:
            token_id  : Token ID dari Polymarket
            amount    : Jumlah USDC yang mau dibet
            side      : "BUY"
            price     : Odds (harga token, antara 0 dan 1)
            direction : Label untuk logging

        Returns:
            True jika order berhasil terisi (filled)
        """
        if self.dry_run:
            logger.info(f"[Executor] DRY_RUN: {direction} ${amount:.2f} @ {price:.4f}")
            return True

        if not self._initialized:
            logger.error("[Executor] Client belum initialized")
            return False

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            # Hitung size dalam shares (bukan USDC)
            # Di Polymarket: amount_usdc = size * price
            # Jadi: size = amount_usdc / price
            size = round(amount / price, 2) if price > 0 else amount

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )

            resp = self._client.create_and_post_order(order_args, OrderType.FOK)

            if resp and resp.get("status") in ("matched", "filled", "MATCHED"):
                logger.info(f"[Executor] Order FILLED: {direction} ${amount:.2f} @ {price:.4f}")
                return True
            else:
                status = resp.get("status", "unknown") if resp else "no response"
                logger.warning(f"[Executor] Order NOT filled: {status} | {direction}")
                return False

        except Exception as e:
            err_str = str(e).lower()
            if "no match" in err_str:
                logger.warning(f"[Executor] No counterparty (no match) untuk {direction}")
            else:
                logger.error(f"[Executor] Order error: {e}")
            return False

    def get_redeemable_positions(self) -> list[dict]:
        """
        Fetch posisi yang sudah menang dan siap di-claim.
        """
        if not self._initialized or not self._funder:
            return []
        try:
            resp = requests.get(
                f"{DATA_BASE_URL}/positions",
                params={
                    "user":       self._funder,
                    "redeemable": "true",
                    "sizeThreshold": "0.01",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as e:
            logger.debug(f"[Executor] get_redeemable_positions error: {e}")
            return []

    def claim_position(self, condition_id: str) -> bool:
        """Claim satu posisi via relayer."""
        if not self._relayer:
            logger.warning("[Executor] Relayer tidak tersedia untuk claim")
            return False
        return self._relayer.redeem_positions(condition_id)
