"""A throwaway regtest node for tests and walkthroughs.

Nothing here is used by the audit commands.  ``Node`` starts a private
``bitcoind -regtest`` on a free port with its own data directory and stops it
again; ``find_bitcoin_core`` locates a Bitcoin Core install: ``$BITCOIN_CORE_DIR``,
or the download made by ``refcheck/fetch.sh`` in a bip322-core checkout that is
installed alongside.  ``python -m bip322audit.testing core`` and ``... port``
print the install directory and a free port for shell scripts.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["Node", "NodeError", "find_bitcoin_core", "free_port", "main"]


class NodeError(Exception):
    """The node answered an RPC with an error; ``code`` and ``message`` are its own."""

    def __init__(self, code: int, message: str):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


def free_port() -> int:
    """A TCP port nothing is listening on right now (the usual small race applies)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_bitcoin_core() -> Path | None:
    """The directory of a Bitcoin Core install (the one holding ``bin/bitcoind``), or None."""
    if os.environ.get("BITCOIN_CORE_DIR"):
        return Path(os.environ["BITCOIN_CORE_DIR"])
    try:
        import bip322core
    except ImportError:
        return None
    downloads = Path(bip322core.__file__).resolve().parent.parent / "refcheck" / "bin"
    found = sorted(p for p in downloads.glob("bitcoin-*") if (p / "bin" / "bitcoind").exists())
    return found[-1] if found else None


class Node:
    """A regtest bitcoind with no networking, reachable over JSON-RPC and through ``bitcoin-cli``."""

    USER = PASSWORD = "regtest"

    def __init__(self, core_dir: Path, datadir: Path, *, wallet: bool = True, extra_args: tuple[str, ...] = (), rpcport: int | None = None):
        self.bitcoind = Path(core_dir) / "bin" / "bitcoind"
        self.bitcoin_cli = Path(core_dir) / "bin" / "bitcoin-cli"
        self.datadir = Path(datadir)
        self.rpcport = rpcport or free_port()
        self.wallet = wallet
        self.extra_args = tuple(extra_args)
        self.proc: subprocess.Popen | None = None
        self._auth = base64.b64encode(f"{self.USER}:{self.PASSWORD}".encode()).decode()

    def start(self, timeout: float = 90.0) -> Node:
        """Start bitcoind on an emptied ``datadir`` and wait until it answers; returns self.

        Raises ``RuntimeError`` when it exits early or is not ready in ``timeout`` seconds, and leaves no daemon behind then.
        """
        if self.datadir.exists():
            shutil.rmtree(self.datadir)
        self.datadir.mkdir(parents=True)
        args = [
            str(self.bitcoind), "-regtest", f"-datadir={self.datadir}", f"-rpcport={self.rpcport}",
            "-rpcbind=127.0.0.1", "-rpcallowip=127.0.0.1", f"-rpcuser={self.USER}", f"-rpcpassword={self.PASSWORD}",
            "-server=1", "-listen=0", "-connect=0", "-dnsseed=0", "-printtoconsole=0",
            *(["-fallbackfee=0.0001"] if self.wallet else ["-disablewallet=1"]),
            *self.extra_args,
        ]  # fmt: skip
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"bitcoind exited early with code {self.proc.returncode}; see {self.datadir}/regtest/debug.log")
                try:
                    self.rpc("getblockchaininfo")
                    return self
                except (NodeError, OSError, ValueError):
                    time.sleep(0.25)
            raise RuntimeError(f"bitcoind did not become ready within {timeout}s")
        except BaseException:
            self.stop()  # never leave a daemon behind
            raise

    def cli_argv(self, rpcwallet: str | None = None) -> list[str]:
        """A ``bitcoin-cli`` command line for this node."""
        argv = [str(self.bitcoin_cli), "-regtest", f"-rpcport={self.rpcport}", f"-rpcuser={self.USER}", f"-rpcpassword={self.PASSWORD}"]
        if rpcwallet:
            argv.append(f"-rpcwallet={rpcwallet}")
        return argv

    def rpc(self, method: str, *params):
        """One JSON-RPC call straight to the node (not through ``bitcoin-cli``): the result, or :class:`NodeError`."""
        body = json.dumps({"jsonrpc": "1.0", "id": "regtest", "method": method, "params": list(params)}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.rpcport}/",
            data=body,
            headers={"Authorization": f"Basic {self._auth}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read())
        if payload.get("error"):
            raise NodeError(payload["error"]["code"], payload["error"]["message"])
        return payload["result"]

    def stop(self) -> None:
        """Stop the node and wait for it to exit; does nothing when it is not running.  The data directory stays."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.rpc("stop")
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def __enter__(self) -> Node:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    """``core`` prints the Bitcoin Core install directory (exit 1 when there is none), ``port`` a free port; anything else is exit 2."""
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["port"]:
        print(free_port())
        return 0
    if argv == ["core"]:
        found = find_bitcoin_core()
        if found is None:
            print("no Bitcoin Core install: set BITCOIN_CORE_DIR, or run refcheck/fetch.sh in the bip322-core checkout", file=sys.stderr)
            return 1
        print(found)
        return 0
    print("usage: python -m bip322audit.testing core|port", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
