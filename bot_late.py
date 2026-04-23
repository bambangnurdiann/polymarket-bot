"""
bot_late.py
===========
Late Bot Polymarket — Multi-Coin (BTC, ETH, SOL, DOGE)

Arsitektur:
  - MultiWS      : 1 WebSocket ke Hyperliquid, fan-out ke semua coin
  - CoinEngine   : 1 per coin — jalankan 5 filter secara independen
  - SignalArbiter: pilih sinyal terkuat dari semua coin per window
  - Eksekusi     : 1 bet per window (sinyal terkuat)

Filter per coin:
  F1 — Entry zone    : elapsed 210s–290s
  F2 — Beat dist     : threshold berbeda per coin
  F3 — Liq dual      : recent(3s) + sustained(30s)
  F4 — CVD align     : cvd_2min sesuai arah sinyal
  F5 — Odds (info)   : monitoring only

Cara menjalankan:
  python bot_late.py

Kontrol:
  A → Toggle auto-bet ON/OFF
  Ctrl+C → Stop

Konfigurasi .env:
  POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER
  POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
  DRY_RUN=false
  MIN_ODDS=0.45
  LATE_ENTRY_MIN=210
  LATE_ENTRY_MAX=290
  SESSION_BLOCK_START=07:55
  SESSION_BLOCK_END=09:05
  AUTO_REDEEM_ENABLED=true
  CLAIM_CHECK_INTERVAL=90
  ACTIVE_COINS=BTC,ETH,SOL,DOGE
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

# ── Logging ───────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("logs/late_bot_live.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── Imports ───────────────────────────────────────────────────
from utils.colors import green, red, yellow, cyan, bold, dim, clear_screen
from utils.telegram_controller import TelegramController, CommandHandler
from fetcher.multi_ws import MultiWS
from fetcher.chainlink_monitor import ChainlinkMonitor
from engine.coin_engine import CoinEngine, SignalResult
from engine.signal_arbiter import SignalArbiter
from engine.result_tracker import ResultTracker
from engine.loss_analyzer import LossAnalyzer, BetContext
from executor.polymarket import PolymarketExecutor

# ── Config ────────────────────────────────────────────────────
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
MIN_ODDS       = float(os.getenv("MIN_ODDS", "0.45"))
ENTRY_MIN      = float(os.getenv("LATE_ENTRY_MIN", "210"))
ENTRY_MAX      = float(os.getenv("LATE_ENTRY_MAX", "290"))
SESSION_START  = os.getenv("SESSION_BLOCK_START", "07:55")
SESSION_END    = os.getenv("SESSION_BLOCK_END",   "09:05")
AUTO_REDEEM    = os.getenv("AUTO_REDEEM_ENABLED", "true").lower() == "true"
CLAIM_INTERVAL = int(os.getenv("CLAIM_CHECK_INTERVAL", "90"))
ACTIVE_COINS   = [c.strip().upper() for c in os.getenv("ACTIVE_COINS", "BTC,ETH,SOL,DOGE").split(",") if c.strip()]

# Chainlink Arbitrage config
CL_ENABLED     = os.getenv("CHAINLINK_ARB_ENABLED", "true").lower() == "true"
CL_MIN_EDGE    = float(os.getenv("CHAINLINK_MIN_EDGE", "0.08"))
CL_MIN_REM     = float(os.getenv("CHAINLINK_MIN_REM", "15"))
CL_MAX_REM     = float(os.getenv("CHAINLINK_MAX_REM", "270"))
CL_VOL         = float(os.getenv("CHAINLINK_VOLATILITY", "0.001"))


# ── Session Block ─────────────────────────────────────────────
def is_session_blocked() -> tuple:
    """
    Cek apakah waktu sekarang masuk dalam salah satu session block.
    Membaca langsung dari os.environ setiap call, sehingga perubahan
    via /block atau /unblock dari Telegram langsung efektif tanpa restart.
    """
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M")

    def to_min(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    nm = to_min(now_str)

    # Kumpulkan semua block dari env (dibaca fresh setiap call)
    blocks = []

    # Format baru: SESSION_BLOCKS=23:55-01:05,03:55-05:05
    raw = os.getenv("SESSION_BLOCKS", "")
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if len(part) > 5 and "-" in part[5:]:
                times = part.rsplit("-", 1)
                if len(times) == 2:
                    blocks.append((times[0].strip(), times[1].strip()))

    # Format lama: SESSION_BLOCK_START + SESSION_BLOCK_END (tetap kompatibel)
    s_start = os.getenv("SESSION_BLOCK_START", "00:00")
    s_end   = os.getenv("SESSION_BLOCK_END",   "00:01")
    if s_start and s_end and s_start != "00:00":
        blocks.append((s_start, s_end))

    for start, end in blocks:
        try:
            sm = to_min(start)
            em = to_min(end)
            # Handle overnight blocks (misal 23:55-01:05)
            blocked = (sm <= nm <= em) if sm <= em else (nm >= sm or nm <= em)
            if blocked:
                return True, f"SESSION BLOCK: {start}–{end} UTC"
        except Exception:
            continue

    return False, ""


# ── State ─────────────────────────────────────────────────────
class BotState:
    def __init__(self, bet_amount: float):
        self.bet_amount       = bet_amount
        self.auto_bet         = True
        self.last_claim_check = 0.0
        self.total_claimed    = 0
        self.uptime_start     = time.time()
        self.manual_bet: Optional[tuple] = None
        self.odds: Dict[str, tuple] = {}
        self.tg  = TelegramController()
        self._last_low_balance_warn = 0.0
        self.loss_analyzer = LossAnalyzer()
        self.stop_requested = False


# ── Dashboard ─────────────────────────────────────────────────
def render_dashboard(
    state:    BotState,
    engines:  Dict[str, CoinEngine],
    mws:      MultiWS,
    results:  ResultTracker,
    executor: PolymarketExecutor,
    signals:  Dict[str, Optional[SignalResult]],
    arbiter:  SignalArbiter,
) -> None:
    clear_screen()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime  = int(time.time() - state.uptime_start)
    up_str  = f"{uptime//3600}h {(uptime%3600)//60}m {uptime%60}s"
    mode_c  = red if not DRY_RUN else yellow
    ws_c    = green if mws.status == "OK" else red
    W = 64

    def sep(): print("  " + "-"*W)
    def line(s=""): print(f"  {s}")

    print()
    print(f"  +{'-'*W}+")
    print(f"  | {bold('LATE BOT')} {mode_c('LIVE' if not DRY_RUN else 'DRY RUN')}  |  {now_str}  |  {up_str}")
    print(f"  | WS:{ws_c(mws.status)}  Auto:{green('ON') if state.auto_bet else yellow('OFF')}  Coins:{' '.join(ACTIVE_COINS)}")
    print(f"  +{'-'*W}+")

    # Per-coin rows
    for coin in ACTIVE_COINS:
        data = mws.coins.get(coin)
        eng  = engines.get(coin)
        sig  = signals.get(coin)
        if not data or not eng:
            continue

        price     = data.price or 0
        beat      = eng.candle.beat_price
        elapsed   = eng.candle.elapsed
        remaining = eng.candle.remaining
        diff      = (price - beat) if price and beat else None
        cvd2      = data.cvd_2min
        odds_up, odds_down = state.odds.get(coin, (0.5, 0.5))

        # Signal label
        if sig and sig.should_bet:
            mode_tag  = cyan("[CL]") if sig.mode == "CHAINLINK" else ""
            sig_label = green(bold(f"▶ BET {sig.direction} str={sig.strength:.2f}")) + f" {mode_tag}"
        elif sig:
            fd    = sig.filter_details or {}
            fails = [k.upper() for k,(s,_) in fd.items() if s=="FAIL"]
            sig_label = dim(f"SKIP [{','.join(fails) or '—'}]")
        else:
            sig_label = dim("—")

        in_zone  = ENTRY_MIN <= elapsed <= ENTRY_MAX
        zone_c   = green if in_zone else dim
        diff_c   = (green if diff and diff > 0 else red) if diff else dim
        diff_str = f"{diff:+.3f}" if diff is not None else "N/A"
        beat_str = f"${beat:,.2f}" if beat else "N/A"

        print(f"  | {bold(f'[{coin}]')} ${price:>10,.2f}  CVD:{cyan(f'{cvd2/1000:+.0f}k'):<10} Sig:{sig_label}")
        print(f"  |   Liq S:${data.liq_short_3s:>6,.0f}  Liq L:${data.liq_long_3s:>6,.0f}  UP={odds_up:.2f}/DN={odds_down:.2f}")
        print(f"  |   Beat:{yellow(beat_str)}  vs:{diff_c(diff_str)}  t={zone_c(f'{elapsed:.0f}s')} rem={remaining:.0f}s")
        print(f"  |   Market: {cyan(eng.candle.get_market_name())}  [{remaining:.0f}s]")
        sep()

    # Arbiter
    print(f"  | {bold('SIGNAL ARBITER')}")
    sep()
    blocked, blk_reason = is_session_blocked()
    if arbiter.window_bet_done:
        line(f"  {green('✓ Sudah bet di window ini')}")
    elif blocked:
        line(f"  {red(blk_reason)}")
    else:
        candidates = [s for s in signals.values() if s and s.should_bet]
        if candidates:
            best = max(candidates, key=lambda s: s.strength)
            line(f"  Best: {bold(best.coin)} {green(best.direction)}  strength={best.strength:.2f}")
            if len(candidates) > 1:
                others = ", ".join(f"{s.coin}({s.strength:.2f})" for s in candidates if s.coin != best.coin)
                line(f"  Others: {dim(others)}")
        else:
            line(f"  {dim('Menunggu sinyal...')}")
    sep()

    # Results
    print(f"  | {bold('RESULTS')}")
    sep()
    pnl_c = green if results.running_pnl >= 0 else red
    line(f"  Saldo: {bold(f'${executor.balance:.2f}')}  Bet:${state.bet_amount:.2f}  PnL:{pnl_c(bold(f'${results.running_pnl:+.2f}'))}")
    line(f"  Bets:{results.total_bets} W:{results.wins} L:{results.losses} WR:{results.win_rate:.1f}%  Claimed:{state.total_claimed}")
    if results.current_bet:
        cb  = results.current_bet
        d_c = green if cb.direction == "UP" else red
        line(f"  Active: {cb.window_id}  {d_c(bold(cb.direction))}  ${cb.bet_amount:.2f} @ {cb.odds:.4f}")
    sep()
    print(f"  | {dim('[A] Auto  [Ctrl+C] Stop'):^{W}} |")
    print(f"  +{'-'*W}+\n")


# ── Odds updater ──────────────────────────────────────────────
async def odds_loop(state: BotState, engines: Dict[str, CoinEngine], executor: PolymarketExecutor) -> None:
    while True:
        for coin in ACTIVE_COINS:
            try:
                market = await asyncio.to_thread(executor.get_active_market, coin)
                if market:
                    up, down = await asyncio.to_thread(executor.get_odds, market)
                    state.odds[coin] = (up, down)
                    engines[coin].update_odds(up, down)
            except Exception as e:
                logger.debug(f"[Odds] {coin}: {e}")
        await asyncio.sleep(3)


# ── Keyboard ──────────────────────────────────────────────────
def setup_keyboard(state: BotState) -> None:
    """Cross-platform keyboard listener. Dinonaktifkan otomatis jika tidak ada TTY (VPS)."""
    import threading

    if not sys.stdin.isatty():
        logger.info("[KB] Tidak ada TTY — keyboard listener dinonaktifkan (normal di VPS)")
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
        logger.info("[KB] Windows keyboard listener aktif")
    except ImportError:
        try:
            import tty, termios
            threading.Thread(target=_listen_linux, daemon=True).start()
            logger.info("[KB] Linux keyboard listener aktif")
        except Exception as e:
            logger.info(f"[KB] Keyboard listener tidak tersedia: {e}")


# ── Claim ─────────────────────────────────────────────────────
async def maybe_claim(state: BotState, executor: PolymarketExecutor) -> None:
    if not AUTO_REDEEM:
        return
    if not executor._relayer or not executor._relayer.is_available():
        return
    now = time.time()
    if now - state.last_claim_check < CLAIM_INTERVAL:
        return
    state.last_claim_check = now
    claimed_now = 0
    positions = await asyncio.to_thread(executor.get_redeemable_positions)
    for pos in positions:
        cid = pos.get("conditionId", "")
        if cid and await asyncio.to_thread(executor.claim_position, cid):
            state.total_claimed += 1
            claimed_now += 1
        await asyncio.sleep(1)
    if claimed_now > 0:
        state.tg.notify_claim(claimed_now, state.total_claimed)


# ── Execute bet ───────────────────────────────────────────────
async def execute_bet(
    coin: str, direction: str,
    state: BotState, engines: Dict[str, CoinEngine], mws: MultiWS,
    results: ResultTracker, executor: PolymarketExecutor, arbiter: SignalArbiter,
    signal: Optional[SignalResult] = None,
) -> None:
    eng = engines.get(coin)
    if not eng:
        return
    odds_up, odds_down = state.odds.get(coin, (0.5, 0.5))
    odds      = odds_up if direction == "UP" else odds_down
    beat      = eng.candle.beat_price or 0
    data      = mws.coins.get(coin)
    price     = data.get_price() if data else 0
    remaining = eng.candle.remaining
    market    = await asyncio.to_thread(executor.get_active_market, coin, True)

    if not market:
        logger.warning(f"[Bet] Tidak ada market aktif untuk {coin}")
        state.tg.notify_error(f"Tidak ada market aktif untuk {coin}")
        return

    token_id = market["token_id_up"] if direction == "UP" else market["token_id_down"]
    logger.info(f"[Bet] {coin} {direction} ${state.bet_amount:.2f} @ {odds:.4f} beat={beat:.3f}")

    ok = await asyncio.to_thread(
        executor.place_order,
        token_id=token_id,
        amount=state.bet_amount,
        side="BUY",
        price=odds,
        direction=direction,
    )
    if ok:
        eng.mark_bet_done()
        arbiter.mark_executed()

        # Extract signal context untuk loss analyzer
        cl_edge = cl_fair_odds = cl_vol = 0.0
        sig_mode = ""
        if signal:
            sig_mode = signal.mode
            if signal.chainlink_signal:
                cl_sig       = signal.chainlink_signal
                cl_edge      = cl_sig.edge
                cl_fair_odds = cl_sig.fair_odds
                cl_vol       = cl_sig.vol_calibrated

        cvd_2min = liq_s3 = liq_l3 = liq_s30 = liq_l30 = 0.0
        if data:
            cvd_2min = data.cvd_2min
            liq_s3   = data.liq_short_3s
            liq_l3   = data.liq_long_3s
            liq_s30  = data.liq_short_30s
            liq_l30  = data.liq_long_30s

        results.record_bet(
            window_id=eng.candle.window_id,
            direction=direction,
            bet_amount=state.bet_amount,
            odds=odds,
            beat_price=beat,
            remaining_secs=remaining,
            odds_spread=abs(odds_up - odds_down),
            beat_distance=abs((price or beat) - beat),
            signal_mode=sig_mode,
            cl_edge=cl_edge,
            cl_fair_odds=cl_fair_odds,
            cl_vol=cl_vol,
            cvd_2min=cvd_2min,
            liq_short_3s=liq_s3,
            liq_long_3s=liq_l3,
            liq_short_30s=liq_s30,
            liq_long_30s=liq_l30,
            coin=coin,
        )
        logger.info(f"[Bet] ✓ {coin} {direction}")
        state.tg.notify_bet(
            coin=coin, direction=direction,
            amount=state.bet_amount, odds=odds,
            beat=beat, price=price or 0,
            window_id=eng.candle.window_id,
        )
    else:
        logger.warning(f"[Bet] ✗ {coin} {direction}")
        state.tg.notify_error(f"Order FAILED: {coin} {direction} @ {odds:.4f}")


# ── Main loop ─────────────────────────────────────────────────
async def main_loop(
    state: BotState, engines: Dict[str, CoinEngine],
    mws: MultiWS, results: ResultTracker,
    executor: PolymarketExecutor, arbiter: SignalArbiter,
) -> None:
    signals: Dict[str, Optional[SignalResult]] = {}
    last_dash     = 0.0
    last_balance  = 0.0
    last_resolved = ""

    logger.info(f"[LateBot] Main loop — coins: {ACTIVE_COINS}")
    asyncio.create_task(odds_loop(state, engines, executor))

    # Init command handler
    cmd_handler = CommandHandler(state.tg)

    # ── SINGLE unified loop ───────────────────────────────────
    while True:
        now = time.time()

        # ── 1. Proses Telegram commands ───────────────────────
        cmd = state.tg.get_pending_command()
        if cmd:
            cmd_handler.process(cmd, state, results, engines, mws)

        # Stop jika diminta via /stop dari Telegram
        if state.stop_requested:
            break

        # ── 2. Master clock dari coin pertama ─────────────────
        master = engines[ACTIVE_COINS[0]]
        master.candle.update()
        current_win = master.candle.window_id
        arbiter.reset_for_window(current_win)

        blocked, _ = is_session_blocked()

        # ── 3. Tick semua coin ────────────────────────────────
        for coin in ACTIVE_COINS:
            eng  = engines[coin]
            data = mws.coins.get(coin)
            if not data:
                signals[coin] = None
                continue
            # Pakai get_price() — otomatis fallback ke REST jika WS stale
            price = data.get_price()
            if price and eng.candle.elapsed < 5:
                eng.candle.update()
                eng.candle.set_beat_price(price)
            signals[coin] = None if blocked else eng.tick(data)

        # ── 4. Balance + low balance warning ──────────────────
        if now - last_balance > 30:
            await asyncio.to_thread(executor.get_balance)
            last_balance = now
            if executor.balance < state.bet_amount * 3 and executor.balance > 0:
                if now - state._last_low_balance_warn > 3600:  # max 1x per jam
                    state.tg.notify_low_balance(executor.balance, state.bet_amount)
                    state._last_low_balance_warn = now

        # ── 5. Daily summary ──────────────────────────────────
        state.tg.maybe_send_daily_summary(executor.balance, results.running_pnl)

        # ── 6. Claim ──────────────────────────────────────────
        await maybe_claim(state, executor)

        # ── 7. Resolve hasil bet sebelumnya ───────────────────
        if results.current_bet:
            cb       = results.current_bet
            bet_coin = getattr(cb, "coin", "BTC")
            cb_eng   = engines.get(bet_coin, master)
            if (cb_eng.candle.elapsed < 5
                    and cb.window_id != current_win
                    and cb.window_id != last_resolved):
                close_data = mws.coins.get(bet_coin) or mws.coins.get("BTC")
                cp = close_data.price if close_data else None
                if cp:
                    rec = results.resolve_bet(cb.window_id, cp)
                    if rec:
                        last_resolved = rec.window_id

                        # Feed loss analyzer
                        now_utc = datetime.now(timezone.utc)
                        ctx = BetContext(
                            timestamp=rec.timestamp,
                            window_id=rec.window_id,
                            direction=rec.direction,
                            result=rec.result,
                            bet_amount=rec.bet_amount,
                            odds=rec.odds,
                            beat_price=rec.beat_price,
                            close_price=rec.close_price or cp,
                            pnl=rec.pnl,
                            remaining_secs=rec.remaining_secs,
                            odds_spread=rec.odds_spread,
                            beat_distance=rec.beat_distance,
                            signal_mode=rec.signal_mode,
                            cl_edge=rec.cl_edge,
                            cl_fair_odds=rec.cl_fair_odds,
                            cl_vol=rec.cl_vol,
                            cvd_2min=rec.cvd_2min,
                            liq_short_3s=rec.liq_short_3s,
                            liq_long_3s=rec.liq_long_3s,
                            liq_short_30s=rec.liq_short_30s,
                            liq_long_30s=rec.liq_long_30s,
                            hour_utc=rec.hour_utc,
                        )
                        state.loss_analyzer.record(ctx)
                        if rec.result == "LOSS":
                            insight = state.loss_analyzer.get_last_loss_insight()
                            if insight:
                                state.tg.notify_loss_insight(insight)
                                streak_type, streak_count = results.current_streak
                                if (
                                    streak_type == "L"
                                    and streak_count >= 3
                                    and insight.get("risk_level") == "HIGH"
                                    and state.auto_bet
                                ):
                                    state.auto_bet = False
                                    state.tg.notify_error(
                                        "Auto-bet di-pause otomatis: HIGH risk loss pattern + loss streak >= 3. "
                                        "Review /analysis lalu /resume jika sudah siap."
                                    )
                                    logger.warning(
                                        "[RiskGuard] Auto-bet paused due to high-risk loss pattern and loss streak"
                                    )

                        # Print report setiap 20 bets
                        if results.total_bets % 20 == 0 and results.total_bets > 0:
                            state.loss_analyzer.print_report()

                        # Telegram: notif hasil bet
                        state.tg.notify_result(
                            coin=bet_coin,
                            direction=rec.direction,
                            result=rec.result,
                            pnl=rec.pnl,
                            running_pnl=results.running_pnl,
                            beat=rec.beat_price,
                            close_price=rec.close_price or cp,
                            win_rate=results.win_rate,
                        )

        # ── 8. Betting logic ──────────────────────────────────
        if not arbiter.window_bet_done:
            if state.manual_bet:
                c, d = state.manual_bet
                state.manual_bet = None
                await execute_bet(c, d, state, engines, mws, results, executor, arbiter)
            elif state.auto_bet:
                valid = [s for s in signals.values() if s and s.should_bet]
                best  = arbiter.select(valid)
                if best:
                    logger.info(f"[Arbiter] {best.coin} {best.direction} str={best.strength:.2f}")
                    await execute_bet(best.coin, best.direction, state, engines, mws, results, executor, arbiter, signal=best)

        # ── 9. Dashboard ──────────────────────────────────────
        any_zone = any(ENTRY_MIN - 10 <= e.candle.elapsed <= ENTRY_MAX + 10 for e in engines.values())
        if now - last_dash >= (0.5 if any_zone else 2.0):
            render_dashboard(state, engines, mws, results, executor, signals, arbiter)
            last_dash = now

        await asyncio.sleep(0.3)


# ── Startup ───────────────────────────────────────────────────
def startup_prompt() -> float:
    """
    Startup prompt dengan dukungan CLI args untuk VPS non-interaktif.
    Usage:
      python bot_late.py --bet 5 --live
      python bot_late.py --bet 2 --dry-run
    """
    import argparse
    parser = argparse.ArgumentParser(description="Late Bot Polymarket Multi-Coin")
    parser.add_argument("--bet",     type=float, default=None,  help="Nominal bet per trade (USDC)")
    parser.add_argument("--live",    action="store_true",        help="Langsung start tanpa prompt")
    parser.add_argument("--dry-run", action="store_true",        help="Override ke dry run mode")
    args = parser.parse_args()

    print()
    print(bold("="*64))
    print(bold("  🎯 LATE BOT POLYMARKET — MULTI COIN"))
    print(bold("="*64))
    print()
    print(f"  Coins      : {bold(', '.join(ACTIVE_COINS))}")
    print(f"  Entry zone : t={ENTRY_MIN:.0f}–{ENTRY_MAX:.0f}s (sinyal terkuat menang)")
    print(f"  Session blk: {SESSION_START}–{SESSION_END} UTC")
    print(f"  Auto Claim : {'ON' if AUTO_REDEEM else 'OFF'}")
    print()

    dry = args.dry_run or DRY_RUN
    if dry:
        print(yellow("  ⚠️  DRY RUN aktif\n"))

    # Bet amount
    if args.bet is not None:
        bet = args.bet
        print(f"  Bet/trade  : {bold(f'${bet:.2f} USDC')} (dari --bet)\n")
    else:
        while True:
            try:
                bet = float(input("  Nominal bet per trade (USDC): $").strip())
                if bet > 0:
                    break
            except ValueError:
                pass
            print(red("  Masukkan angka > 0"))

    print(f"  Bet/trade : {bold(f'${bet:.2f} USDC')}")
    print(f"  Mode      : {red('DRY RUN') if dry else green('LIVE TRADING')}\n")

    if not dry:
        if args.live:
            print(green("  ✓ --live flag detected — langsung start"))
        else:
            if input(f"  Ketik {bold('LIVE')} untuk konfirmasi: ").strip() != "LIVE":
                print(yellow("  Dibatalkan."))
                sys.exit(0)
    else:
        if not args.live and sys.stdin.isatty():
            input("  Tekan Enter untuk mulai...")

    print(green("\n  ✓ Multi-Coin Late Bot dimulai!\n"))
    return bet


async def run():
    bet      = startup_prompt()
    state    = BotState(bet)
    mws      = MultiWS(ACTIVE_COINS)
    arbiter  = SignalArbiter(min_strength=0.2)
    results  = ResultTracker(csv_path="logs/late_bot_results.csv")
    executor = PolymarketExecutor(dry_run=DRY_RUN)

    # Init Chainlink Monitor (F0 strategy)
    cl_monitor = None
    if CL_ENABLED:
        cl_monitor = ChainlinkMonitor(coins=ACTIVE_COINS, poll_interval=2.5)
        logger.info(f"[LateBot] Chainlink Arb ENABLED — min_edge={CL_MIN_EDGE}")
    else:
        logger.info("[LateBot] Chainlink Arb DISABLED")

    # Buat CoinEngine dengan chainlink monitor
    engines = {
        coin: CoinEngine(
            coin,
            entry_min=ENTRY_MIN,
            entry_max=ENTRY_MAX,
            min_odds=MIN_ODDS,
            chainlink_monitor=cl_monitor,
            cl_min_edge=CL_MIN_EDGE,
            cl_min_remaining=CL_MIN_REM,
            cl_max_remaining=CL_MAX_REM,
        )
        for coin in ACTIVE_COINS
    }

    setup_keyboard(state)
    await mws.connect()
    if cl_monitor:
        await cl_monitor.start()
    logger.info("[LateBot] Connecting...")
    await asyncio.sleep(3)

    await asyncio.to_thread(executor.get_balance)
    logger.info(f"[LateBot] Saldo: ${executor.balance:.2f}")
    if not DRY_RUN and executor.balance < bet:
        print(red(f"\n  ⚠️  Saldo ${executor.balance:.2f} < bet ${bet:.2f}"))

    state.tg.notify_start("Late Bot Multi-Coin", bet, ACTIVE_COINS, DRY_RUN)

    try:
        await main_loop(state, engines, mws, results, executor, arbiter)
    except KeyboardInterrupt:
        pass
    finally:
        await mws.disconnect()
        if cl_monitor:
            await cl_monitor.stop()
        state.tg.notify_stop(results.total_bets, results.wins, results.losses, results.running_pnl)
        state.tg.stop()
        print(yellow("\n\n  Late Bot dihentikan."))
        print(f"  Hasil: {results.summary()}\n")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(yellow("\n  Bot dihentikan."))
