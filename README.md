# Bot Sniper Polymarket BTC 5-Menit

Bot otomatis untuk trading di Polymarket market BTC Up or Down 5 menit.

## Struktur File

```
polymarket-bot/
  bot_sniper.py          ← Bot utama (file yang dijalankan)
  generate_api_creds.py  ← Generate API credentials
  claim_bot.py           ← Auto-claim standalone (opsional)
  .env                   ← Kredensial (RAHASIA, jangan share)
  requirements.txt       ← Dependencies
  executor/
    polymarket.py        ← Eksekutor order ke Polymarket
  fetcher/
    hyperliquid_ws.py    ← WebSocket harga BTC (real-time)
    hyperliquid_rest.py  ← REST fallback harga BTC
    candle_tracker.py    ← Tracker window 5 menit
    chainlink.py         ← Oracle harga BTC (Polymarket pakai ini)
  engine/
    result_tracker.py    ← Tracker hasil bet & PnL
  utils/
    colors.py            ← Helper warna terminal
  logs/                  ← Semua log tersimpan di sini
```

## Quick Start

### 1. Install dependencies
```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. Setup .env
Isi file `.env` dengan credentials:
```
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
DRY_RUN=true
MIN_ODDS=0.45
RELAYER_API_KEY=...
RELAYER_API_KEY_ADDRESS=0x...
AUTO_REDEEM_ENABLED=true
CLAIM_CHECK_INTERVAL=90
```

### 3. Generate API credentials (jika belum punya)
```bash
python generate_api_creds.py
```

### 4. Test dengan DRY_RUN dulu
```bash
# Pastikan DRY_RUN=true di .env
python bot_sniper.py
```

### 5. Live trading
```bash
# Ganti DRY_RUN=false di .env
python bot_sniper.py
# Ketik LIVE saat diminta konfirmasi
```

## Strategi Bot

- **Filter F1**: Hanya bet jika sisa waktu 7–30 detik
- **Filter F2**: Bet UP jika BTC > beat_price + $25, Bet DOWN jika BTC < beat_price - $25
- **1 bet per window**: Tidak double bet
- **Min odds**: Tidak bet jika odds < 0.45

## Konfigurasi .env

| Key | Default | Keterangan |
|-----|---------|-----------|
| DRY_RUN | false | true = simulasi, tidak bet sungguhan |
| MIN_ODDS | 0.45 | Minimum odds untuk bet |
| BEAT_DISTANCE | 25 | Jarak minimum dari beat price ($) |
| SNIPE_WINDOW_MAX | 30 | Mulai snipe saat sisa N detik |
| SNIPE_WINDOW_MIN | 7 | Berhenti snipe saat sisa N detik |

## Kontrol Keyboard (saat bot berjalan)

| Tombol | Fungsi |
|--------|--------|
| U | Manual bet UP (bypass filter) |
| D | Manual bet DOWN (bypass filter) |
| A | Toggle auto-bet ON/OFF |
| Ctrl+C | Stop bot |

## Monitoring

```bash
# Log real-time
tail -f logs/sniper_live.log

# Hasil bet
cat logs/sniper_live_results.csv
```

## ⚠️ Penting

- **SELALU test dengan DRY_RUN=true dulu** sebelum live
- **Mulai dengan nominal kecil** ($1-2) sebelum naikkan
- **Gunakan wallet terpisah** khusus bot
- **Jangan all-in** — hanya taruh yang siap hilang
- **Monitor win rate** — evaluasi jika WR konsisten < 40%
