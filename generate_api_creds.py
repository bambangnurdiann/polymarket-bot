"""
generate_api_creds.py
=====================
Script untuk generate API credentials Polymarket dari private key.

Cara pakai:
  1. Pastikan POLYMARKET_PRIVATE_KEY sudah ada di .env
  2. python generate_api_creds.py
  3. Copy output ke .env

Credentials yang dihasilkan:
  - POLYMARKET_API_KEY
  - POLYMARKET_API_SECRET
  - POLYMARKET_API_PASSPHRASE
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

def sanitize_private_key(raw: str) -> str:
    """
    Bersihkan private key dari karakter yang tidak valid.
    - Strip whitespace/newline
    - Pastikan diawali 0x
    - Hanya karakter hex yang valid
    """
    key = raw.strip()
    # Hapus semua whitespace tersembunyi
    key = "".join(key.split())
    # Tambah 0x jika belum ada
    if not key.startswith("0x") and not key.startswith("0X"):
        key = "0x" + key
    # Pastikan lowercase
    key = key.lower()
    # Validasi: harus 0x + 64 karakter hex
    hex_part = key[2:]
    valid_chars = set("0123456789abcdef")
    invalid = [c for c in hex_part if c not in valid_chars]
    if invalid:
        raise ValueError(
            f"Private key mengandung karakter tidak valid: {set(invalid)}\n"
            f"  Pastikan private key hanya berisi karakter hex (0-9, a-f)\n"
            f"  Panjang hex part: {len(hex_part)} (harus 64)"
        )
    if len(hex_part) != 64:
        raise ValueError(
            f"Panjang private key salah: {len(hex_part)} karakter (harus 64)\n"
            f"  Pastikan lo copy private key lengkap dari wallet"
        )
    return key


def main():
    print()
    print("=" * 55)
    print("  GENERATE POLYMARKET API CREDENTIALS")
    print("=" * 55)
    print()

    raw_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not raw_key:
        print("❌ POLYMARKET_PRIVATE_KEY tidak ditemukan di .env")
        print("   Tambahkan POLYMARKET_PRIVATE_KEY=0x... ke file .env")
        sys.exit(1)

    # Sanitize private key
    try:
        private_key = sanitize_private_key(raw_key)
    except ValueError as e:
        print(f"❌ Private key tidak valid: {e}")
        print()
        print("  Tips:")
        print("  - Buka MetaMask → Account Details → Export Private Key")
        print("  - Copy key tanpa spasi atau karakter tambahan")
        print("  - Paste ke .env: POLYMARKET_PRIVATE_KEY=0x<key>")
        sys.exit(1)

    print(f"  Private key ditemukan: {private_key[:8]}...{private_key[-4:]}")
    print(f"  Panjang            : {len(private_key[2:])} karakter hex ✓")
    print()

    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
        )

        print("  Generating API credentials...")
        creds = client.create_or_derive_api_creds()

        # Docs: response bisa dict atau object tergantung versi library
        if isinstance(creds, dict):
            api_key        = creds.get("api_key") or creds.get("apiKey", "")
            api_secret     = creds.get("api_secret") or creds.get("secret", "")
            api_passphrase = creds.get("api_passphrase") or creds.get("passphrase", "")
        else:
            api_key        = getattr(creds, "api_key", "")
            api_secret     = getattr(creds, "api_secret", "")
            api_passphrase = getattr(creds, "api_passphrase", "")

        if not api_key:
            print("❌ Credentials kosong — coba lagi atau generate manual di polymarket.com/settings")
            sys.exit(1)

        print()
        print("✅ Credentials berhasil dibuat!")
        print()
        print("  Tambahkan ke file .env:")
        print("  " + "-" * 45)
        print(f"  POLYMARKET_API_KEY={api_key}")
        print(f"  POLYMARKET_API_SECRET={api_secret}")
        print(f"  POLYMARKET_API_PASSPHRASE={api_passphrase}")
        print("  " + "-" * 45)
        print()

        # Tanya apakah mau langsung update .env
        ans = input("  Update .env otomatis? (y/n): ").strip().lower()
        if ans == 'y':
            _update_env(api_key, api_secret, api_passphrase)
            print("✅ .env berhasil diupdate!")
        else:
            print("  Copy manual ke .env ya.")

    except ImportError:
        print("❌ py-clob-client belum terinstall")
        print("   Run: pip install py-clob-client")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        print()
        print("  Kemungkinan penyebab:")
        print("  - Private key salah format (harus diawali 0x)")
        print("  - Koneksi internet bermasalah")
        print("  - Wallet belum pernah digunakan di Polymarket")
        sys.exit(1)

    print()


def _update_env(api_key: str, api_secret: str, api_passphrase: str) -> None:
    """Update file .env dengan API credentials baru."""
    env_path = ".env"
    if not os.path.exists(env_path):
        print("❌ File .env tidak ditemukan")
        return

    with open(env_path, "r") as f:
        content = f.read()

    # Replace atau append tiap key
    def set_key(content: str, key: str, value: str) -> str:
        import re
        pattern = rf"^{key}=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, content, re.MULTILINE):
            return re.sub(pattern, replacement, content, flags=re.MULTILINE)
        else:
            return content + f"\n{replacement}"

    content = set_key(content, "POLYMARKET_API_KEY", api_key)
    content = set_key(content, "POLYMARKET_API_SECRET", api_secret)
    content = set_key(content, "POLYMARKET_API_PASSPHRASE", api_passphrase)

    with open(env_path, "w") as f:
        f.write(content)


if __name__ == "__main__":
    main()