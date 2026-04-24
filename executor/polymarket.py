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

Changelog:
  - signature_type=1 (POLY_PROXY) — fix utama untuk balance $0
  - Balance parsing fix: dibagi 1_000_000 (USDC Polygon = 6 desimal)
  - Balance fallback via on-chain web3 jika CLOB API gagal
  - Error logging lebih detail (tidak silent lagi)
  - FIX tick_size error: ganti create_and_post_order → create_order + post_order
    dengan tick_size eksplisit "0.01" di PartialCreateOrderOptions.
    Ini bypass get_tick_size() internal di py-clob-client yang mengembalikan
    str bukan TickSize object → AttributeError: 'str' object has no attribute 'tick_size'
"""

import logging
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Polymarket API endpoints
CLOB_BASE_URL  = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_BASE_URL  = "https://data-api.polymarket.com"

# USDC contract di Polygon (untuk on-chain fallback)
USDC_POLYGON   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS  = 6

# Polygon RPC untuk fallback on-chain balance
POLYGON_RPCS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://matic-mainnet.chainstacklabs.com",
]

# Tick size Polymarket
POLYMARKET_TICK     = Decimal("0.01")
POLYMARKET_TICK_STR = "0.01"


class PolymarketRelayer:
    """Handle gasless transactions via Polymarket Relayer."""

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
        if not self.is_available():
            return False
        try:
            payload = {"conditionId": condition_id, "funder": self._funder}
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
    """Eksekutor utama untuk semua operasi Polymarket."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.balance: float = 0.0
        self._client = None
        self._relayer: Optional[PolymarketRelayer] = None
        self._initialized = False
        self._market_cache:    dict = {}
        self._market_cache_ts: dict = {}
        self._odds_cache: dict = {}
        self._odds_cache_time: float = 0.0

        # Deteksi versi py-clob-client saat init
        self._has_partial_options = False
        self._has_create_order    = False

        self._private_key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self._funder       = os.getenv("POLYMARKET_FUNDER", "")
        self._api_key      = os.getenv("POLYMARKET_API_KEY", "")
        self._api_secret   = os.getenv("POLYMARKET_API_SECRET", "")
        self._api_pass     = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        self._relayer_key  = os.getenv("RELAYER_API_KEY", "")
        self._relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS", "")

        self._init()

    def _init(self) -> None:
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
                chain_id=137,
                creds=creds,
                funder=self._funder,
                signature_type=1,
            )

            # Probe versi API yang tersedia
            try:
                from py_clob_client.clob_types import PartialCreateOrderOptions
                self._has_partial_options = True
            except ImportError:
                self._has_partial_options = False
                logger.warning("[Executor] PartialCreateOrderOptions tidak tersedia — versi py-clob-client lama")

            # (flags _has_partial_options/_has_create_order tidak dipakai di place_order baru)
            self._has_create_order = callable(getattr(self._client, "create_order", None))

            if self._relayer_key and self._relayer_addr:
                self._relayer = PolymarketRelayer(
                    api_key=self._relayer_key,
                    api_key_address=self._relayer_addr,
                    private_key=self._private_key,
                    funder=self._funder,
                )

            self._initialized = True
            logger.info(
                f"[Executor] CLOB client initialized (sig_type=1 POLY_PROXY) | "
                f"create_order={self._has_create_order}"
            )

        except ImportError:
            logger.error("[Executor] py-clob-client tidak terinstall. Run: pip install py-clob-client")
        except Exception as e:
            logger.error(f"[Executor] Init error: {e}")

    # ── Balance ───────────────────────────────────────────────

    def get_balance(self) -> float:
        if self._initialized and self._client:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                resp   = self._client.get_balance_allowance(params)

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
                    raw_float = float(raw_balance)
                    self.balance = raw_float / 1_000_000 if raw_float > 1_000 else raw_float
                    logger.debug(f"[Executor] Balance CLOB: raw={raw_float} → ${self.balance:.2f}")
                    return self.balance

                logger.warning(f"[Executor] Balance response tidak terduga: {resp}")

            except Exception as e:
                err_str = str(e).lower()
                if "401" in err_str or "unauthorized" in err_str:
                    logger.error(
                        "[Executor] ⚠️  AUTH ERROR saat get_balance — API key invalid atau expired.\n"
                        "           Jalankan: python regen_creds.py"
                    )
                else:
                    logger.warning(f"[Executor] get_balance CLOB error: {type(e).__name__}: {e}")

        # Fallback: on-chain USDC balance di Polygon
        if self._funder:
            try:
                from web3 import Web3
                USDC_ABI = [{"inputs": [{"name": "account", "type": "address"}],
                             "name": "balanceOf",
                             "outputs": [{"name": "", "type": "uint256"}],
                             "stateMutability": "view", "type": "function"}]

                for rpc in POLYGON_RPCS:
                    try:
                        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
                        if not w3.is_connected():
                            continue
                        contract = w3.eth.contract(
                            address=Web3.to_checksum_address(USDC_POLYGON),
                            abi=USDC_ABI,
                        )
                        raw = contract.functions.balanceOf(
                            Web3.to_checksum_address(self._funder)
                        ).call()
                        self.balance = raw / (10 ** USDC_DECIMALS)
                        logger.info(f"[Executor] Balance (on-chain fallback): ${self.balance:.2f}")
                        return self.balance
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"[Executor] on-chain balance error: {e}")

        return self.balance

    # ── Market fetching ───────────────────────────────────────

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
        return self.get_active_market("BTC", force_refresh)

    def get_active_market(self, coin: str = "BTC", force_refresh: bool = False) -> Optional[dict]:
        coin = coin.upper()
        now  = time.time()

        if (not force_refresh
                and coin in self._market_cache
                and (now - self._market_cache_ts.get(coin, 0)) < 30):
            return self._market_cache[coin]

        result = (
            self._fetch_market_via_events(coin)
            or self._fetch_market_via_clob(coin)
            or self._fetch_market_via_search(coin)
        )

        if result:
            self._market_cache[coin]    = result
            self._market_cache_ts[coin] = now
            logger.info(f"[Executor] Market {coin} found: {result.get('question','')[:60]}")

        return result or self._market_cache.get(coin)

    def _fetch_market_via_events(self, coin: str) -> Optional[dict]:
        slug_prefix = f"{coin.lower()}-updown-5m"
        try:
            resp = requests.get(
                f"{GAMMA_BASE_URL}/events",
                params={"active": "true", "limit": "50", "order": "createdAt", "ascending": "false"},
                timeout=8,
            )
            if resp.status_code != 200:
                return None
            events = resp.json()
            if not isinstance(events, list):
                return None
            for ev in events:
                ev_slug = ev.get("slug", "").lower()
                if not ev_slug.startswith(slug_prefix):
                    continue
                markets = ev.get("markets", [])
                if not markets:
                    continue
                m = markets[0]
                if not m.get("acceptingOrders", False):
                    continue
                result = self._parse_market_dict(m, coin)
                if result:
                    result["question"] = ev.get("title", result["question"])
                    return result
        except Exception as e:
            logger.debug(f"[Executor] _fetch_market_via_events({coin}): {e}")
        return None

    def _fetch_market_via_clob(self, coin: str) -> Optional[dict]:
        kw_list = self.COIN_QUESTION_KW.get(coin, [coin.lower()])
        try:
            resp = requests.get(
                f"{CLOB_BASE_URL}/markets",
                params={"active": "true", "closed": "false", "limit": "100"},
                timeout=5,
            )
            if resp.status_code != 200:
                return None
            data    = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            for m in markets:
                q    = m.get("question", "").lower()
                slug = m.get("market_slug", "").lower()
                coin_match = any(k in q or k in slug for k in kw_list)
                time_match = ("5 min" in q or "5-min" in q or "updown" in slug or "5m" in slug)
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
        kw_list = self.COIN_QUESTION_KW.get(coin, [coin.lower()])
        try:
            for kw in kw_list[:1]:
                resp = requests.get(
                    f"{GAMMA_BASE_URL}/markets",
                    params={"search": f"{kw} up or down 5", "active": "true", "limit": "10"},
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

    def _parse_market_dict(self, m: dict, coin: str) -> Optional[dict]:
        token_up = token_down = None

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

        if len(clob_ids) >= 2 and len(outcomes) >= 2:
            for i, out in enumerate(outcomes):
                out_str = str(out).strip().upper()
                if out_str in ("UP", "HIGHER", "YES") and i < len(clob_ids):
                    token_up   = clob_ids[i]
                elif out_str in ("DOWN", "LOWER", "NO") and i < len(clob_ids):
                    token_down = clob_ids[i]
        elif len(clob_ids) >= 2:
            token_up   = clob_ids[0]
            token_down = clob_ids[1]

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

    # ── Odds ──────────────────────────────────────────────────

    def get_odds(self, market: dict) -> tuple:
        now       = time.time()
        cache_key = market.get("market_id", "")
        if cache_key and (now - self._odds_cache_time) < 3:
            cached = self._odds_cache.get(cache_key)
            if cached:
                return cached

        # Cara 1: Gamma outcomePrices
        try:
            cond_id = market.get("market_id", "")
            resp    = requests.get(
                f"{GAMMA_BASE_URL}/markets",
                params={"conditionId": cond_id},
                timeout=4,
            )
            if resp.status_code == 200:
                import json as _json
                data    = resp.json()
                markets = data if isinstance(data, list) else [data]
                target  = next((m for m in markets if m.get("conditionId") == cond_id), None)
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
                            if cache_key:
                                self._odds_cache[cache_key] = result
                                self._odds_cache_time = now
                            return result
        except Exception as e:
            logger.debug(f"[Executor] get_odds gamma error: {e}")

        # Cara 2: CLOB midpoints
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
                    if cache_key:
                        self._odds_cache[cache_key] = result
                        self._odds_cache_time = now
                    return result
        except Exception as e:
            logger.debug(f"[Executor] get_odds midpoints error: {e}")

        # Cara 3: CLOB order book
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

            result = (best_ask(token_up), best_ask(token_down))
            if cache_key:
                self._odds_cache[cache_key] = result
                self._odds_cache_time = now
            return result
        except Exception as e:
            logger.debug(f"[Executor] get_odds book error: {e}")

        return (0.5, 0.5)

    # ── Order placement ───────────────────────────────────────

    def _round_amounts(self, amount: float, price: float):
        """
        Hitung (price_dec, size_dec, maker_dec) dengan presisi Polymarket API.
        Return tuple atau None jika invalid.
        """
        TICK_2 = Decimal("0.01")
        TICK_4 = Decimal("0.0001")
        price_d = Decimal(str(price)).quantize(TICK_2, rounding=ROUND_HALF_UP)
        if price_d <= 0 or price_d >= 1:
            return None
        size_d  = (Decimal(str(amount)) / price_d).quantize(TICK_4, rounding=ROUND_DOWN)
        maker_d = (size_d * price_d).quantize(TICK_2, rounding=ROUND_DOWN)
        if size_d <= 0 or maker_d <= 0:
            return None
        return float(price_d), float(size_d), float(maker_d)

    def place_order(
        self,
        token_id:   str,
        amount:     float,
        side:       str,
        price:      float,
        direction:  str,
    ) -> bool:
        """
        Submit order FOK ke Polymarket.

        Root cause error 'invalid amounts' (analisa final):
          create_order() pakai get_order_amounts():
            maker = round(size * price, ...)  → presisi error, misal 1.994655
          to_token_decimals(1.994655) = 1994655 → API baca 1994655/1e6=1.994655 → >2 desimal

          create_market_order() pakai get_market_order_amounts() untuk BUY:
            maker = round_down(amount, 2)  → selalu tepat 2 desimal (misal 2.00)
            taker = round(maker / price, 4)
          to_token_decimals(2.00) = 2000000 → API baca 2.00 ✓

        Solusi: gunakan create_market_order() + MarketOrderArgs, bukan create_order.
        """
        if self.dry_run:
            logger.info(f"[Executor] DRY_RUN: {direction} ${amount:.2f} @ {price:.4f}")
            return True

        if not self._initialized or not self._client:
            logger.error("[Executor] Client belum initialized")
            return False

        # Round price & amount ke 2 desimal
        TICK_2     = Decimal("0.01")
        price_d    = Decimal(str(price)).quantize(TICK_2, rounding=ROUND_HALF_UP)
        amount_d   = Decimal(str(amount)).quantize(TICK_2, rounding=ROUND_DOWN)
        price_dec  = float(price_d)
        amount_dec = float(amount_d)

        if price_d <= 0 or price_d >= 1:
            logger.warning(f"[Executor] Price out of range: {price_d}")
            return False
        if amount_d <= 0:
            logger.warning(f"[Executor] Amount <= 0")
            return False

        logger.info(f"[Executor] place_order {direction}: amount={amount_dec} @ price={price_dec}")

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions

            # Gunakan create_market_order() — ini satu-satunya cara yang menghasilkan
            # maker tepat 2 desimal karena get_market_order_amounts BUY melakukan:
            #   maker = round_down(amount, 2)  → tepat 2 desimal
            #   taker = round(maker / price, 4) → tepat 4 desimal
            market_args = MarketOrderArgs(
                token_id=token_id,
                price=price_dec,
                amount=amount_dec,
                side=side,
            )

            options = PartialCreateOrderOptions(
                tick_size=POLYMARKET_TICK_STR,
                neg_risk=False,
            )

            signed = self._client.create_market_order(market_args, options)

            if signed is None:
                logger.error("[Executor] create_market_order mengembalikan None")
                return False

            # Log amount yang akan dikirim (dari signed.dict())
            order_dict = signed.dict() if (hasattr(signed, "dict") and callable(signed.dict)) else {}
            logger.info(
                f"[Executor] Signed: maker={order_dict.get('makerAmount')} "
                f"taker={order_dict.get('takerAmount')}"
            )

            # POST via post_order — amount sudah benar dari create_market_order
            resp = self._client.post_order(signed, OrderType.FOK)
            return self._eval_resp(resp, direction, amount_dec, price_dec)

        except Exception as e:
            self._log_order_err(e, direction)
            return False


    def _eval_resp(self, resp, direction: str, amount: float, price_dec: float) -> bool:
        if resp and resp.get("status") in ("matched", "filled", "MATCHED"):
            logger.info(f"[Executor] Order FILLED: {direction} ${amount:.2f} @ {price_dec:.4f}")
            return True
        status = resp.get("status", "unknown") if resp else "no response"
        logger.warning(f"[Executor] Order NOT filled: status={status} | {direction}")
        return False

    def _log_order_err(self, e: Exception, direction: str) -> None:
        err = str(e).lower()
        if "no match" in err:
            logger.warning(f"[Executor] No counterparty untuk {direction}")
        elif "401" in err or "unauthorized" in err:
            logger.error(f"[Executor] Auth error saat place_order: {e}")
        elif "tick_size" in err:
            logger.error(
                f"[Executor] tick_size error masih terjadi: {e}\n"
                f"           Coba: pip install py-clob-client --upgrade"
            )
        else:
            logger.error(f"[Executor] Order error [{direction}]: {type(e).__name__}: {e}")

    # ── Positions & claim ─────────────────────────────────────

    def get_redeemable_positions(self) -> list:
        if not self._initialized or not self._funder:
            return []
        try:
            resp = requests.get(
                f"{DATA_BASE_URL}/positions",
                params={"user": self._funder, "redeemable": "true", "sizeThreshold": "0.01"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as e:
            logger.debug(f"[Executor] get_redeemable_positions error: {e}")
            return []

    def claim_position(self, condition_id: str) -> bool:
        if not self._relayer:
            logger.warning("[Executor] Relayer tidak tersedia untuk claim")
            return False
        return self._relayer.redeem_positions(condition_id)
