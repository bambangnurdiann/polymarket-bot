"""
fetcher/chainlink.py
====================
Ambil harga BTC dari Chainlink oracle di Polygon.

Polymarket menggunakan Chainlink sebagai oracle resmi untuk menentukan
hasil UP/DOWN. Harga ini yang PALING PENTING untuk dimonitor karena
inilah yang menentukan apakah bet kamu menang atau kalah.

Chainlink BTC/USD di Polygon:
  Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6f
  Network : Polygon Mainnet (Chain ID 137)
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Chainlink BTC/USD Aggregator di Polygon
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

# ABI minimal untuk latestRoundData
CHAINLINK_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",         "type": "uint80"},
            {"name": "answer",          "type": "int256"},
            {"name": "startedAt",       "type": "uint256"},
            {"name": "updatedAt",       "type": "uint256"},
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

# Public RPC Polygon (tidak butuh API key)
POLYGON_RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://matic-mainnet.chainstacklabs.com",
]


class ChainlinkBTC:
    """
    Client untuk harga BTC dari Chainlink oracle Polygon.
    
    Attributes:
        btc_price    : float  — Harga BTC terkini
        last_update  : float  — Timestamp update terakhir
        decimals     : int    — Desimal Chainlink (biasanya 8)
    """

    def __init__(self, poll_interval: float = 15.0):
        self.btc_price: Optional[float] = None
        self.last_update: float = 0.0
        self.decimals: int = 8
        self.poll_interval = poll_interval
        self._w3 = None
        self._contract = None
        self._initialized = False

    def _init_web3(self) -> bool:
        """Inisialisasi Web3 connection ke Polygon."""
        try:
            from web3 import Web3
            for rpc_url in POLYGON_RPC_URLS:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
                    if w3.is_connected():
                        self._w3 = w3
                        self._contract = w3.eth.contract(
                            address=Web3.to_checksum_address(CHAINLINK_BTC_USD_POLYGON),
                            abi=CHAINLINK_ABI,
                        )
                        self.decimals = self._contract.functions.decimals().call()
                        self._initialized = True
                        logger.info(f"[Chainlink] Connected via {rpc_url}")
                        return True
                except Exception as e:
                    logger.debug(f"[Chainlink] RPC {rpc_url} failed: {e}")
                    continue
        except ImportError:
            logger.warning("[Chainlink] web3 library tidak terinstall")
        return False

    def update(self) -> bool:
        """
        Update harga BTC dari Chainlink.
        Returns True jika berhasil.
        """
        now = time.time()
        if now - self.last_update < self.poll_interval:
            return self.btc_price is not None

        if not self._initialized:
            if not self._init_web3():
                return False

        try:
            round_data = self._contract.functions.latestRoundData().call()
            # round_data: (roundId, answer, startedAt, updatedAt, answeredInRound)
            raw_price = round_data[1]
            self.btc_price = raw_price / (10 ** self.decimals)
            self.last_update = now
            return True
        except Exception as e:
            logger.debug(f"[Chainlink] Update error: {e}")
            # Reset init agar coba reconnect berikutnya
            self._initialized = False
            return False

    @property
    def is_stale(self) -> bool:
        """True jika data lebih dari 60 detik yang lalu."""
        return (time.time() - self.last_update) > 60

    @property
    def status(self) -> str:
        if not self._initialized:
            return "INIT"
        if self.is_stale:
            return "STALE"
        return "OK"
