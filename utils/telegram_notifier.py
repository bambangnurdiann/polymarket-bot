"""
utils/telegram_notifier.py
==========================
Telegram notifier untuk monitoring bot.

Notifikasi yang dikirim:
  - Bot start/stop
  - Bet berhasil (UP/DOWN, amount, odds)
  - Bet hasil (WIN/LOSS + PnL)
  - Error kritis
  - Ringkasan harian (setiap 24 jam)
  - Peringatan saldo rendah

Setup:
  1. Buat bot via @BotFather di Telegram → dapat BOT_TOKEN
  2. Kirim /start ke bot kamu → cari CHAT_ID via @userinfobot
  3. Isi di .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-xxx
       TELEGRAM_CHAT_ID=123456789

Cara test:
  python utils/telegram_notifier.py
"""

import logging
import os
import time
import threading
from datetime import datetime
from typing import Optional
from queue import Queue, Empty

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Telegram bot notifier dengan queue — tidak memblokir main loop.

    Semua pesan dikirim via background thread, sehingga
    jika Telegram lambat/down, bot tetap berjalan normal.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)

        self._queue: Queue = Queue(maxsize=100)
        self._last_daily   = 0.0
        self._daily_stats  = {"bets": 0, "wins": 0, "losses": 0, "pnl": 0.0}

        if self.enabled:
            self._start_worker()
            logger.info(f"[Telegram] Notifier aktif — chat_id={self.chat_id}")
        else:
            logger.info("[Telegram] Dinonaktifkan (TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID kosong)")

    def _start_worker(self) -> None:
        """Start background thread yang mengirim pesan dari queue."""
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()

    def _worker(self) -> None:
        """Background thread: kirim pesan dari queue satu per satu."""
        while True:
            try:
                text = self._queue.get(timeout=1)
                self._send_raw(text)
                time.sleep(0.5)  # Rate limit: max 2 msg/detik
            except Empty:
                continue
            except Exception as e:
                logger.debug(f"[Telegram] Worker error: {e}")

    def _send_raw(self, text: str) -> bool:
        """Kirim pesan ke Telegram. Returns True jika berhasil."""
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                self.API_URL.format(token=self.token),
                json={
                    "chat_id":    self.chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"[Telegram] Send error: {e}")
            return False

    def _enqueue(self, text: str) -> None:
        """Masukkan pesan ke queue (non-blocking)."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(text)
        except Exception:
            pass  # Queue penuh — skip

    # ── Public methods ────────────────────────────────────────

    def notify_start(self, bot_name: str, bet_amount: float, coins: list, dry_run: bool) -> None:
        mode = "🔴 DRY RUN" if dry_run else "🟢 LIVE"
        coins_str = ", ".join(coins) if coins else "BTC"
        text = (
            f"🚀 <b>{bot_name} Started</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Mode     : {mode}\n"
            f"Bet/trade: <b>${bet_amount:.2f} USDC</b>\n"
            f"Coins    : {coins_str}\n"
            f"Time     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._enqueue(text)

    def notify_stop(self, total_bets: int, wins: int, losses: int, pnl: float) -> None:
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        wr = (wins/total_bets*100) if total_bets > 0 else 0
        text = (
            f"🛑 <b>Bot Stopped</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Total bets: {total_bets}\n"
            f"W/L       : {wins}/{losses} ({wr:.1f}%)\n"
            f"{pnl_emoji} Net PnL  : <b>${pnl:+.2f}</b>\n"
            f"Time      : {datetime.now().strftime('%H:%M:%S')}"
        )
        self._enqueue(text)

    def notify_bet(
        self,
        coin:      str,
        direction: str,
        amount:    float,
        odds:      float,
        beat:      float,
        price:     float,
        window_id: str,
    ) -> None:
        arrow  = "⬆️" if direction == "UP" else "⬇️"
        diff   = price - beat
        text = (
            f"{arrow} <b>BET {direction} — {coin}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Window : {window_id}\n"
            f"Amount : <b>${amount:.2f} USDC</b>\n"
            f"Odds   : {odds:.4f}\n"
            f"Price  : ${price:,.2f}\n"
            f"Beat   : ${beat:,.2f} ({diff:+.2f})\n"
            f"Time   : {datetime.now().strftime('%H:%M:%S')}"
        )
        self._enqueue(text)

    def notify_result(
        self,
        coin:        str,
        direction:   str,
        result:      str,   # "WIN" atau "LOSS"
        pnl:         float,
        running_pnl: float,
        beat:        float,
        close_price: float,
        win_rate:    float,
    ) -> None:
        if result == "WIN":
            emoji = "✅"
            result_str = "<b>WIN</b>"
        else:
            emoji = "❌"
            result_str = "<b>LOSS</b>"

        pnl_emoji   = "📈" if running_pnl >= 0 else "📉"
        text = (
            f"{emoji} <b>{result_str} — {coin} {direction}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"PnL trade : <b>${pnl:+.2f}</b>\n"
            f"Beat      : ${beat:,.2f}\n"
            f"Close     : ${close_price:,.2f}\n"
            f"{pnl_emoji} Total PnL: <b>${running_pnl:+.2f}</b>\n"
            f"Win rate  : {win_rate:.1f}%\n"
            f"Time      : {datetime.now().strftime('%H:%M:%S')}"
        )
        self._enqueue(text)

        # Update daily stats
        self._daily_stats["bets"]   += 1
        self._daily_stats["pnl"]    += pnl
        if result == "WIN":
            self._daily_stats["wins"] += 1
        else:
            self._daily_stats["losses"] += 1

    def notify_error(self, message: str) -> None:
        text = (
            f"⚠️ <b>Bot Error</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{message}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        self._enqueue(text)

    def notify_low_balance(self, balance: float, bet_amount: float) -> None:
        text = (
            f"💸 <b>Saldo Rendah!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Saldo saat ini: <b>${balance:.2f}</b>\n"
            f"Bet per trade : ${bet_amount:.2f}\n"
            f"Sisa trade    : ~{int(balance/bet_amount)} kali\n"
            f"⚡ Segera top up USDC!"
        )
        self._enqueue(text)

    def notify_claim(self, amount_claimed: int, total_claimed: int) -> None:
        text = (
            f"💰 <b>Auto-Claim Berhasil</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Baru di-claim : {amount_claimed} posisi\n"
            f"Total sesi    : {total_claimed} posisi\n"
            f"Time          : {datetime.now().strftime('%H:%M:%S')}"
        )
        self._enqueue(text)

    def maybe_send_daily_summary(self, balance: float, running_pnl: float) -> None:
        """Kirim ringkasan harian setiap 24 jam."""
        now = time.time()
        if now - self._last_daily < 86400:  # 24 jam
            return

        self._last_daily = now
        stats  = self._daily_stats
        bets   = stats["bets"]
        wins   = stats["wins"]
        losses = stats["losses"]
        pnl    = stats["pnl"]
        wr     = (wins/bets*100) if bets > 0 else 0

        pnl_emoji = "📈" if pnl >= 0 else "📉"
        text = (
            f"📊 <b>Ringkasan Harian</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Tanggal  : {datetime.now().strftime('%Y-%m-%d')}\n"
            f"Total bet: {bets} (W:{wins} L:{losses})\n"
            f"Win rate : {wr:.1f}%\n"
            f"{pnl_emoji} PnL hari: <b>${pnl:+.2f}</b>\n"
            f"Total PnL: <b>${running_pnl:+.2f}</b>\n"
            f"Saldo    : ${balance:.2f}"
        )
        self._enqueue(text)

        # Reset daily stats
        self._daily_stats = {"bets": 0, "wins": 0, "losses": 0, "pnl": 0.0}

    def test(self) -> bool:
        """Kirim pesan test dan return True jika berhasil."""
        text = (
            f"✅ <b>Polymarket Bot — Test Message</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Telegram berhasil terhubung!\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return self._send_raw(text)


# ── Test CLI ──────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print()
    print("Testing Telegram connection...")
    notif = TelegramNotifier()

    if not notif.enabled:
        print("❌ TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID tidak ditemukan di .env")
        print()
        print("Cara setup:")
        print("1. Buka Telegram, cari @BotFather")
        print("2. Ketik /newbot → ikuti instruksi → dapat BOT_TOKEN")
        print("3. Cari @userinfobot → ketik /start → dapat CHAT_ID kamu")
        print("4. Tambah ke .env:")
        print("   TELEGRAM_BOT_TOKEN=123456:ABC-xxx")
        print("   TELEGRAM_CHAT_ID=123456789")
    else:
        ok = notif.test()
        if ok:
            print("✅ Pesan test berhasil dikirim ke Telegram!")
        else:
            print("❌ Gagal kirim. Cek BOT_TOKEN dan CHAT_ID di .env")
    print()
