"""
debug_beat_price.py
===================
Script untuk verifikasi beat price WCT vs Polymarket secara real-time.
Jalankan TERPISAH dari bot untuk debug.

Usage: python debug_beat_price.py
"""
import asyncio
import time
import os
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

WINDOW_SECONDS = 300
CHAINLINK_BTC = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
POLYGON_RPC   = "https://polygon-rpc.com"
CHAINLINK_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",    "type": "uint80"},
            {"name": "answer",     "type": "int256"},
            {"name": "startedAt",  "type": "uint256"},
            {"name": "updatedAt",  "type": "uint256"},
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


def get_window_info(ts=None):
    now = ts or time.time()
    win_start = (now // WINDOW_SECONDS) * WINDOW_SECONDS
    win_end   = win_start + WINDOW_SECONDS
    win_id    = datetime.fromtimestamp(win_start, tz=timezone.utc).strftime("%Y%m%d-%H%M")
    remaining = win_end - now
    return win_start, win_end, win_id, remaining


def fetch_chainlink():
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 5}))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_BTC),
            abi=CHAINLINK_ABI,
        )
        data = contract.functions.latestRoundData().call()
        decimals = contract.functions.decimals().call()
        round_id   = data[0]
        price      = data[1] / (10 ** decimals)
        updated_at = data[3]
        return round_id, price, updated_at
    except Exception as e:
        print(f"Error: {e}")
        return None, None, None


def main():
    print("\n=== CHAINLINK ROUND DEBUGGER ===")
    print("Monitoring round transitions untuk verifikasi beat price\n")

    prev_round_id = None
    rounds_seen   = []

    while True:
        now = time.time()
        win_start, win_end, win_id, remaining = get_window_info(now)

        round_id, price, updated_at = fetch_chainlink()
        if not round_id:
            time.sleep(2)
            continue

        # Round baru
        if round_id != prev_round_id:
            dt_updated = datetime.fromtimestamp(updated_at, tz=timezone.utc)
            print(
                f"  [NEW ROUND] #{round_id} "
                f"${price:,.2f} "
                f"updatedAt={dt_updated.strftime('%H:%M:%S')} UTC "
                f"(fetched at rem={remaining:.0f}s)"
            )
            rounds_seen.append({
                "round_id":   round_id,
                "price":      price,
                "updated_at": updated_at,
                "fetched_rem": remaining,
                "win_id":     win_id,
            })
            prev_round_id = round_id

        # Saat window hampir tutup, tampilkan kandidat beat price
        if remaining <= 15:
            win_end_ts = win_end
            candidates = [r for r in rounds_seen if r["updated_at"] <= win_end_ts]
            if candidates:
                best = max(candidates, key=lambda r: r["updated_at"])
                dt_best = datetime.fromtimestamp(best["updated_at"], tz=timezone.utc)
                print(
                    f"\n  >>> PREDIKSI BEAT PRICE window berikutnya: "
                    f"${best['price']:,.2f} "
                    f"(round #{best['round_id']}, "
                    f"CL_updated={dt_best.strftime('%H:%M:%S')} UTC)"
                )
                print(f"  >>> Bandingkan dengan Polymarket saat window close!\n")

        # Status line
        dt_updated = datetime.fromtimestamp(updated_at, tz=timezone.utc) if updated_at else None
        updated_str = dt_updated.strftime("%H:%M:%S") if dt_updated else "?"
        print(
            f"\r  win={win_id} rem={remaining:5.1f}s | "
            f"CL=${price:,.2f} round=#{round_id} "
            f"updatedAt={updated_str} UTC",
            end="", flush=True
        )

        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nDiberhentikan.")
