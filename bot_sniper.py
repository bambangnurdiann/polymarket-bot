"""
bot_sniper.py
=============
Bot Sniper Polymarket BTC 5-Menit

Strategi:
  - Masuk hanya di 30 detik terakhir window
  - Bet UP jika BTC > beat_price + $25
  - Bet DOWN jika BTC < beat_price - $25
  - Skip jika jarak < $25
  - 1 bet per window

Cara menjalankan:
  python bot_sniper.py

Kontrol keyboard (Windows):
  U → Manual bet UP
  D → Manual bet DOWN
  A → Toggle auto-bet ON/OFF
  Ctrl+C → Stop bot

Konfigurasi via .env:
  POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER
  POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
  DRY_RUN=false
  MIN_ODDS=0.45
  BEAT_DISTANCE=25   (opsional, default $25)
  SNIPE_WINDOW_MAX=30 (opsional, default 30 detik)
  SNIPE_WINDOW_MIN=7  (opsional, default 7 detik)
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

# ── Setup logging ────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("logs/sniper_live.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── Import komponen bot ──────────────────────────────────────
from utils.colors import (
    green, red, yellow, cyan, magenta, blue, bold, dim, white,
    clear_screen
)
from fetcher.candle_tracker import CandleTracker
from fetcher.hyperliquid_ws import HyperliquidWS
from fetcher.hyperliquid_rest import HyperliquidREST
from fetcher.chainlink import ChainlinkBTC
from engine.result_tracker import ResultTracker
from executor.polymarket import PolymarketExecutor

# ── Konfigurasi dari .env ─────────────────────────────────────
DRY_RUN          = os.getenv("DRY_RUN", "false").lower() == "true"
MIN_ODDS         = float(os.getenv("MIN_ODDS", "0.45"))
BEAT_DISTANCE    = float(os.getenv("BEAT_DISTANCE", "25"))      # $25 default
SNIPE_MAX        = float(os.getenv("SNIPE_WINDOW_MAX", "30"))   # 30 detik
SNIPE_MIN        = float(os.getenv("SNIPE_WINDOW_MIN", "7"))    # 7 detik
AUTO_REDEEM      = os.getenv("AUTO_REDEEM_ENABLED", "true").lower() == "true"
CLAIM_INTERVAL   = int(os.getenv("CLAIM_CHECK_INTERVAL", "90"))

# ── State global ──────────────────────────────────────────────
class BotState:
    def __init__(self, bet_amount: float):
        self.bet_amount       = bet_amount
        self.auto_bet         = True
        self.bet_this_window  = False
        self.current_window   = None
        self.last_skip_reason = "Startup"
        self.last_bet_dir     = None
        self.last_bet_time    = 0.0
        self.last_claim_check = 0.0
        self.total_claimed    = 0
        self.manual_override  = None  # "UP" atau "DOWN"


# ── Dashboard renderer ────────────────────────────────────────
def render_dashboard(
    state:    BotState,
    candle:   CandleTracker,
    ws:       HyperliquidWS,
    rest:     HyperliquidREST,
    chainlink: ChainlinkBTC,
    results:  ResultTracker,
    executor: PolymarketExecutor,
    odds_up:  float,
    odds_down: float,
) -> None:
    """Render dashboard ke terminal (clear screen lalu print ulang)."""

    clear_screen()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Harga BTC (prioritas: WS > REST)
    btc_price = ws.btc_price or rest.btc_price
    cl_price  = chainlink.btc_price
    beat      = candle.beat_price

    # Hitung jarak dari beat
    beat_dist = None
    beat_dir  = None
    if btc_price and beat:
        beat_dist = btc_price - beat
        beat_dir  = "UP" if beat_dist >= 0 else "DOWN"

    # Filter status
    remaining = candle.remaining
    f1_ok = SNIPE_MIN <= remaining <= SNIPE_MAX
    f2_ok = beat_dist is not None and abs(beat_dist) >= BEAT_DISTANCE

    # Mode string
    mode_str = red("🔴 DRY RUN") if DRY_RUN else green("🟢 LIVE")
    auto_str = green("ON") if state.auto_bet else yellow("OFF")

    W = 60  # lebar dashboard

    def line(content=""):
        print(f"  {content}")

    # ── Header ──────────────────────────────────────────────
    print()
    print(bold("  ╔" + "═"*W + "╗"))
    print(bold(f"  ║") + cyan(f"  🎯 BOT SNIPER POLYMARKET BTC 5-MENIT".center(W)) + bold("║"))
    print(bold(f"  ║") + dim(f"  {now_str}  |  Mode: {mode_str}  |  Auto: {auto_str}".center(W+20)) + bold("║"))
    print(bold("  ╠" + "═"*W + "╣"))

    # ── Window info ──────────────────────────────────────────
    print(bold(f"  ║") + f"  {'POLYMARKET WINDOW':30}" + bold("║"))
    print(bold(f"  ║") + dim("  " + "─"*(W-2)) + bold("║"))

    prog_bar = candle.progress_bar(35)
    rem_color = red if remaining < 10 else yellow if remaining < 20 else green
    line(f"  Window  : {bold(candle.window_id or 'N/A')}")
    line(f"  Sisa    : {rem_color(f'{remaining:.1f}s')}  {prog_bar}")
    line(f"  Market  : {dim(candle.get_market_name())}")
    print(bold("  ╠" + "═"*W + "╣"))

    # ── Harga BTC ────────────────────────────────────────────
    print(bold(f"  ║") + f"  {'HARGA BTC':30}" + bold("║"))
    print(bold(f"  ║") + dim("  " + "─"*(W-2)) + bold("║"))

    btc_str  = f"${btc_price:,.2f}" if btc_price else "N/A"
    cl_str   = f"${cl_price:,.2f}" if cl_price else "N/A"
    beat_str = f"${beat:,.2f}" if beat else "Menunggu..."
    ws_stat  = green(f"WS:{ws.status}") if ws.status == "OK" else red(f"WS:{ws.status}")

    line(f"  Hyperliquid : {bold(btc_str)}  {ws_stat}")
    line(f"  Chainlink   : {cyan(cl_str)}  (oracle Polymarket)")
    line(f"  Beat Price  : {yellow(beat_str)}")

    if beat_dist is not None:
        dist_color = green if abs(beat_dist) >= BEAT_DISTANCE else dim
        dist_str = f"{'↑' if beat_dist >= 0 else '↓'} ${abs(beat_dist):,.2f}"
        line(f"  Jarak Beat  : {dist_color(dist_str)}  (min: ${BEAT_DISTANCE:.0f})")

    print(bold("  ╠" + "═"*W + "╣"))

    # ── Odds Polymarket ──────────────────────────────────────
    print(bold(f"  ║") + f"  {'ODDS POLYMARKET':30}" + bold("║"))
    print(bold(f"  ║") + dim("  " + "─"*(W-2)) + bold("║"))

    def odds_color(o):
        if o >= 0.55: return green(f"{o:.4f}")
        if o >= MIN_ODDS: return yellow(f"{o:.4f}")
        return red(f"{o:.4f}")

    up_ok   = odds_up   >= MIN_ODDS
    down_ok = odds_down >= MIN_ODDS
    line(f"  UP   : {odds_color(odds_up)}  {'✓' if up_ok else '✗ (terlalu rendah)'}")
    line(f"  DOWN : {odds_color(odds_down)}  {'✓' if down_ok else '✗ (terlalu rendah)'}")
    print(bold("  ╠" + "═"*W + "╣"))

    # ── Filter status ────────────────────────────────────────
    print(bold(f"  ║") + f"  {'FILTER STATUS':30}" + bold("║"))
    print(bold(f"  ║") + dim("  " + "─"*(W-2)) + bold("║"))

    f1_str = green("✓ PASS") if f1_ok else red(f"✗ FAIL  (sisa: {remaining:.1f}s, butuh {SNIPE_MIN}-{SNIPE_MAX}s)")
    f2_str = green("✓ PASS") if f2_ok else red(f"✗ FAIL  (jarak ${abs(beat_dist):.1f} < ${BEAT_DISTANCE:.0f})") if beat_dist is not None else yellow("⏳ Tunggu beat price")

    line(f"  F1 Waktu  : {f1_str}")
    line(f"  F2 Jarak  : {f2_str}")
    line(f"  Skip krn  : {dim(state.last_skip_reason)}")
    print(bold("  ╠" + "═"*W + "╣"))

    # ── Posisi aktif ─────────────────────────────────────────
    print(bold(f"  ║") + f"  {'POSISI AKTIF':30}" + bold("║"))
    print(bold(f"  ║") + dim("  " + "─"*(W-2)) + bold("║"))

    if results.current_bet:
        cb = results.current_bet
        line(f"  Window  : {cb.window_id}")
        dir_color = green if cb.direction == "UP" else red
        line(f"  Arah    : {dir_color(bold(cb.direction))}  @ odds {cb.odds:.4f}")
        line(f"  Bet     : ${cb.bet_amount:.2f} USDC")
        line(f"  Beat    : ${cb.beat_price:,.2f}")
    else:
        line(f"  {dim('Tidak ada posisi aktif')}")
        if state.bet_this_window:
            line(f"  {dim('(Sudah bet di window ini — tunggu close)')}")

    print(bold("  ╠" + "═"*W + "╣"))

    # ── Akun & Hasil ─────────────────────────────────────────
    print(bold(f"  ║") + f"  {'AKUN & HASIL':30}" + bold("║"))
    print(bold(f"  ║") + dim("  " + "─"*(W-2)) + bold("║"))

    bal_str  = f"${executor.balance:.2f} USDC"
    pnl      = results.running_pnl
    pnl_color = green if pnl >= 0 else red
    wr_str   = f"{results.win_rate:.1f}%"
    streak_t, streak_n = results.current_streak

    line(f"  Saldo    : {bold(bal_str)}")
    line(f"  Bet/trade: ${state.bet_amount:.2f} USDC")
    line(f"  Total PnL: {pnl_color(bold(f'${pnl:+.2f}'))}")
    line(f"  Bets     : {results.total_bets} (W:{results.wins} L:{results.losses}  WR:{wr_str})")
    line(f"  Streak   : {green(f'W{streak_n}') if streak_t == 'W' else red(f'L{streak_n}') if streak_t == 'L' else dim('-')}")
    line(f"  Claimed  : {state.total_claimed} posisi")

    print(bold("  ╠" + "═"*W + "╣"))

    # ── Kontrol ──────────────────────────────────────────────
    print(bold(f"  ║") + dim(f"  [U] UP  [D] DOWN  [A] Auto:{auto_str}  [Ctrl+C] Stop".ljust(W)) + bold("║"))
    print(bold("  ╚" + "═"*W + "╝"))
    print()


# ── Filter logic ──────────────────────────────────────────────
def check_filters(
    state:    BotState,
    candle:   CandleTracker,
    btc_price: float,
    odds_up:  float,
    odds_down: float,
) -> tuple[bool, str, str]:
    """
    Cek semua filter untuk menentukan apakah bot harus bet.

    Returns:
        (should_bet: bool, direction: str, reason: str)
        direction: "UP", "DOWN", atau ""
        reason: penjelasan kenapa skip atau bet
    """

    # Sudah bet di window ini?
    if state.bet_this_window:
        return False, "", "Sudah bet di window ini"

    # F1 — Time check
    remaining = candle.remaining
    if not (SNIPE_MIN <= remaining <= SNIPE_MAX):
        if remaining > SNIPE_MAX:
            return False, "", f"Terlalu awal ({remaining:.1f}s sisa)"
        else:
            return False, "", f"Terlalu mepet ({remaining:.1f}s < {SNIPE_MIN}s)"

    # Beat price harus ada
    beat = candle.beat_price
    if not beat or beat <= 0:
        return False, "", "Beat price belum tersedia"

    # Harga BTC harus ada
    if not btc_price or btc_price <= 0:
        return False, "", "Harga BTC tidak tersedia"

    # F2 — Beat distance
    diff = btc_price - beat
    abs_diff = abs(diff)

    if abs_diff < BEAT_DISTANCE:
        return False, "", f"Jarak terlalu dekat (${abs_diff:.2f} < ${BEAT_DISTANCE:.0f})"

    # Tentukan arah
    direction = "UP" if diff > 0 else "DOWN"

    # Cek odds minimum
    odds = odds_up if direction == "UP" else odds_down
    if odds < MIN_ODDS:
        return False, "", f"Odds {direction} terlalu rendah ({odds:.4f} < {MIN_ODDS})"

    return True, direction, f"Signal {direction}: BTC ${diff:+.2f} dari beat"


# ── Keyboard listener (cross-platform) ────────────────────────
def setup_keyboard_listener(state: BotState) -> None:
    """
    Setup keyboard listener non-blocking.
    Mendukung Windows (msvcrt) dan Linux/Mac (sys.stdin raw mode).
    Di VPS tanpa TTY, keyboard listener dinonaktifkan otomatis.
    """
    import threading
    import sys

    def _listen_windows():
        import msvcrt
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getwch().upper()
                if key == 'U':
                    state.manual_override = "UP"
                    logger.info("[KB] Manual override: UP")
                elif key == 'D':
                    state.manual_override = "DOWN"
                    logger.info("[KB] Manual override: DOWN")
                elif key == 'A':
                    state.auto_bet = not state.auto_bet
                    logger.info(f"[KB] Auto-bet: {'ON' if state.auto_bet else 'OFF'}")
            time.sleep(0.05)

    def _listen_linux():
        import tty, termios, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    key = sys.stdin.read(1).upper()
                    if key == 'U':
                        state.manual_override = "UP"
                        logger.info("[KB] Manual override: UP")
                    elif key == 'D':
                        state.manual_override = "DOWN"
                        logger.info("[KB] Manual override: DOWN")
                    elif key == 'A':
                        state.auto_bet = not state.auto_bet
                        logger.info(f"[KB] Auto: {'ON' if state.auto_bet else 'OFF'}")
                    elif key == '\x03':  # Ctrl+C
                        raise KeyboardInterrupt
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    # Cek apakah ada TTY (di VPS screen/tmux ada TTY, tapi jika pipe tidak ada)
    if not sys.stdin.isatty():
        logger.info("[KB] Tidak ada TTY — keyboard listener dinonaktifkan")
        return

    try:
        import msvcrt
        t = threading.Thread(target=_listen_windows, daemon=True)
        t.start()
        logger.info("[KB] Windows keyboard listener aktif")
    except ImportError:
        try:
            import tty, termios
            t = threading.Thread(target=_listen_linux, daemon=True)
            t.start()
            logger.info("[KB] Linux keyboard listener aktif")
        except Exception as e:
            logger.info(f"[KB] Keyboard listener tidak tersedia: {e}")


# ── Auto-claim ─────────────────────────────────────────────────
def maybe_run_claim(state: BotState, executor: PolymarketExecutor) -> None:
    """Cek dan claim posisi yang menang jika sudah waktunya."""
    if not AUTO_REDEEM:
        return
    if not executor._relayer or not executor._relayer.is_available():
        return

    now = time.time()
    if now - state.last_claim_check < CLAIM_INTERVAL:
        return

    state.last_claim_check = now
    positions = executor.get_redeemable_positions()
    if not positions:
        return

    logger.info(f"[Claim] Ditemukan {len(positions)} posisi untuk di-claim")
    for pos in positions:
        cond_id = pos.get("conditionId", "")
        if not cond_id:
            continue
        ok = executor.claim_position(cond_id)
        if ok:
            state.total_claimed += 1
        time.sleep(1)


# ── Main loop ──────────────────────────────────────────────────
async def main_loop(
    state:    BotState,
    candle:   CandleTracker,
    ws:       HyperliquidWS,
    rest:     HyperliquidREST,
    chainlink: ChainlinkBTC,
    results:  ResultTracker,
    executor: PolymarketExecutor,
) -> None:
    """Loop utama bot sniper."""

    odds_up:   float = 0.5
    odds_down: float = 0.5
    last_odds_update: float = 0.0
    last_balance_update: float = 0.0
    last_dashboard_update: float = 0.0
    last_resolved_window: str = ""

    logger.info("[Bot] Main loop dimulai")

    while True:
        now = time.time()

        # Update candle tracker
        candle.update()

        # Deteksi window baru
        if candle.is_new_window:
            state.bet_this_window = False
            logger.info(f"[Window] Baru: {candle.window_id}")

        # Ambil harga BTC
        btc_price = ws.btc_price
        if ws.is_stale or not btc_price:
            rest.update()
            btc_price = rest.btc_price

        # Set beat price di awal window (5 detik pertama)
        if btc_price and candle.elapsed < 5:
            candle.set_beat_price(btc_price)

        # Update Chainlink (setiap 15 detik)
        chainlink.update()

        # Update odds (setiap 3 detik)
        if now - last_odds_update > 3:
            market = executor.get_active_btc_market()
            if market:
                odds_up, odds_down = executor.get_odds(market)
            last_odds_update = now

        # Update balance (setiap 30 detik)
        if now - last_balance_update > 30:
            executor.get_balance()
            last_balance_update = now

        # Auto-claim check
        maybe_run_claim(state, executor)

        # ── Resolve bet window sebelumnya ──────────────────
        # Ketika window baru mulai, resolve bet di window sebelumnya
        remaining = candle.remaining
        if (remaining > 295 and results.current_bet and
                results.current_bet.window_id != candle.window_id and
                results.current_bet.window_id != last_resolved_window):
            # Gunakan chainlink price sebagai close price (oracle resmi)
            close_price = chainlink.btc_price or btc_price
            if close_price:
                rec = results.resolve_bet(results.current_bet.window_id, close_price)
                if rec:
                    last_resolved_window = rec.window_id

        # ── Logika betting ─────────────────────────────────
        should_execute = False
        direction = ""

        # Manual override (tombol U/D)
        if state.manual_override:
            direction = state.manual_override
            state.manual_override = None
            should_execute = True
            logger.info(f"[Manual] Override: {direction}")

        # Auto-bet
        elif state.auto_bet and btc_price:
            do_bet, direction, reason = check_filters(
                state, candle, btc_price, odds_up, odds_down
            )
            state.last_skip_reason = reason
            if do_bet:
                should_execute = True

        # ── Eksekusi bet ───────────────────────────────────
        if should_execute and direction and not state.bet_this_window:
            market = executor.get_active_btc_market(force_refresh=True)
            if market:
                odds = odds_up if direction == "UP" else odds_down
                token_id = market["token_id_up"] if direction == "UP" else market["token_id_down"]

                logger.info(
                    f"[Bet] Eksekusi {direction} | "
                    f"${state.bet_amount:.2f} @ {odds:.4f} | "
                    f"sisa {remaining:.1f}s | "
                    f"beat={candle.beat_price} btc={btc_price}"
                )

                ok = executor.place_order(
                    token_id=token_id,
                    amount=state.bet_amount,
                    side="BUY",
                    price=odds,
                    direction=direction,
                )

                if ok:
                    state.bet_this_window = True
                    state.last_bet_dir    = direction
                    state.last_bet_time   = now
                    results.record_bet(
                        window_id=candle.window_id,
                        direction=direction,
                        bet_amount=state.bet_amount,
                        odds=odds,
                        beat_price=candle.beat_price or btc_price,
                    )
                    logger.info(f"[Bet] ✓ {direction} berhasil!")
                else:
                    state.last_skip_reason = f"Order FAILED: {direction}"
                    logger.warning(f"[Bet] ✗ {direction} gagal")
            else:
                state.last_skip_reason = "Tidak ada market aktif"
                logger.warning("[Bet] Tidak ada market aktif")

        # ── Update dashboard ───────────────────────────────
        dashboard_interval = 0.2 if remaining <= 35 else 1.0
        if now - last_dashboard_update >= dashboard_interval:
            render_dashboard(
                state, candle, ws, rest, chainlink,
                results, executor, odds_up, odds_down
            )
            last_dashboard_update = now

        # ── Sleep ──────────────────────────────────────────
        if remaining <= 35:
            await asyncio.sleep(0.2)  # Agresif di zona snipe
        else:
            await asyncio.sleep(2.0)  # Hemat resource di luar zona


# ── Startup & konfirmasi ───────────────────────────────────────
def startup_prompt() -> tuple[float, bool]:
    """
    Tampilkan info startup dan minta konfirmasi dari user.
    Support CLI args untuk VPS non-interaktif:
      python bot_sniper.py --bet 5 --live
      python bot_sniper.py --bet 2 --dry-run

    Returns: (bet_amount, dry_run)
    """
    import argparse

    parser = argparse.ArgumentParser(description="Bot Sniper Polymarket BTC 5-Menit")
    parser.add_argument("--bet",     type=float, default=None,  help="Nominal bet per trade (USDC)")
    parser.add_argument("--live",    action="store_true",        help="Langsung live trading tanpa prompt")
    parser.add_argument("--dry-run", action="store_true",        help="Mode dry run (override .env)")
    args = parser.parse_args()

    # Tentukan dry_run
    dry = args.dry_run or DRY_RUN

    print()
    print(bold("=" * 60))
    print(bold("  🎯 BOT SNIPER POLYMARKET BTC 5-MENIT"))
    print(bold("=" * 60))
    print()
    print(f"  Strategi  : Sniper 30 detik terakhir")
    print(f"  Filter F1 : Sisa waktu {SNIPE_MIN}–{SNIPE_MAX} detik")
    print(f"  Filter F2 : Jarak beat ≥ ${BEAT_DISTANCE:.0f}")
    print(f"  Min Odds  : {MIN_ODDS}")
    print(f"  Auto Claim: {'ON' if AUTO_REDEEM else 'OFF'}")
    print()

    if dry:
        print(yellow("  ⚠️  Mode DRY RUN aktif (tidak ada bet sungguhan)"))
        print()

    # Bet amount: dari CLI arg atau prompt
    if args.bet is not None:
        bet_amount = args.bet
        print(f"  Bet per trade : {bold(f'${bet_amount:.2f} USDC')} (dari --bet)")
    else:
        while True:
            try:
                raw = input(f"  Masukkan nominal bet per trade (USDC, contoh: 5): $")
                bet_amount = float(raw.strip())
                if bet_amount <= 0:
                    print(red("  Nominal harus lebih dari 0"))
                    continue
                break
            except ValueError:
                print(red("  Masukkan angka yang valid"))

    print()
    print(f"  Bet per trade : {bold(f'${bet_amount:.2f} USDC')}")
    print(f"  Mode          : {red('DRY RUN') if dry else green('LIVE TRADING')}")
    print()

    # Konfirmasi: skip jika --live atau --dry-run diberikan
    if not dry:
        if args.live:
            print(green("  ✓ --live flag detected — langsung start"))
        else:
            confirm = input(f"  Ketik {bold('LIVE')} untuk konfirmasi: ").strip()
            if confirm != "LIVE":
                print(yellow("\n  Dibatalkan. Ketik LIVE untuk mulai live trading."))
                sys.exit(0)
    else:
        if not args.live and sys.stdin.isatty():
            input("  Tekan Enter untuk mulai dry run...")

    print()
    print(green("  ✓ Bot dimulai!"))
    print()
    return bet_amount, dry


# ── Entry point ────────────────────────────────────────────────
async def run():
    bet_amount, dry_run = startup_prompt()

    # Inisialisasi semua komponen
    state     = BotState(bet_amount)
    candle    = CandleTracker()
    ws        = HyperliquidWS()
    rest      = HyperliquidREST()
    chainlink = ChainlinkBTC()
    results   = ResultTracker()
    executor  = PolymarketExecutor(dry_run=dry_run)

    # Setup keyboard listener
    setup_keyboard_listener(state)

    # Connect WebSocket
    await ws.connect()
    logger.info("[Bot] WebSocket connecting...")
    await asyncio.sleep(2)  # Beri waktu WS connect

    # Cek saldo awal
    executor.get_balance()
    logger.info(f"[Bot] Saldo: ${executor.balance:.2f} USDC")

    if not dry_run and executor.balance < bet_amount:
        print(red(f"\n  ⚠️  Saldo (${executor.balance:.2f}) lebih kecil dari bet amount (${bet_amount:.2f})"))
        print(yellow("  Isi USDC ke wallet Polymarket terlebih dahulu."))
        if executor.balance <= 0:
            print(red("  Cek kredensial .env — balance 0 kemungkinan credentials salah."))

    try:
        await main_loop(state, candle, ws, rest, chainlink, results, executor)
    except KeyboardInterrupt:
        pass
    finally:
        await ws.disconnect()
        print(yellow("\n\n  Bot dihentikan. Sampai jumpa!"))
        print(f"  Hasil akhir: {results.summary()}")
        print()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(yellow("\n  Bot dihentikan."))
