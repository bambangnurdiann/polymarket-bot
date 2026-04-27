"""
bot_late.py
===========
Late Bot Polymarket — Strategi Asli (Simple & Focused)

STRATEGI:
  - Entry window: 7–30 detik TERAKHIR sebelum close
  - F1 Time Check : 7s <= remaining <= 30s
  - F2 Beat Distance: |price - beat| >= $25, arah dari selisih
  - Polling: 2s saat remaining > 35s, 200ms saat remaining <= 35s
  - 1 bet per window, no re-entry

PRICE SOURCE:
  - Beat price  : Polymarket Gamma API (strike_price field)
  - Current price: Chainlink oracle (via ChainlinkMonitor) — SAMA dengan
                   harga yang Polymarket tampilkan di UI
  - Fallback    : Hyperliquid hanya kalau Chainlink tidak tersedia
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from dotenv import load_dotenv
load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("logs/late_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

from utils.colors import green, red, yellow, cyan, bold, dim, clear_screen
from utils.telegram_controller import TelegramController, CommandHandler
from fetcher.multi_ws import MultiWS
from fetcher.chainlink_monitor import ChainlinkMonitor
from engine.result_tracker import ResultTracker
from engine.circuit_breaker import CircuitBreaker
from executor.polymarket import PolymarketExecutor

# ── Config ────────────────────────────────────────────────────
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACTIVE_COINS   = [c.strip().upper() for c in os.getenv("ACTIVE_COINS", "BTC").split(",") if c.strip()]
AUTO_REDEEM    = os.getenv("AUTO_REDEEM_ENABLED", "true").lower() == "true"
CLAIM_INTERVAL = int(os.getenv("CLAIM_CHECK_INTERVAL", "90"))

# ── Strategi Parameter ────────────────────────────────────────
ENTRY_MIN_REM  = float(os.getenv("ENTRY_MIN_REM", "7"))    # minimal sisa detik
ENTRY_MAX_REM  = float(os.getenv("ENTRY_MAX_REM", "30"))   # maksimal sisa detik
BEAT_DISTANCE  = float(os.getenv("BEAT_DISTANCE", "25"))   # jarak min dari beat ($)
MIN_ODDS       = float(os.getenv("MIN_ODDS", "0.45"))       # odds minimum

# ── Chainlink config ──────────────────────────────────────────
CL_ENABLED = os.getenv("CHAINLINK_ARB_ENABLED", "true").lower() == "true"

# ── Circuit breaker ───────────────────────────────────────────
CB_MAX_STREAK    = int(os.getenv("CB_MAX_STREAK", "5"))
CB_HARD_STOP     = int(os.getenv("CB_HARD_STOP_STREAK", "7"))
CB_SESSION_LIMIT = int(os.getenv("CB_SESSION_MAX_LOSS", "20"))
CB_MAX_DRAWDOWN  = float(os.getenv("CB_MAX_DRAWDOWN", "0.70"))

# ── Session block ─────────────────────────────────────────────
def is_session_blocked() -> tuple:
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M")

    def to_min(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    nm     = to_min(now_str)
    blocks = []

    raw = os.getenv("SESSION_BLOCKS", "")
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if len(part) > 5 and "-" in part[5:]:
                times = part.rsplit("-", 1)
                if len(times) == 2:
                    blocks.append((times[0].strip(), times[1].strip()))

    for start, end in blocks:
        try:
            sm = to_min(start)
            em = to_min(end)
            blocked = (sm <= nm <= em) if sm <= em else (nm >= sm or nm <= em)
            if blocked:
                return True, f"SESSION BLOCK: {start}–{end} UTC"
        except Exception:
            continue

    return False, ""


# ── Window tracker ────────────────────────────────────────────
class WindowState:
    """State untuk satu window 5 menit."""
    WINDOW_DURATION = 300

    def __init__(self):
        self.window_id:    str   = ""
        self.window_start: float = 0.0
        self.window_end:   float = 0.0
        self.beat_price:   Optional[float] = None
        self.bet_done:     bool  = False
        self.update()

    def update(self):
        now          = time.time()
        window_start = (now // self.WINDOW_DURATION) * self.WINDOW_DURATION
        window_end   = window_start + self.WINDOW_DURATION

        dt        = datetime.fromtimestamp(window_start, tz=timezone.utc)
        window_id = dt.strftime("%Y%m%d-%H%M")

        if window_id != self.window_id:
            # Window baru — reset state
            self.window_id    = window_id
            self.window_start = window_start
            self.window_end   = window_end
            self.beat_price   = None
            self.bet_done     = False
            logger.info(f"[Window] Baru: {window_id}")

        self.window_start = window_start
        self.window_end   = window_end

    @property
    def remaining(self) -> float:
        return max(0.0, self.window_end - time.time())

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.window_start)


# ── Bot State ─────────────────────────────────────────────────
class BotState:
    def __init__(self, bet_amount: float, starting_balance: float = 0.0):
        self.bet_amount       = bet_amount
        self.auto_bet         = True
        self.uptime_start     = time.time()
        self.last_claim_check = 0.0
        self.total_claimed    = 0
        self.stop_requested   = False
        self.manual_bet: Optional[tuple] = None
        self._last_low_balance_warn = 0.0

        self.tg = TelegramController()
        self.circuit_breaker = CircuitBreaker(
            max_streak=CB_MAX_STREAK,
            hard_stop_streak=CB_HARD_STOP,
            session_max_loss=CB_SESSION_LIMIT,
            max_drawdown_pct=CB_MAX_DRAWDOWN,
            starting_balance=starting_balance,
        )
        self.circuit_breaker.set_telegram_callback(self.tg.send)


# ── Dashboard ─────────────────────────────────────────────────
def render_dashboard(
    state:    BotState,
    windows:  Dict[str, WindowState],
    mws:      MultiWS,
    results:  ResultTracker,
    executor: PolymarketExecutor,
    cl_monitor,
) -> None:
    clear_screen()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime  = int(time.time() - state.uptime_start)
    up_str  = f"{uptime//3600}h {(uptime%3600)//60}m {uptime%60}s"
    mode_c  = red if not DRY_RUN else yellow
    ws_c    = green if mws.status == "OK" else red
    W       = 68

    def sep(char="-"): print("  " + char * W)
    def row(txt=""): print(f"  | {txt}")

    cb      = state.circuit_breaker
    cb_ok, cb_reason = cb.can_bet()
    cb_str  = green(f"CB:OK({cb.state.consecutive_losses}L)") if cb_ok \
              else red(f"CB:⛔{cb.state.consecutive_losses}L")

    blocked, blk_reason = is_session_blocked()
    sess_str = red("BLOCKED") if blocked else green("OK")

    print()
    print(f"  +{'─'*W}+")
    row(f"{bold('LATE BOT')} {mode_c('LIVE' if not DRY_RUN else 'DRY-RUN')}  "
        f"|  {now_str}  |  ⏱{up_str}")
    row(f"WS:{ws_c(mws.status)}  Auto:{'ON' if state.auto_bet else yellow('OFF')}  "
        f"{cb_str}  Session:{sess_str}  Coins:{bold(' '.join(ACTIVE_COINS))}")
    print(f"  +{'─'*W}+")

    for coin in ACTIVE_COINS:
        win   = windows.get(coin)
        data  = mws.coins.get(coin)
        if not win or not data:
            continue

        win.update()

        # Harga: prioritas Chainlink → Hyperliquid
        cl_price = cl_monitor.get_price(coin) if cl_monitor else None
        hl_price = data.get_price() if data else None
        price    = cl_price or hl_price or 0.0
        beat     = win.beat_price
        rem      = win.remaining
        elapsed  = win.elapsed

        # Source indicator
        price_src = "CL" if cl_price else ("HL" if hl_price else "N/A")
        price_c   = green if cl_price else (yellow if hl_price else red)
        price_disp = f"${price:>11,.2f}" if price else "       N/A   "

        # Beat distance & direction
        beat_str = f"${beat:,.2f}" if beat else dim("N/A")
        if beat and price:
            diff      = price - beat
            abs_diff  = abs(diff)
            direction = "UP" if diff > 0 else "DOWN"
            dist_c    = green if abs_diff >= BEAT_DISTANCE else yellow
            dist_str  = dist_c(f"Δ${abs_diff:.0f} {direction}")
        else:
            dist_str = dim("Δ N/A")

        # Zone check
        in_zone   = ENTRY_MIN_REM <= rem <= ENTRY_MAX_REM
        zone_c    = green if in_zone else (cyan if rem > ENTRY_MAX_REM else red)
        zone_lbl  = zone_c(f"rem={rem:.0f}s")

        # Progress bar (remaining)
        bar_width = 38
        pos       = int((1 - rem / 300) * bar_width)
        zone_lo   = int((1 - ENTRY_MAX_REM / 300) * bar_width)
        zone_hi   = int((1 - ENTRY_MIN_REM / 300) * bar_width)
        bar_chars = []
        for i in range(bar_width):
            if i == pos:
                bar_chars.append(green("●") if in_zone else yellow("●"))
            elif zone_lo <= i <= zone_hi:
                bar_chars.append(cyan("▪"))
            else:
                bar_chars.append(dim("─"))
        bar = dim("[") + "".join(bar_chars) + dim("]")

        # F1 & F2 status
        f1_ok = ENTRY_MIN_REM <= rem <= ENTRY_MAX_REM
        f2_ok = bool(beat and price and abs(price - beat) >= BEAT_DISTANCE)

        if win.bet_done:
            signal_lbl = green("✓ BET DONE — tunggu window berikutnya")
        elif not cb_ok:
            signal_lbl = red(f"⛔ CB: {cb_reason[:40]}")
        elif blocked:
            signal_lbl = red(f"⛔ {blk_reason[:40]}")
        elif f1_ok and f2_ok:
            d = "UP" if (price - beat) > 0 else "DOWN"
            signal_lbl = green(bold(f"▶ READY BET {d}"))
        elif not f1_ok:
            signal_lbl = dim(f"F1: menunggu rem={rem:.0f}s masuk [{ENTRY_MIN_REM:.0f}-{ENTRY_MAX_REM:.0f}]s")
        elif not beat:
            signal_lbl = yellow("Menunggu beat price dari API...")
        elif not price:
            signal_lbl = yellow("Menunggu harga dari Chainlink/Hyperliquid...")
        else:
            signal_lbl = dim(f"F2: jarak ${abs(price - beat):.0f} < ${BEAT_DISTANCE:.0f}")

        row(f"{bold(f'[{coin}]')} {price_c(price_disp)} [{price_src}]  "
            f"Beat:{yellow(beat_str)}  {dist_str}")
        row(f"  {bar} {zone_lbl}  {signal_lbl}")
        sep()

    # Results
    pnl_c = green if results.running_pnl >= 0 else red
    cb_s  = cb.state
    row(bold("RESULTS"))
    sep()
    row(f"  Saldo: {bold(f'${executor.balance:.2f}')}  "
        f"Bet:${state.bet_amount:.2f}  "
        f"PnL:{pnl_c(bold(f'${results.running_pnl:+.2f}'))}")
    row(f"  Bets:{results.total_bets}  "
        f"W:{green(str(results.wins))}  "
        f"L:{red(str(results.losses))}  "
        f"WR:{bold(f'{results.win_rate:.1f}%')}  "
        f"Streak: L{cb_s.consecutive_losses}/W{cb_s.consecutive_wins}")
    if results.current_bet:
        cb_r = results.current_bet
        d_c  = green if cb_r.direction == "UP" else red
        row(f"  {bold('⏳ ACTIVE:')} {cb_r.window_id}  "
            f"{d_c(bold(cb_r.direction))}  "
            f"${cb_r.bet_amount:.2f} @ {cb_r.odds:.4f}")
    sep()
    row(f"  {dim('[A] Toggle Auto  [Ctrl+C] Stop')}")
    print(f"  +{'─'*W}+\n")


# ── Beat price sync ───────────────────────────────────────────
async def beat_sync_loop(
    windows:  Dict[str, WindowState],
    executor: PolymarketExecutor,
    mws:      MultiWS,
    cl_monitor,
) -> None:
    """
    Loop sync beat price dari Polymarket API setiap 5 detik.
    Fallback: pakai Chainlink price di awal window jika API tidak punya strike_price.
    """
    # Catat harga Chainlink saat awal window — dipakai sebagai beat fallback
    cl_beat_snapshot: Dict[str, tuple] = {}  # coin -> (window_id, price)

    while True:
        for coin in ACTIVE_COINS:
            try:
                win = windows.get(coin)
                if not win:
                    continue

                # Force refresh market setiap loop agar selalu dapat data terbaru
                market = executor.get_active_market(coin, force_refresh=True)

                if market and win:
                    strike = market.get("strike_price")
                    if strike and strike > 0:
                        # ✅ Dapat dari API — paling akurat
                        if win.beat_price != strike:
                            win.beat_price = strike
                            logger.info(
                                f"[{coin}] ✅ Beat dari API: ${strike:,.2f} "
                                f"(window {win.window_id})"
                            )
                    else:
                        # ⚠️ API tidak punya strike_price
                        # Fallback: pakai harga Chainlink di detik pertama window
                        if cl_monitor:
                            cl_price = cl_monitor.get_price(coin)
                            if cl_price and cl_price > 0:
                                snap = cl_beat_snapshot.get(coin)
                                # Pakai snapshot hanya kalau masih window yang sama
                                if snap and snap[0] == win.window_id:
                                    # Sudah ada snapshot untuk window ini
                                    if win.beat_price is None:
                                        win.beat_price = snap[1]
                                        logger.warning(
                                            f"[{coin}] ⚠️ Beat fallback Chainlink: "
                                            f"${snap[1]:,.2f} (API tidak punya strike_price)"
                                        )
                                else:
                                    # Window baru — ambil snapshot sekarang
                                    # Hanya di 30 detik pertama window
                                    if win.elapsed <= 30:
                                        cl_beat_snapshot[coin] = (win.window_id, cl_price)
                                        if win.beat_price is None:
                                            win.beat_price = cl_price
                                            logger.warning(
                                                f"[{coin}] ⚠️ Beat snapshot Chainlink: "
                                                f"${cl_price:,.2f} t={win.elapsed:.0f}s "
                                                f"(API tidak punya strike_price)"
                                            )

                # Inject Chainlink price ke MultiWS untuk display
                if cl_monitor and mws:
                    cl_price = cl_monitor.get_price(coin)
                    if cl_price and cl_price > 0:
                        mws.inject_chainlink_price(coin, cl_price)

            except Exception as e:
                logger.debug(f"[BeatSync] {coin}: {e}")

        await asyncio.sleep(5)


# ── Claim ─────────────────────────────────────────────────────
def maybe_claim(state: BotState, executor: PolymarketExecutor) -> None:
    if not AUTO_REDEEM:
        return
    now = time.time()
    if now - state.last_claim_check < CLAIM_INTERVAL:
        return
    state.last_claim_check = now

    positions = executor.get_redeemable_positions()
    if not positions:
        return

    logger.info(f"[Claim] {len(positions)} posisi redeemable")
    claimed_count  = 0
    claimed_amount = 0.0

    for pos in positions:
        cid = pos.get("conditionId", "")
        if not cid:
            continue
        size = float(pos.get("size", pos.get("currentValue", 0)) or 0)
        ok   = executor.claim_position(cid)
        if ok:
            state.total_claimed += 1
            claimed_count       += 1
            claimed_amount      += size
        time.sleep(1)

    if claimed_count > 0:
        old_bal = executor.balance
        executor.get_balance()
        gain = executor.balance - old_bal
        state.tg.send(
            f"💰 <b>Auto-Claim</b>\n"
            f"Posisi: {claimed_count} | Saldo: +${gain:.2f}"
        )


# ── Execute bet ───────────────────────────────────────────────
def execute_bet(
    coin:      str,
    direction: str,
    price:     float,
    beat:      float,
    remaining: float,
    odds:      float,
    state:     BotState,
    windows:   Dict[str, WindowState],
    results:   ResultTracker,
    executor:  PolymarketExecutor,
) -> None:
    win = windows.get(coin)
    if not win:
        return

    # Final safety checks
    cb_ok, cb_reason = state.circuit_breaker.can_bet()
    if not cb_ok:
        logger.info(f"[Bet] Diblok CB: {cb_reason}")
        return

    blocked, _ = is_session_blocked()
    if blocked:
        return

    market = executor.get_active_market(coin, force_refresh=True)
    if not market:
        logger.warning(f"[Bet] Tidak ada market aktif untuk {coin}")
        state.tg.notify_error(f"Tidak ada market aktif untuk {coin}")
        return

    token_id = market["token_id_up"] if direction == "UP" else market["token_id_down"]

    logger.info(
        f"[Bet] {coin} {direction} ${state.bet_amount:.2f} @ {odds:.4f} "
        f"| price=${price:,.2f} beat=${beat:,.2f} Δ${abs(price-beat):.0f} "
        f"| rem={remaining:.0f}s"
    )

    ok = executor.place_order(
        token_id=token_id,
        amount=state.bet_amount,
        side="BUY",
        price=odds,
        direction=direction,
    )

    # Selalu lock window setelah attempt (baik berhasil maupun tidak)
    win.bet_done = True

    if ok:
        results.record_bet(
            window_id=win.window_id,
            direction=direction,
            bet_amount=state.bet_amount,
            odds=odds,
            beat_price=beat,
            remaining_secs=remaining,
            odds_spread=0.0,
            beat_distance=abs(price - beat),
            signal_mode="LATE",
            coin=coin,
            market_id=market.get("market_id", ""),
            beat_source="POLYMARKET_API",
            beat_reliable=True,
        )
        logger.info(f"[Bet] ✓ {coin} {direction}")
        state.tg.notify_bet(
            coin=coin,
            direction=direction,
            amount=state.bet_amount,
            odds=odds,
            beat=beat,
            price=price,
            window_id=win.window_id,
            beat_source="POLYMARKET_API",
            beat_reliable=True,
        )
    else:
        logger.warning(f"[Bet] ✗ {coin} {direction} — order gagal")
        state.tg.notify_error(f"Order FAILED: {coin} {direction}")


# ── Main loop ─────────────────────────────────────────────────
async def main_loop(
    state:      BotState,
    windows:    Dict[str, WindowState],
    mws:        MultiWS,
    results:    ResultTracker,
    executor:   PolymarketExecutor,
    cl_monitor,
) -> None:
    last_dash      = 0.0
    last_balance   = 0.0
    last_resolved  = ""
    cmd_handler    = CommandHandler(state.tg)

    # Mulai beat sync loop
    asyncio.create_task(beat_sync_loop(windows, executor, mws, cl_monitor))

    logger.info(f"[LateBot] Start — coins: {ACTIVE_COINS}")
    logger.info(f"[LateBot] Entry zone: remaining {ENTRY_MIN_REM}-{ENTRY_MAX_REM}s")
    logger.info(f"[LateBot] Beat distance min: ${BEAT_DISTANCE}")

    while True:
        now = time.time()

        # 1. Telegram commands
        cmd = state.tg.get_pending_command()
        if cmd:
            if cmd.cmd == "/resume":
                state.circuit_breaker.force_resume()
                state.auto_bet = True
                state.tg.send("▶️ Bot di-resume. Auto-bet aktif.")
            else:
                cmd_handler.process(cmd, state, results, {}, mws)

        if state.stop_requested:
            break

        # 2. Update semua window
        for coin in ACTIVE_COINS:
            windows[coin].update()

        blocked, _ = is_session_blocked()
        cb_ok, _   = state.circuit_breaker.can_bet()

        # 3. Balance refresh (setiap 30 detik)
        if now - last_balance > 30:
            executor.get_balance()
            last_balance = now
            state.circuit_breaker.check_drawdown(executor.balance)
            if executor.balance < state.bet_amount * 3 and executor.balance > 0:
                if now - state._last_low_balance_warn > 3600:
                    state.tg.notify_low_balance(executor.balance, state.bet_amount)
                    state._last_low_balance_warn = now

        # 4. Daily summary
        state.tg.maybe_send_daily_summary(executor.balance, results.running_pnl)

        # 5. Claim
        maybe_claim(state, executor)

        # 6. Resolve bet aktif
        if results.current_bet:
            cb_r     = results.current_bet
            bet_coin = getattr(cb_r, "coin", "BTC")
            bet_win  = windows.get(bet_coin, windows[ACTIVE_COINS[0]])

            is_new_window = (cb_r.window_id != bet_win.window_id)
            if is_new_window and cb_r.window_id != last_resolved:
                elapsed_new = bet_win.elapsed
                if elapsed_new >= 10:  # Tunggu minimal 10 detik di window baru
                    # Ambil harga close dari Chainlink (paling akurat)
                    close_price = None
                    if cl_monitor:
                        close_price = cl_monitor.get_price(bet_coin)
                    if not close_price:
                        data = mws.coins.get(bet_coin)
                        close_price = data.get_price() if data else None

                    if close_price:
                        market_id = getattr(cb_r, "market_id", "")
                        rec = results.resolve_bet(
                            cb_r.window_id,
                            close_price,
                            market_id=market_id,
                        )
                        if rec:
                            last_resolved = rec.window_id
                            state.circuit_breaker.record_result(rec.result, rec.pnl)
                            state.tg.notify_result(
                                coin=bet_coin,
                                direction=rec.direction,
                                result=rec.result,
                                bet_amount=rec.bet_amount,
                                payout=rec.payout,
                                pnl=rec.pnl,
                                running_pnl=results.running_pnl,
                                beat=rec.beat_price,
                                close_price=rec.close_price or close_price,
                                win_rate=results.win_rate,
                                odds=rec.odds,
                            )

        # 7. CORE BETTING LOGIC ─────────────────────────────────
        if state.auto_bet and cb_ok and not blocked:
            for coin in ACTIVE_COINS:
                win = windows[coin]

                if win.bet_done:
                    continue

                remaining = win.remaining

                # F1: Time check — hanya aktif di zona 7-30 detik terakhir
                if not (ENTRY_MIN_REM <= remaining <= ENTRY_MAX_REM):
                    continue

                # Ambil harga terkini (prioritas Chainlink)
                cl_price = cl_monitor.get_price(coin) if cl_monitor else None
                data     = mws.coins.get(coin)
                price    = cl_price or (data.get_price() if data else None)
                beat     = win.beat_price

                if not price or not beat:
                    logger.debug(
                        f"[{coin}] Skip — price={price} beat={beat}"
                    )
                    continue

                # F2: Beat distance check
                diff     = price - beat
                abs_diff = abs(diff)

                if abs_diff < BEAT_DISTANCE:
                    logger.debug(
                        f"[{coin}] F2 skip — Δ${abs_diff:.0f} < ${BEAT_DISTANCE}"
                    )
                    continue

                direction = "UP" if diff > 0 else "DOWN"

                # Odds check
                market = executor.get_active_market(coin, force_refresh=False)
                if not market:
                    continue

                up_odds, down_odds = executor.get_odds(market)
                odds = up_odds if direction == "UP" else down_odds

                if odds < MIN_ODDS:
                    logger.debug(
                        f"[{coin}] Skip — odds {odds:.3f} < min {MIN_ODDS}"
                    )
                    continue

                # ✅ Semua filter pass — execute bet
                logger.info(
                    f"[{coin}] SIGNAL: {direction} | "
                    f"price=${price:,.2f} beat=${beat:,.2f} "
                    f"Δ${abs_diff:.0f} | rem={remaining:.0f}s | odds={odds:.4f}"
                )

                execute_bet(
                    coin=coin,
                    direction=direction,
                    price=price,
                    beat=beat,
                    remaining=remaining,
                    odds=odds,
                    state=state,
                    windows=windows,
                    results=results,
                    executor=executor,
                )
                break  # 1 bet per iterasi

        # 8. Dashboard
        # Polling cepat saat mendekati zona entry
        any_near_zone = any(
            ENTRY_MIN_REM - 5 <= w.remaining <= ENTRY_MAX_REM + 5
            for w in windows.values()
        )

        if now - last_dash >= (0.2 if any_near_zone else 2.0):
            render_dashboard(state, windows, mws, results, executor, cl_monitor)
            last_dash = now

        # Sleep: 200ms saat near zone, 2s otherwise
        await asyncio.sleep(0.2 if any_near_zone else 2.0)


# ── Keyboard ──────────────────────────────────────────────────
def setup_keyboard(state: BotState) -> None:
    import threading
    if not sys.stdin.isatty():
        return

    def _listen_windows():
        import msvcrt
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getwch().upper()
                if key == 'A':
                    state.auto_bet = not state.auto_bet
                    logger.info(f"[KB] Auto: {'ON' if state.auto_bet else 'OFF'}")
            time.sleep(0.05)

    def _listen_linux():
        import tty, termios, select
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    key = sys.stdin.read(1).upper()
                    if key == 'A':
                        state.auto_bet = not state.auto_bet
                        logger.info(f"[KB] Auto: {'ON' if state.auto_bet else 'OFF'}")
                    elif key == '\x03':
                        raise KeyboardInterrupt
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    try:
        import msvcrt
        threading.Thread(target=_listen_windows, daemon=True).start()
    except ImportError:
        try:
            import tty, termios
            threading.Thread(target=_listen_linux, daemon=True).start()
        except Exception as e:
            logger.info(f"[KB] Keyboard listener tidak tersedia: {e}")


# ── Startup ───────────────────────────────────────────────────
def startup_prompt() -> float:
    import argparse
    parser = argparse.ArgumentParser(description="Late Bot Polymarket")
    parser.add_argument("--bet",     type=float, default=None)
    parser.add_argument("--live",    action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print()
    print(bold("="*60))
    print(bold("  🎯 LATE BOT POLYMARKET"))
    print(bold("="*60))
    print()
    print(f"  Coins       : {bold(', '.join(ACTIVE_COINS))}")
    print(f"  Entry zone  : remaining {ENTRY_MIN_REM:.0f}–{ENTRY_MAX_REM:.0f} detik terakhir")
    print(f"  Beat dist   : ≥ ${BEAT_DISTANCE:.0f}")
    print(f"  Min odds    : {MIN_ODDS}")
    print(f"  Auto Claim  : {'ON' if AUTO_REDEEM else 'OFF'}")
    print()

    dry = args.dry_run or DRY_RUN
    if dry:
        print(yellow("  ⚠️  DRY RUN aktif\n"))

    if args.bet is not None:
        bet = args.bet
    else:
        while True:
            try:
                bet = float(input("  Nominal bet per trade (USDC): $").strip())
                if bet > 0:
                    break
            except ValueError:
                pass
            print(red("  Masukkan angka > 0"))

    print(f"\n  Bet/trade : {bold(f'${bet:.2f} USDC')}")
    print(f"  Mode      : {red('DRY RUN') if dry else green('LIVE')}\n")

    if not dry:
        if args.live:
            print(green("  ✓ --live flag detected"))
        else:
            if input(f"  Ketik {bold('LIVE')} untuk konfirmasi: ").strip() != "LIVE":
                print(yellow("  Dibatalkan."))
                sys.exit(0)
    else:
        if not args.live and sys.stdin.isatty():
            input("  Tekan Enter untuk mulai...")

    print(green("\n  ✓ Bot dimulai!\n"))
    return bet


# ── Run ───────────────────────────────────────────────────────
async def run():
    bet      = startup_prompt()
    executor = PolymarketExecutor(dry_run=DRY_RUN)
    executor.get_balance()
    starting_balance = executor.balance

    state   = BotState(bet, starting_balance=starting_balance)
    mws     = MultiWS(ACTIVE_COINS)
    results = ResultTracker(csv_path="logs/late_bot_results.csv")

    # Init window state per coin
    windows: Dict[str, WindowState] = {
        coin: WindowState() for coin in ACTIVE_COINS
    }

    # Chainlink monitor
    cl_monitor = None
    if CL_ENABLED:
        cl_monitor = ChainlinkMonitor(coins=ACTIVE_COINS, poll_interval=2.5)
        logger.info("[LateBot] Chainlink monitor ENABLED")

    setup_keyboard(state)
    await mws.connect()
    if cl_monitor:
        await cl_monitor.start()
    await asyncio.sleep(3)

    # Pre-load market & beat price
    for coin in ACTIVE_COINS:
        market = executor.get_active_market(coin, force_refresh=True)
        if market:
            strike = market.get("strike_price")
            if strike:
                windows[coin].beat_price = strike
                logger.info(f"[{coin}] Beat price loaded dari API: ${strike:,.2f}")
            else:
                logger.warning(
                    f"[{coin}] Strike price tidak ada di market response — "
                    f"akan pakai Chainlink sebagai fallback"
                )
                # Log semua field yang ada untuk debug
                logger.info(f"[{coin}] Market fields: { {k: str(v)[:60] for k, v in market.items()} }")
        else:
            logger.warning(f"[{coin}] Market tidak ditemukan sama sekali")

    logger.info(f"[LateBot] Saldo: ${executor.balance:.2f}")

    if not DRY_RUN and executor.balance < bet:
        print(red(f"\n  ⚠️  Saldo ${executor.balance:.2f} < bet ${bet:.2f}"))

    state.tg.notify_start("Late Bot", bet, ACTIVE_COINS, DRY_RUN)

    try:
        await main_loop(state, windows, mws, results, executor, cl_monitor)
    except KeyboardInterrupt:
        pass
    finally:
        await mws.disconnect()
        if cl_monitor:
            await cl_monitor.stop()
        state.tg.notify_stop(
            results.total_bets, results.wins, results.losses, results.running_pnl
        )
        print(yellow("\n\n  Bot dihentikan."))
        print(f"  Hasil: {results.summary()}\n")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(yellow("\n  Bot dihentikan."))
