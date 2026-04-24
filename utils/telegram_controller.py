"""
utils/telegram_controller.py
=============================
Telegram Bot Controller — kontrol bot via Telegram secara real-time.

Commands yang tersedia:
  /status      — Status bot, PnL, WR saat ini
  /bet <amount> — Ubah nominal bet (contoh: /bet 3)
  /pause       — Pause auto-bet (bot tetap jalan tapi tidak bet)
  /resume      — Resume auto-bet
  /stop        — Stop bot sepenuhnya
  /config      — Lihat konfigurasi saat ini
  /set <key> <value> — Ubah konfigurasi (contoh: /set edge 0.12)
  /block <HH:MM-HH:MM> — Tambah session block (contoh: /block 03:55-05:05)
  /unblock     — Hapus semua session block
  /analysis    — Tampilkan loss analysis report
  /help        — Daftar semua command

Keys yang bisa diubah via /set:
  edge     → CHAINLINK_MIN_EDGE (contoh: /set edge 0.12)
  distance → LATE_BEAT_DISTANCE (contoh: /set distance 40)
  minrem   → CHAINLINK_MIN_REM  (contoh: /set minrem 60)
  maxrem   → CHAINLINK_MAX_REM  (contoh: /set maxrem 180)
  minodd   → MIN_ODDS           (contoh: /set minodd 0.47)
"""

import logging
import os
import threading
import time
from datetime import datetime
from queue import Queue, Empty
from typing import Optional, Callable

import requests

logger = logging.getLogger(__name__)


class BotCommand:
    """Satu command dari Telegram yang perlu diproses main loop."""
    def __init__(self, cmd: str, args: list, message_id: int):
        self.cmd        = cmd
        self.args       = args
        self.message_id = message_id
        self.ts         = time.time()


class TelegramController:
    """
    Two-way Telegram controller.
    - Kirim notifikasi (seperti TelegramNotifier)
    - Terima command dan forward ke main loop via queue
    """

    SEND_URL    = "https://api.telegram.org/bot{token}/sendMessage"
    UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)

        self._send_queue:    Queue = Queue(maxsize=100)
        self._command_queue: Queue = Queue(maxsize=50)
        self._last_update_id = 0
        self._running        = False
        self._last_daily     = 0.0
        self._daily_stats    = {"bets": 0, "wins": 0, "losses": 0, "pnl": 0.0}

        # Callback yang akan dipanggil saat ada command
        self._command_callback: Optional[Callable] = None

        if self.enabled:
            self._start_workers()
            logger.info(f"[TelegramCtrl] Controller aktif — chat_id={self.chat_id}")
        else:
            logger.info("[TelegramCtrl] Dinonaktifkan (token/chat_id kosong)")

    def _start_workers(self) -> None:
        """Start sender dan receiver threads."""
        self._running = True
        threading.Thread(target=self._sender_worker, daemon=True).start()
        threading.Thread(target=self._receiver_worker, daemon=True).start()

    def stop(self) -> None:
        """Stop sender/receiver workers dengan graceful."""
        self._running = False

    # ── Sender ────────────────────────────────────────────────

    def _sender_worker(self) -> None:
        while self._running:
            try:
                text = self._send_queue.get(timeout=1)
                self._send_raw(text)
                time.sleep(0.3)
            except Empty:
                continue
            except Exception as e:
                logger.debug(f"[TelegramCtrl] Sender error: {e}")

    def _send_raw(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                self.SEND_URL.format(token=self.token),
                json={
                    "chat_id":    self.chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"[TelegramCtrl] Send error: {e}")
            return False

    def send(self, text: str) -> None:
        """Kirim pesan ke Telegram (non-blocking)."""
        if not self.enabled:
            return
        try:
            self._send_queue.put_nowait(text)
        except Exception:
            pass

    # ── Receiver ──────────────────────────────────────────────

    def _receiver_worker(self) -> None:
        """Poll Telegram updates setiap 2 detik."""
        while self._running:
            try:
                self._poll_updates()
            except Exception as e:
                logger.debug(f"[TelegramCtrl] Receiver error: {e}")
            time.sleep(2)

    def _poll_updates(self) -> None:
        resp = requests.get(
            self.UPDATES_URL.format(token=self.token),
            params={
                "offset":  self._last_update_id + 1,
                "timeout": 1,
                "limit":   10,
            },
            timeout=5,
        )
        if resp.status_code != 200:
            return

        data = resp.json()
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._last_update_id = update["update_id"]
            msg = update.get("message", {})
            if not msg:
                continue

            # Hanya proses pesan dari chat_id yang authorized
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id != str(self.chat_id):
                logger.warning(f"[TelegramCtrl] Unauthorized message from {chat_id}")
                continue

            text = msg.get("text", "").strip()
            if text.startswith("/"):
                parts   = text.split()
                cmd     = parts[0].lower()
                args    = parts[1:]
                msg_id  = msg.get("message_id", 0)
                command = BotCommand(cmd, args, msg_id)
                try:
                    self._command_queue.put_nowait(command)
                    logger.info(f"[TelegramCtrl] Command received: {cmd} {args}")
                except Exception:
                    pass

    def get_pending_command(self) -> Optional[BotCommand]:
        """Ambil command berikutnya dari queue (non-blocking)."""
        try:
            return self._command_queue.get_nowait()
        except Empty:
            return None

    # ── Notification methods (sama dengan TelegramNotifier) ───

    def notify_start(self, bot_name: str, bet_amount: float, coins: list, dry_run: bool) -> None:
        mode = "🔴 DRY RUN" if dry_run else "🟢 LIVE"
        self.send(
            f"🚀 <b>{bot_name} Started</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Mode     : {mode}\n"
            f"Bet/trade: <b>${bet_amount:.2f} USDC</b>\n"
            f"Coins    : {', '.join(coins)}\n"
            f"Time     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Ketik /help untuk daftar command"
        )

    def notify_stop(self, total_bets: int, wins: int, losses: int, pnl: float) -> None:
        wr = (wins/total_bets*100) if total_bets > 0 else 0
        self.send(
            f"🛑 <b>Bot Stopped</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Total bets: {total_bets}\n"
            f"W/L       : {wins}/{losses} ({wr:.1f}%)\n"
            f"{'📈' if pnl >= 0 else '📉'} Net PnL: <b>${pnl:+.2f}</b>"
        )

    def notify_bet(self, coin: str, direction: str, amount: float,
                   odds: float, beat: float, price: float, window_id: str) -> None:
        arrow = "⬆️" if direction == "UP" else "⬇️"
        self.send(
            f"{arrow} <b>BET {direction} — {coin}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Window : {window_id}\n"
            f"Amount : <b>${amount:.2f} USDC</b>\n"
            f"Odds   : {odds:.4f}\n"
            f"Price  : ${price:,.2f}\n"
            f"Beat   : ${beat:,.2f} ({price-beat:+.2f})"
        )

    def notify_result(self, coin: str, direction: str, result: str,
                      pnl: float, running_pnl: float, beat: float,
                      close_price: float, win_rate: float) -> None:
        emoji = "✅" if result == "WIN" else "❌"
        self.send(
            f"{emoji} <b>{result} — {coin} {direction}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"PnL trade : <b>${pnl:+.2f}</b>\n"
            f"Beat      : ${beat:,.2f} → ${close_price:,.2f}\n"
            f"{'📈' if running_pnl >= 0 else '📉'} Total PnL: <b>${running_pnl:+.2f}</b>\n"
            f"Win rate  : {win_rate:.1f}%"
        )
        self._daily_stats["bets"]   += 1
        self._daily_stats["pnl"]    += pnl
        if result == "WIN":
            self._daily_stats["wins"] += 1
        else:
            self._daily_stats["losses"] += 1

    def notify_error(self, message: str) -> None:
        self.send(f"⚠️ <b>Bot Error</b>\n━━━━━━━━━━━━━━━\n{message}")

    def notify_low_balance(self, balance: float, bet_amount: float) -> None:
        self.send(
            f"💸 <b>Saldo Rendah!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Saldo   : <b>${balance:.2f}</b>\n"
            f"Sisa bet: ~{int(balance/bet_amount)} kali\n"
            f"⚡ Segera top up!"
        )

    def notify_claim(self, amount_claimed: int, total_claimed: int) -> None:
        self.send(
            f"💰 <b>Auto-Claim Berhasil</b>\n"
            f"Claimed : {amount_claimed} posisi\n"
            f"Total   : {total_claimed} posisi"
        )

    def notify_loss_insight(self, insight: dict) -> None:
        """Kirim ringkasan analisa loss per-event."""
        if not insight:
            return
        drivers = insight.get("primary_drivers", [])[:3]
        actions = insight.get("actions", [])[:3]
        drv_txt = "\n".join(f"• {d}" for d in drivers) if drivers else "• (belum ada driver dominan)"
        act_txt = "\n".join(f"• {a}" for a in actions) if actions else "• (belum ada aksi)"
        self.send(
            f"🧠 <b>Loss Deep Analysis</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Window     : {insight.get('window_id', '-')}\n"
            f"Risk       : <b>{insight.get('risk_level', '-')}</b>\n"
            f"Loss streak: {insight.get('loss_streak', 0)}\n"
            f"Sample     : {insight.get('sample_size', 0)} trades\n\n"
            f"<b>Primary Drivers</b>\n{drv_txt}\n\n"
            f"<b>Suggested Actions</b>\n{act_txt}"
        )

    def maybe_send_daily_summary(self, balance: float, running_pnl: float) -> None:
        now = time.time()
        if now - self._last_daily < 86400:
            return
        self._last_daily = now
        s   = self._daily_stats
        wr  = (s["wins"]/s["bets"]*100) if s["bets"] > 0 else 0
        self.send(
            f"📊 <b>Ringkasan Harian</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Tanggal  : {datetime.now().strftime('%Y-%m-%d')}\n"
            f"Bets     : {s['bets']} (W:{s['wins']} L:{s['losses']})\n"
            f"Win rate : {wr:.1f}%\n"
            f"PnL hari : <b>${s['pnl']:+.2f}</b>\n"
            f"Total PnL: <b>${running_pnl:+.2f}</b>\n"
            f"Saldo    : ${balance:.2f}"
        )
        self._daily_stats = {"bets": 0, "wins": 0, "losses": 0, "pnl": 0.0}

    def test(self) -> bool:
        return self._send_raw(
            f"✅ <b>Bot Connected!</b>\n"
            f"Telegram controller aktif.\n"
            f"Ketik /help untuk daftar command."
        )


# ── Command Handler ────────────────────────────────────────────

class CommandHandler:
    """
    Proses command dari Telegram dan eksekusi perubahan ke bot state.
    Dipanggil dari main loop bot_late.py setiap iterasi.
    """

    def __init__(self, tg: TelegramController):
        self.tg = tg

    def process(self, cmd: BotCommand, state, results, engines, mws) -> None:
        """
        Proses satu command.

        Args:
            cmd     : BotCommand dari Telegram
            state   : BotState dari bot_late.py
            results : ResultTracker
            engines : Dict[str, CoinEngine]
            mws     : MultiWS
        """
        c    = cmd.cmd
        args = cmd.args

        try:
            if c == "/help":
                self._cmd_help()

            elif c == "/status":
                self._cmd_status(state, results, engines, mws)

            elif c == "/bet":
                self._cmd_bet(args, state)

            elif c == "/pause":
                self._cmd_pause(state)

            elif c == "/resume":
                self._cmd_resume(state)

            elif c == "/stop":
                self._cmd_stop(state)

            elif c == "/config":
                self._cmd_config(state)

            elif c == "/set":
                self._cmd_set(args, state, engines)

            elif c == "/block":
                self._cmd_block(args, state)

            elif c == "/unblock":
                self._cmd_unblock(state)

            elif c == "/analysis":
                self._cmd_analysis(state)

            elif c == "/winrate":
                self._cmd_winrate(results)

            else:
                self.tg.send(f"❓ Command tidak dikenal: <code>{c}</code>\nKetik /help")

        except Exception as e:
            logger.error(f"[CommandHandler] Error processing {c}: {e}")
            self.tg.send(f"⚠️ Error: {e}")

    def _cmd_help(self) -> None:
        self.tg.send(
            "📋 <b>Daftar Command Bot</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "/status — Status bot & PnL\n"
            "/bet &lt;amount&gt; — Ubah nominal bet\n"
            "  contoh: <code>/bet 3</code>\n"
            "/pause — Pause auto-bet\n"
            "/resume — Resume auto-bet\n"
            "/stop — Stop bot\n"
            "/config — Lihat konfigurasi\n"
            "/set &lt;key&gt; &lt;value&gt; — Ubah config\n"
            "  <code>/set edge 0.12</code>\n"
            "  <code>/set distance 40</code>\n"
            "  <code>/set minrem 60</code>\n"
            "  <code>/set maxrem 180</code>\n"
            "  <code>/set minodd 0.47</code>\n"
            "/block &lt;HH:MM-HH:MM&gt; — Tambah session block\n"
            "  contoh: <code>/block 03:55-05:05</code>\n"
            "/unblock — Hapus semua session block\n"
            "/analysis — Loss analysis report\n"
            "/winrate — WR detail per jam\n"
        )

    def _cmd_status(self, state, results, engines, mws) -> None:
        uptime = int(time.time() - state.uptime_start)
        up_str = f"{uptime//3600}h {(uptime%3600)//60}m"
        pnl_e  = "📈" if results.running_pnl >= 0 else "📉"
        st, sn = results.current_streak
        streak = f"{'🔥' if st=='W' else '❄️'} {st}{sn}"

        # Harga BTC terkini
        btc_data  = mws.coins.get("BTC")
        btc_price = btc_data.get_price() if btc_data else None
        price_str = f"${btc_price:,.2f}" if btc_price else "N/A"

        # Engine info
        eng = engines.get("BTC") or list(engines.values())[0]
        beat = eng.candle.beat_price
        rem  = eng.candle.remaining

        beat_str = f"${beat:,.2f}" if beat else "N/A"
        self.tg.send(
            f"📊 <b>Bot Status</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Auto-bet : {'🟢 ON' if state.auto_bet else '🔴 OFF'}\n"
            f"Uptime   : {up_str}\n"
            f"Bet/trade: ${state.bet_amount:.2f}\n\n"
            f"<b>Market</b>\n"
            f"BTC Price: {price_str}\n"
            f"Beat     : {beat_str}\n"
            f"Sisa win : {rem:.0f}s\n\n"
            f"<b>Hasil</b>\n"
            f"Bets : {results.total_bets} (W:{results.wins} L:{results.losses})\n"
            f"WR   : {results.win_rate:.1f}%\n"
            f"{pnl_e} PnL : <b>${results.running_pnl:+.2f}</b>\n"
            f"Streak: {streak}"
        )

    def _cmd_bet(self, args: list, state) -> None:
        if not args:
            self.tg.send("❌ Format: <code>/bet &lt;amount&gt;</code>\nContoh: /bet 3")
            return
        try:
            amount = float(args[0])
            if amount <= 0 or amount > 100:
                self.tg.send("❌ Amount harus antara 0 dan 100")
                return
            old = state.bet_amount
            state.bet_amount = amount
            self.tg.send(
                f"✅ <b>Bet amount diubah</b>\n"
                f"${old:.2f} → <b>${amount:.2f}</b> per trade"
            )
            logger.info(f"[TelegramCtrl] Bet amount changed: ${old} → ${amount}")
        except ValueError:
            self.tg.send("❌ Amount tidak valid. Gunakan angka, contoh: /bet 3")

    def _cmd_pause(self, state) -> None:
        state.auto_bet = False
        self.tg.send("⏸ <b>Auto-bet PAUSED</b>\nBot tetap berjalan tapi tidak akan bet.\nGunakan /resume untuk lanjut.")
        logger.info("[TelegramCtrl] Auto-bet paused")

    def _cmd_resume(self, state) -> None:
        state.auto_bet = True
        self.tg.send("▶️ <b>Auto-bet RESUMED</b>\nBot akan bet kembali saat ada sinyal.")
        logger.info("[TelegramCtrl] Auto-bet resumed")

    def _cmd_stop(self, state) -> None:
        self.tg.send(
            "🛑 <b>Stop command diterima</b>\n"
            "Bot akan berhenti setelah window saat ini selesai.\n"
            "Untuk stop paksa, SSH ke VPS dan Ctrl+C."
        )
        state.auto_bet  = False
        state.stop_requested = True
        logger.info("[TelegramCtrl] Stop requested")

    def _cmd_config(self, state) -> None:
        import os
        self.tg.send(
            f"⚙️ <b>Konfigurasi Saat Ini</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"bet_amount  : ${state.bet_amount:.2f}\n"
            f"auto_bet    : {'ON' if state.auto_bet else 'OFF'}\n"
            f"MIN_EDGE    : {os.getenv('CHAINLINK_MIN_EDGE','?')}\n"
            f"BEAT_DIST   : {os.getenv('LATE_BEAT_DISTANCE','?')}\n"
            f"MIN_REM     : {os.getenv('CHAINLINK_MIN_REM','?')}s\n"
            f"MAX_REM     : {os.getenv('CHAINLINK_MAX_REM','?')}s\n"
            f"MIN_ODDS    : {os.getenv('MIN_ODDS','?')}\n"
            f"SESSION_BLK : {os.getenv('SESSION_BLOCKS', os.getenv('SESSION_BLOCK_START','none'))}\n"
            f"DRY_RUN     : {os.getenv('DRY_RUN','false')}\n"
            f"ACTIVE_COINS: {os.getenv('ACTIVE_COINS','BTC')}"
        )

    def _cmd_set(self, args: list, state, engines) -> None:
        if len(args) < 2:
            self.tg.send(
                "❌ Format: <code>/set &lt;key&gt; &lt;value&gt;</code>\n\n"
                "Keys tersedia:\n"
                "  edge, distance, minrem, maxrem, minodd"
            )
            return

        key   = args[0].lower()
        value = args[1]

        KEY_MAP = {
            "edge":     "CHAINLINK_MIN_EDGE",
            "distance": "LATE_BEAT_DISTANCE",
            "minrem":   "CHAINLINK_MIN_REM",
            "maxrem":   "CHAINLINK_MAX_REM",
            "minodd":   "MIN_ODDS",
        }

        if key not in KEY_MAP:
            self.tg.send(f"❌ Key tidak dikenal: <code>{key}</code>\nGunakan: edge, distance, minrem, maxrem, minodd")
            return

        try:
            float(value)  # Validasi angka
        except ValueError:
            self.tg.send(f"❌ Value tidak valid: <code>{value}</code>")
            return

        env_key = KEY_MAP[key]
        old_val = os.getenv(env_key, "?")
        os.environ[env_key] = value

        # Update .env file juga
        self._update_env_file(env_key, value)

        self.tg.send(
            f"✅ <b>Config diubah</b>\n"
            f"{env_key}\n"
            f"{old_val} → <b>{value}</b>\n\n"
            f"⚠️ Efektif di window berikutnya."
        )
        logger.info(f"[TelegramCtrl] Config changed: {env_key} = {value}")
        numeric_value = float(value)
        if env_key == "MIN_ODDS":
            for eng in engines.values():
                eng.min_odds = numeric_value
        elif env_key == "LATE_BEAT_DISTANCE":
            for eng in engines.values():
                eng.config["beat_distance"] = numeric_value
        elif env_key == "CHAINLINK_MIN_EDGE":
            for eng in engines.values():
                eng.cl_min_edge = numeric_value
        elif env_key == "CHAINLINK_MIN_REM":
            for eng in engines.values():
                eng.cl_min_rem = numeric_value
        elif env_key == "CHAINLINK_MAX_REM":
            for eng in engines.values():
                eng.cl_max_rem = numeric_value

    def _cmd_block(self, args: list, state) -> None:
        if not args:
            self.tg.send("❌ Format: <code>/block HH:MM-HH:MM</code>\nContoh: /block 03:55-05:05")
            return

        block_str = args[0]
        # Validasi format
        parts = block_str.split("-")
        if len(parts) != 2:
            self.tg.send("❌ Format salah. Contoh: /block 03:55-05:05")
            return

        # Tambah ke SESSION_BLOCKS
        current = os.getenv("SESSION_BLOCKS", "")
        new_blocks = f"{current},{block_str}" if current else block_str
        os.environ["SESSION_BLOCKS"] = new_blocks
        self._update_env_file("SESSION_BLOCKS", new_blocks)

        self.tg.send(
            f"✅ <b>Session Block Ditambahkan</b>\n"
            f"Block: <b>{block_str}</b>\n"
            f"Semua block: {new_blocks}"
        )
        logger.info(f"[TelegramCtrl] Session block added: {block_str}")

    def _cmd_unblock(self, state) -> None:
        os.environ["SESSION_BLOCKS"] = ""
        os.environ["SESSION_BLOCK_START"] = "00:00"
        os.environ["SESSION_BLOCK_END"]   = "00:01"
        self._update_env_file("SESSION_BLOCKS", "")
        self._update_env_file("SESSION_BLOCK_START", "00:00")
        self._update_env_file("SESSION_BLOCK_END", "00:01")
        self.tg.send("✅ <b>Semua session block dihapus</b>\nBot akan bet di semua jam.")
        logger.info("[TelegramCtrl] All session blocks removed")

    def _cmd_analysis(self, state) -> None:
        try:
            insights = state.loss_analyzer.analyze()
            if insights.get("status") == "insufficient_data":
                self.tg.send(f"📊 Butuh lebih banyak data ({insights['count']}/10 minimum)")
                return

            # Format ringkas untuk Telegram
            wr_rem = insights.get("wr_by_remaining", {})
            best_bucket = max(wr_rem.items(), key=lambda x: x[1].get("wr", 0), default=("?", {}))
            worst_bucket = min(wr_rem.items(), key=lambda x: x[1].get("wr", 100), default=("?", {}))

            recs = insights.get("recommendations", [])
            rec_str = "\n".join(f"• {r['action']}" for r in recs[:3])

            self.tg.send(
                f"📊 <b>Loss Analysis</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Total bets: {insights['total_bets']}\n"
                f"Overall WR: <b>{insights['overall_wr']:.1f}%</b>\n\n"
                f"Best zone : {best_bucket[0]}s ({best_bucket[1].get('wr', 0):.1f}%)\n"
                f"Worst zone: {worst_bucket[0]}s ({worst_bucket[1].get('wr', 0):.1f}%)\n\n"
                f"<b>Rekomendasi:</b>\n{rec_str}\n\n"
                f"Untuk report lengkap: jalankan\n"
                f"<code>python3 engine/loss_analyzer.py</code>"
            )
        except Exception as e:
            self.tg.send(f"⚠️ Error analysis: {e}")

    def _cmd_winrate(self, results) -> None:
        if results.total_bets == 0:
            self.tg.send("📊 Belum ada data bet")
            return
        self.tg.send(
            f"📊 <b>Win Rate Summary</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Total : {results.total_bets} bets\n"
            f"Win   : {results.wins} ({results.win_rate:.1f}%)\n"
            f"Loss  : {results.losses}\n"
            f"PnL   : <b>${results.running_pnl:+.2f}</b>\n"
            f"Streak: {results.current_streak[0]}{results.current_streak[1]}"
        )

    def _update_env_file(self, key: str, value: str) -> None:
        """Update nilai di file .env."""
        env_path = ".env"
        if not os.path.exists(env_path):
            return
        try:
            import re
            with open(env_path, "r") as f:
                content = f.read()
            pattern     = rf"^{key}=.*$"
            replacement = f"{key}={value}"
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            else:
                content += f"\n{key}={value}"
            with open(env_path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.debug(f"[TelegramCtrl] Update .env error: {e}")
