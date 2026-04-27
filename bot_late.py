"""
bot_late.py
===========
Late Bot Polymarket — Beat Price HANYA dari Scraper (Selenium UI)

STRATEGI:
  - Entry window: 7-30 detik TERAKHIR sebelum close
  - F1 Time Check : 7s <= remaining <= 30s
  - F2 Beat Distance: |price - beat| >= threshold, arah dari selisih
  - 1 bet per window, no re-entry
  - Beat price 100% dari UI Polymarket via Selenium scraper

PERUBAHAN dari versi sebelumnya:
  - Hapus PrevWindowResolver (tidak dipakai)
  - Hapus Chainlink monitor untuk beat price
  - Hapus semua fallback Gamma API / PREV_WINDOW untuk beat
  - Fix freeze: semua blocking call pakai asyncio.wait_for + run_in_executor
  - Fix freeze: maybe_claim dijalankan di executor, bukan blocking di loop
  - Harga live (bukan beat) tetap diambil dari MultiWS (WebSocket)
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
from fetcher.polymarket_scraper import ScraperBeatSource
from engine.result_tracker import ResultTracker
from engine.circuit_breaker import CircuitBreaker
from executor.polymarket import PolymarketExecutor

# ── Config ────────────────────────────────────────────────────
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
ACTIVE_COINS   = [c.strip().upper() for c in os.getenv("ACTIVE_COINS", "BTC").split(",") if c.strip()]
AUTO_REDEEM    = os.getenv("AUTO_REDEEM_ENABLED", "true").lower() == "true"
CLAIM_INTERVAL = int(os.getenv("CLAIM_CHECK_INTERVAL", "90"))

# ── Strategi Parameter ────────────────────────────────────────
ENTRY_MIN_REM  = float(os.getenv("LATE_ENTRY_MIN", os.getenv("ENTRY_MIN_REM", "7")))
ENTRY_MAX_REM  = float(os.getenv("LATE_ENTRY_MAX", os.getenv("ENTRY_MAX_REM", "30")))
BEAT_DISTANCE  = float(os.getenv("LATE_BEAT_DISTANCE", os.getenv("BEAT_DISTANCE", "25")))
MIN_ODDS       = float(os.getenv("MIN_ODDS", "0.45"))

# ── Scraper config ────────────────────────────────────────────
SCRAPER_RETRY_INTERVAL = float(os.getenv("SCRAPER_RETRY_INTERVAL", "45"))
SCRAPER_TIMEOUT        = float(os.getenv("SCRAPER_TIMEOUT", "30"))   # timeout per scrape call

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
    WINDOW_DURATION = 300

    def __init__(self):
        self.window_id:    str   = ""
        self.window_start: float = 0.0
        self.window_end:   float = 0.0
        self.beat_price:   Optional[float] = None
        self.beat_source:  str   = "UNKNOWN"
        self.bet_done:     bool  = False
        self._prev_id:     str   = ""
        self.update()

    def update(self):
        now          = time.time()
        window_start = (now // self.WINDOW_DURATION) * self.WINDOW_DURATION
        window_end   = window_start + self.WINDOW_DURATION

        dt        = datetime.fromtimestamp(window_start, tz=timezone.utc)
        window_id = dt.strftime("%Y%m%d-%H%M")

        if window_id != self.window_id:
            self._prev_id     = self.window_id
            self.window_id    = window_id
            self.window_start = window_start
            self.window_end   = window_end
            self.beat_price   = None
            self.beat_source  = "UNKNOWN"
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

    def set_beat(self, price: float) -> bool:
        """Set beat price dari scraper. Returns True jika berubah."""
        if price and price != self.beat_price:
            old = self.beat_price
            self.beat_price  = price
            self.beat_source = "SCRAPER"
            if old and abs(price - old) > 1:
                logger.info(f"[Window] Beat updated: ${old:,.2f} → ${price:,.2f}")
            return True
        return False


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
    state:          BotState,
    windows:        Dict[str, WindowState],
    mws:            MultiWS,
    results:        ResultTracker,
    executor:       PolymarketExecutor,
    scraper_source: ScraperBeatSource,
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
    sess_str   = red("BLOCKED") if blocked else green("OK")
    s_status   = scraper_source.status
    scraper_str = f"  Scraper:{green(s_status) if 'OK' in s_status else yellow(s_status)}"

    print()
    print(f"  +{'─'*W}+")
    row(f"{bold('LATE BOT')} {mode_c('LIVE' if not DRY_RUN else 'DRY-RUN')}  "
        f"|  {now_str}  |  ⏱{up_str}")
    row(f"WS:{ws_c(mws.status)}  Auto:{'ON' if state.auto_bet else yellow('OFF')}  "
        f"{cb_str}  Session:{sess_str}  Coins:{bold(' '.join(ACTIVE_COINS))}"
        f"{scraper_str}")
    print(f"  +{'─'*W}+")

    for coin in ACTIVE_COINS:
        win  = windows.get(coin)
        data = mws.coins.get(coin)
        if not win or not data:
            continue

        win.update()

        price = data.get_price() or 0.0
        beat  = win.beat_price
        rem   = win.remaining

        price_c    = green if price else red
        price_disp = f"${price:>11,.2f}" if price else "       N/A   "
        beat_str   = f"🌐${beat:,.2f}" if beat else dim("N/A (fetching...)")

        if beat and price:
            diff      = price - beat
            abs_diff  = abs(diff)
            direction = "UP" if diff > 0 else "DOWN"
            dist_c    = green if abs_diff >= BEAT_DISTANCE else yellow
            dist_str  = dist_c(f"Δ${abs_diff:.0f} {direction}")
        else:
            dist_str = dim("Δ N/A")

        in_zone  = ENTRY_MIN_REM <= rem <= ENTRY_MAX_REM
        zone_c   = green if in_zone else (cyan if rem > ENTRY_MAX_REM else red)
        zone_lbl = zone_c(f"rem={rem:.0f}s")

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

        f1_ok = ENTRY_MIN_REM <= rem <= ENTRY_MAX_REM
        f2_ok = bool(beat and price and abs(price - beat) >= BEAT_DISTANCE)

        if win.bet_done:
            signal_lbl = green("✓ BET DONE — tunggu window berikutnya")
        elif not cb_ok:
            signal_lbl = red(f"⛔ CB: {cb_reason[:40]}")
        elif blocked:
            signal_lbl = red(f"⛔ {blk_reason[:40]}")
        elif not beat:
            signal_lbl = yellow("⏳ Beat fetching via scraper...")
        elif f1_ok and f2_ok:
            d = "UP" if (price - beat) > 0 else "DOWN"
            signal_lbl = green(bold(f"▶ READY BET {d}"))
        elif not f1_ok:
            signal_lbl = dim(f"F1: menunggu rem={rem:.0f}s masuk [{ENTRY_MIN_REM:.0f}-{ENTRY_MAX_REM:.0f}]s")
        else:
            signal_lbl = dim(f"F2: jarak ${abs(price - beat):.0f} < ${BEAT_DISTANCE:.0f}") if beat else dim("F2: tunggu beat")

        row(f"{bold(f'[{coin}]')} {price_c(price_disp)} [WS]  "
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


# ── Beat sync loop ────────────────────────────────────────────
async def beat_sync_loop(
    windows:        Dict[str, WindowState],
    scraper_source: ScraperBeatSource,
) -> None:
    """
    Loop yang hanya menggunakan scraper sebagai sumber beat price.
    Berjalan setiap 5 detik. Setiap scrape call diberi timeout
    SCRAPER_TIMEOUT detik agar tidak bisa freeze event loop.
    """
    while True:
        for coin in ACTIVE_COINS:
            try:
                win = windows.get(coin)
                if not win:
                    continue

                # Beri timeout agar tidak bisa nge-hang selamanya
                try:
                    price = await asyncio.wait_for(
                        scraper_source.try_get_beat(coin),
                        timeout=SCRAPER_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[BeatSync] {coin} scraper timeout ({SCRAPER_TIMEOUT}s) — skip")
                    continue

                if price:
                    updated = win.set_beat(price)
                    if updated:
                        logger.info(
                            f"[BeatSync] 🌐 {coin} beat = ${price:,.2f} "
                            f"(window {win.window_id})"
                        )

            except Exception as e:
                logger.debug(f"[BeatSync] {coin}: {e}")

        await asyncio.sleep(5)


# ── Claim ─────────────────────────────────────────────────────
async def maybe_claim_async(
    state:    BotState,
    executor: PolymarketExecutor,
    loop:     asyncio.AbstractEventLoop,
) -> None:
    """
    Versi async dari maybe_claim — blocking I/O dijalankan
    di executor sehingga event loop tidak freeze.
    """
    if not AUTO_REDEEM:
        return
    now = time.time()
    if now - state.last_claim_check < CLAIM_INTERVAL:
        return
    state.last_claim_check = now

    def _do_claim():
        positions = executor.get_redeemable_positions()
        if not positions:
            return 0, 0.0

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

        return claimed_count, claimed_amount

    try:
        claimed_count, _ = await asyncio.wait_for(
            loop.run_in_executor(None, _do_claim),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.warning("[Claim] Timeout saat claim — lanjut")
        return
    except Exception as e:
        logger.warning(f"[Claim] Error: {e}")
        return

    if claimed_count > 0:
        old_bal = executor.balance
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, executor.get_balance),
                timeout=10,
            )
        except asyncio.TimeoutError:
            pass
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
        f"| price=${price:,.2f} beat=${beat:,.2f} [SCRAPER] Δ${abs(price-beat):.0f} "
        f"| rem={remaining:.0f}s"
    )

    ok = executor.place_order(
        token_id=token_id,
        amount=state.bet_amount,
        side="BUY",
        price=odds,
        direction=direction,
    )

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
            beat_source="SCRAPER",
            beat_reliable=True,
        )
        logger.info(f"[Bet] ✓ {coin} {direction} [SCRAPER]")
        state.tg.notify_bet(
            coin=coin,
            direction=direction,
            amount=state.bet_amount,
            odds=odds,
            beat=beat,
            price=price,
            window_id=win.window_id,
            beat_source="SCRAPER",
            beat_reliable=True,
        )
    else:
        logger.warning(f"[Bet] ✗ {coin} {direction} — order gagal")
        state.tg.notify_error(f"Order FAILED: {coin} {direction}")


# ── Main loop ─────────────────────────────────────────────────
async def main_loop(
    state:          BotState,
    windows:        Dict[str, WindowState],
    mws:            MultiWS,
    results:        ResultTracker,
    executor:       PolymarketExecutor,
    scraper_source: ScraperBeatSource,
) -> None:
    last_dash      = 0.0
    last_balance   = 0.0
    last_resolved  = ""
    cmd_handler    = CommandHandler(state.tg)
    loop           = asyncio.get_event_loop()

    # Mulai beat sync loop sebagai background task
    asyncio.create_task(beat_sync_loop(windows, scraper_source))

    logger.info(f"[LateBot] Start — coins: {ACTIVE_COINS}")
    logger.info(f"[LateBot] Entry zone: remaining {ENTRY_MIN_REM}-{ENTRY_MAX_REM}s")
    logger.info(f"[LateBot] Beat distance min: ${BEAT_DISTANCE}")
    logger.info(f"[LateBot] Beat source: SCRAPER only")
    logger.info(f"[LateBot] Scraper retry_interval={SCRAPER_RETRY_INTERVAL}s, timeout={SCRAPER_TIMEOUT}s")

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

        # 2. Update windows + deteksi window baru
        for coin in ACTIVE_COINS:
            old_id = windows[coin].window_id
            windows[coin].update()
            new_id = windows[coin].window_id

            if old_id and new_id != old_id:
                logger.info(f"[Main] {coin} window baru: {old_id} → {new_id}")
                # Invalidate scraper cache saat window baru
                scraper_source.on_new_window(coin)

        blocked, _ = is_session_blocked()
        cb_ok, _   = state.circuit_breaker.can_bet()

        # 3. Balance refresh — pakai executor agar tidak blocking
        if now - last_balance > 30:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, executor.get_balance),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                logger.warning("[Main] Balance refresh timeout — skip")
            last_balance = now
            state.circuit_breaker.check_drawdown(executor.balance)
            if executor.balance < state.bet_amount * 3 and executor.balance > 0:
                if now - state._last_low_balance_warn > 3600:
                    state.tg.notify_low_balance(executor.balance, state.bet_amount)
                    state._last_low_balance_warn = now

        # 4. Daily summary
        state.tg.maybe_send_daily_summary(executor.balance, results.running_pnl)

        # 5. Claim — non-blocking
        await maybe_claim_async(state, executor, loop)

        # 6. Resolve bet aktif
        if results.current_bet:
            cb_r     = results.current_bet
            bet_coin = getattr(cb_r, "coin", "BTC")
            bet_win  = windows.get(bet_coin, windows[ACTIVE_COINS[0]])

            is_new_window = (cb_r.window_id != bet_win.window_id)
            if is_new_window and cb_r.window_id != last_resolved:
                if bet_win.elapsed >= 10:
                    data        = mws.coins.get(bet_coin)
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

        # 7. CORE BETTING LOGIC
        if state.auto_bet and cb_ok and not blocked:
            for coin in ACTIVE_COINS:
                win = windows[coin]

                if win.bet_done:
                    continue

                remaining = win.remaining

                # F1: Time check
                if not (ENTRY_MIN_REM <= remaining <= ENTRY_MAX_REM):
                    continue

                # Harga live dari WebSocket
                data  = mws.coins.get(coin)
                price = data.get_price() if data else None
                beat  = win.beat_price

                if not price or not beat:
                    logger.debug(f"[{coin}] Skip — price={price} beat={beat}")
                    continue

                # F2: Beat distance check
                diff     = price - beat
                abs_diff = abs(diff)

                if abs_diff < BEAT_DISTANCE:
                    logger.debug(f"[{coin}] F2 skip — Δ${abs_diff:.0f} < ${BEAT_DISTANCE}")
                    continue

                direction = "UP" if diff > 0 else "DOWN"

                # Odds check
                market = executor.get_active_market(coin, force_refresh=False)
                if not market:
                    continue

                up_odds, down_odds = executor.get_odds(market)
                odds = up_odds if direction == "UP" else down_odds

                if odds < MIN_ODDS:
                    logger.debug(f"[{coin}] Skip — odds {odds:.3f} < min {MIN_ODDS}")
                    continue

                logger.info(
                    f"[{coin}] SIGNAL: {direction} | "
                    f"price=${price:,.2f} beat=${beat:,.2f} [SCRAPER] "
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
                break

        # 8. Dashboard
        any_near_zone = any(
            ENTRY_MIN_REM - 5 <= w.remaining <= ENTRY_MAX_REM + 5
            for w in windows.values()
        )

        if now - last_dash >= (0.2 if any_near_zone else 2.0):
            render_dashboard(
                state, windows, mws, results, executor, scraper_source
            )
            last_dash = now

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
    print(f"  Coins        : {bold(', '.join(ACTIVE_COINS))}")
    print(f"  Entry zone   : remaining {ENTRY_MIN_REM:.0f}–{ENTRY_MAX_REM:.0f} detik terakhir")
    print(f"  Beat dist    : ≥ ${BEAT_DISTANCE:.0f}")
    print(f"  Min odds     : {MIN_ODDS}")
    print(f"  Auto Claim   : {'ON' if AUTO_REDEEM else 'OFF'}")
    print()
    print(bold("  🌐 Beat Price: SCRAPER only (UI Polymarket)"))
    print(f"  Scraper retry  : tiap {SCRAPER_RETRY_INTERVAL:.0f}s per window")
    print(f"  Scraper timeout: {SCRAPER_TIMEOUT:.0f}s per call")
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

    # Init Scraper (satu-satunya sumber beat price)
    logger.info("[LateBot] Initializing Selenium scraper...")
    scraper_source = ScraperBeatSource(
        coins=ACTIVE_COINS,
        retry_interval=SCRAPER_RETRY_INTERVAL,
    )
    logger.info("[LateBot] Scraper initialized ✓")

    setup_keyboard(state)
    await mws.connect()
    await asyncio.sleep(3)

    # ── Pre-load beat prices via scraper ─────────────────────
    logger.info("[LateBot] Pre-loading beat prices dari scraper...")
    loop = asyncio.get_event_loop()
    for coin in ACTIVE_COINS:
        try:
            price = await asyncio.wait_for(
                scraper_source.try_get_beat(coin),
                timeout=SCRAPER_TIMEOUT,
            )
            if price:
                windows[coin].set_beat(price)
                logger.info(f"[{coin}] ✅ Pre-loaded beat: ${price:,.2f}")
            else:
                logger.warning(
                    f"[{coin}] Beat belum dapat — "
                    f"scraper retry tiap {SCRAPER_RETRY_INTERVAL:.0f}s"
                )
        except asyncio.TimeoutError:
            logger.warning(f"[{coin}] Pre-load scraper timeout — lanjut")
        except Exception as e:
            logger.warning(f"[{coin}] Pre-load error: {e}")

    logger.info(f"[LateBot] Saldo: ${executor.balance:.2f}")

    if not DRY_RUN and executor.balance < bet:
        print(red(f"\n  ⚠️  Saldo ${executor.balance:.2f} < bet ${bet:.2f}"))

    state.tg.notify_start("Late Bot", bet, ACTIVE_COINS, DRY_RUN)

    try:
        await main_loop(
            state, windows, mws, results, executor, scraper_source
        )
    except KeyboardInterrupt:
        pass
    finally:
        await mws.disconnect()
        scraper_source.stop()
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
