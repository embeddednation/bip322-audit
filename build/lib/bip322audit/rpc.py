"""``bitcoin-cli`` as the only channel to a node.

Every call is a subprocess so the user's own configuration (network, cookie,
``-rpcconnect``, ``-rpcwallet``) applies unchanged and nothing here holds
credentials.  Amounts are parsed as :class:`decimal.Decimal` and converted to
satoshis exactly.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from decimal import Decimal

__all__ = ["BARE_STRING_RPCS", "NETWORK_BY_CHAIN", "SATOSHI", "BitcoinCli", "RpcError", "btc", "network_of", "to_sat"]

SATOSHI = Decimal(100_000_000)
# what getblockchaininfo calls the chain -> the address and key encoding (embit's network name); testnet4 shares testnet's
NETWORK_BY_CHAIN = {"main": "main", "test": "test", "testnet4": "test", "regtest": "regtest", "signet": "signet"}
# RPCs that print a bare string; anything else that is not JSON is a wrapper talking, not the node
BARE_STRING_RPCS = frozenset(
    {"getblockhash", "getbestblockhash", "getnewaddress", "getrawchangeaddress", "sendtoaddress", "sendrawtransaction", "help", "stop"}
)
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class RpcError(Exception):
    """bitcoin-cli failed or returned something unexpected."""


def network_of(chain: str) -> str:
    """The encoding network for a node's chain; an unknown chain is an error, never a silent mainnet."""
    try:
        return NETWORK_BY_CHAIN[chain]
    except (KeyError, TypeError):
        raise RpcError(f"unknown chain {chain!r} (known: {', '.join(NETWORK_BY_CHAIN)})") from None


def btc(sat: int) -> str:
    """Satoshis as a BTC string with 8 decimals, exact."""
    return f"{Decimal(int(sat)) / SATOSHI:.8f}"


def to_sat(amount) -> int:
    """Exact BTC -> satoshi conversion for the numbers bitcoin-cli prints."""
    value = Decimal(str(amount)) * SATOSHI
    if value != value.to_integral_value():
        raise RpcError(f"amount {amount} is not a whole number of satoshis")
    return int(value)


class BitcoinCli:
    """Run ``bitcoin-cli`` with a fixed prefix, e.g. ``bitcoin-cli -signet -rpcwallet=watch``.

    ``argv`` (the prefix as a list) and ``timeout`` (seconds per call) are plain attributes and may be changed
    after construction.  Every method goes through :meth:`call`, so a subclass that overrides ``call`` alone
    (the tests' fake nodes do) answers for all of them.  Nothing is remembered between calls.
    """

    def __init__(self, command: str | list[str] = "bitcoin-cli", timeout: float = 600.0):
        self.argv = shlex.split(command) if isinstance(command, str) else list(command)
        self.timeout = timeout

    def call(self, method: str, *params):
        """One RPC: the parsed JSON answer (numbers with a decimal point as ``Decimal``), None for no output.

        Strings are passed as they are, everything else as JSON.  Raises :class:`RpcError` when the command
        cannot be run, times out, exits non-zero, or prints something that is not JSON (bare strings are
        accepted from the RPCs in ``BARE_STRING_RPCS`` only).
        """
        args = [str(p) if isinstance(p, str) else json.dumps(p) for p in params]
        try:
            proc = subprocess.run([*self.argv, method, *args], capture_output=True, text=True, timeout=self.timeout, check=False)
        except FileNotFoundError as exc:
            raise RpcError(
                f"cannot run {self.argv[0]!r}: {exc} (--cli is split like a shell command line: quote a path that contains spaces inside it)"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RpcError(f"{method} timed out after {self.timeout:g}s (see --timeout; the node may still be working on it)") from exc
        if proc.returncode != 0:
            raise RpcError(f"{method}: {proc.stderr.strip() or proc.stdout.strip() or f'exit {proc.returncode}'}")
        text = proc.stdout.strip()
        if text == "":
            return None
        try:
            return json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError:
            if method in BARE_STRING_RPCS:
                return text  # bare strings (getblockhash, getbestblockhash) come back unquoted
            raise RpcError(f"{method}: the --cli command printed something that is not JSON: {text[:60]!r}") from None

    # ---- convenience ------------------------------------------------------- #

    def info(self) -> dict:
        """``getblockchaininfo``: chain, tip height and tip hash as of one moment."""
        info = self.call("getblockchaininfo")
        if not isinstance(info, dict) or not {"chain", "blocks", "bestblockhash"} <= set(info):
            raise RpcError("getblockchaininfo: unexpected answer from the --cli command")
        return info

    def chain(self) -> str:
        """The chain as the node names it: ``main``, ``test``, ``testnet4``, ``signet`` or ``regtest``."""
        return self.info()["chain"]

    def tip(self) -> tuple[int, str]:
        """``(height, hash)`` of the best block, from one answer."""
        info = self.info()
        return int(info["blocks"]), info["bestblockhash"]

    def node_name(self) -> str:
        """The node's software and version in words, from its ``subversion`` ("/Satoshi:31.1.0/" is Bitcoin Core 31.1.0)."""
        try:
            raw = str(self.call("getnetworkinfo")["subversion"])
        except (RpcError, KeyError, TypeError):
            return "unknown node"
        parts = dict(p.split(":", 1) for p in raw.strip("/").split("/") if ":" in p)
        core = parts.get("Satoshi")
        if core and "Knots" in parts:
            return f"Bitcoin Knots {core} ({parts['Knots']})"
        if core:
            return f"Bitcoin Core {core}"
        return raw.strip("/") or "unknown node"

    def block_header(self, block_hash: str) -> dict:
        """``getblockheader``: a dict with at least ``height``; ``confirmations`` is -1 for a block off the main chain."""
        header = self.call("getblockheader", block_hash)
        if not isinstance(header, dict) or "height" not in header:
            raise RpcError(f"getblockheader {block_hash}: unexpected answer from the --cli command")
        return header

    def block_hash(self, height: int) -> str:
        """``getblockhash``: the main chain's block at ``height``, as 64 hex characters."""
        text = self.call("getblockhash", int(height))
        if not isinstance(text, str) or not _HASH_RE.fullmatch(text):
            raise RpcError(f"getblockhash {height}: not a block hash: {str(text)[:60]!r}")
        return text
