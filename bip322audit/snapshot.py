"""Snapshot: the wallet's coins at the stamp block, and one BIP-322 PSBT per funded address.

Sources for the coins:

* ``listunspent`` on a Core wallet that holds the descriptor (``-rpcwallet=``);
  fast, and its ``desc`` field tells us branch and index of every address.
* ``scantxoutset`` with the wallet's descriptors; no Core wallet needed, scans
  the whole UTXO set (a minute or two on mainnet).

Only outputs confirmed at or before the stamp block are included, so the
snapshot means "the wallet's coins as of block N".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from bip322core.msglint import lint_message
from bip322core.psbt import create_psbt
from bip322core.wallet import DerivedAddress, Wallet, WalletError

from . import TOOL
from .rpc import BitcoinCli, RpcError, btc, network_of, to_sat
from .stamp import DEFAULT_DEPTH, Stamp, compose_message, fetch_stamp, iso_utc

__all__ = [
    "DEFAULT_DEPTH",
    "TIP_RETRIES",
    "AddressCoins",
    "Snapshot",
    "Utxo",
    "check_wallet_against_node",
    "coins_from_listunspent",
    "coins_from_scantxoutset",
    "descriptor_kind",
    "load_snapshot",
    "psbt_file_name",
    "take_snapshot",
    "wallet_from_node",
    "write_bundle",
    "write_text_atomic",
]

_ORIGIN_RE = re.compile(r"\[[0-9a-fA-F]{8}((?:/\d+[h'H]?)+)\]")
TIP_RETRIES = 5  # how often the coins are read again when a block arrives while they are being read


def write_text_atomic(path: Path, text: str) -> None:
    """Write beside the target and rename over it: a crash or a full disk leaves the old file, never half a new one."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def descriptor_kind(descriptor: str) -> str:
    """``wsh(sortedmulti)`` or ``wpkh``: the descriptor's functions without its keys, safe to print anywhere."""
    names = re.findall(r"[a-z_0-9]+(?=\()", descriptor.split("#")[0])[:2]
    return names[0] + (f"({names[1]})" if len(names) > 1 else "") if names else "unknown"


def _left(left_out: dict | None, amount) -> None:
    if left_out is not None:
        left_out["outputs"] = left_out.get("outputs", 0) + 1
        left_out["amount_sat"] = left_out.get("amount_sat", 0) + to_sat(amount)


@dataclass
class Utxo:
    """A listed output, as ``snapshot.json`` and ``proofs.json`` carry it: these five fields and nothing else."""

    txid: str
    vout: int
    amount_sat: int
    height: int  # block that created it
    blockhash: str | None = None  # that block's hash: lets a verifier fetch the creating tx without -txindex

    def to_dict(self) -> dict:
        return {"txid": self.txid, "vout": self.vout, "amount_sat": self.amount_sat, "height": self.height, "blockhash": self.blockhash}

    @classmethod
    def from_dict(cls, d: dict) -> Utxo:
        return cls(str(d["txid"]), int(d["vout"]), int(d["amount_sat"]), int(d["height"]), d.get("blockhash"))


@dataclass
class AddressCoins:
    """One wallet address and its outputs confirmed at the stamp block (none for an address proven before it is funded)."""

    derived: DerivedAddress
    utxos: list[Utxo] = field(default_factory=list)

    @property
    def total_sat(self) -> int:
        return sum(u.amount_sat for u in self.utxos)


def _index_from_desc(wallet: Wallet, desc: str | None, address: str) -> DerivedAddress | None:
    """Branch/index from the concrete descriptor Core reports for a UTXO, confirmed by re-deriving."""
    if not desc:
        return None
    match = _ORIGIN_RE.search(desc)
    if not match:
        return None
    parts = match.group(1).strip("/").split("/")
    if len(parts) < 2 or not parts[-1].isdigit() or not parts[-2].isdigit():
        return None
    branch, index = int(parts[-2]), int(parts[-1])
    if branch >= max(wallet.num_branches, 1):
        return None
    candidate = wallet.derive(index, branch)
    return candidate if candidate.address == address else None


def _locate(wallet: Wallet, address: str, desc: str | None, max_index: int) -> DerivedAddress | None:
    """The wallet address behind a node entry, or None when it is not ours (or not an address at all)."""
    try:
        return _index_from_desc(wallet, desc, address) or wallet.find_address(address, max_index=max_index)
    except WalletError:
        return None


def _memo_locate(wallet: Wallet, max_index: int):
    """:func:`_locate` that derives once per node entry: a foreign address costs a search of the whole range, per output otherwise."""
    return cache(lambda address, desc: _locate(wallet, address, desc, max_index))


def coins_from_listunspent(
    cli: BitcoinCli, wallet: Wallet, stamp: Stamp, tip_height: int, *, max_index: int = 1000, left_out: dict | None = None
) -> list[AddressCoins]:
    """Wallet coins confirmed at the stamp block, via the Core wallet selected with ``-rpcwallet``.

    Heights are counted back from ``tip_height``, so the caller must make sure the tip did not move
    meanwhile (:func:`take_snapshot` does).  ``left_out`` counts the node wallet's outputs that are
    not this descriptor's: ``{"outputs": n, "amount_sat": s}``.  Within one call an address is derived
    once and a block's hash is asked for once.
    """
    minconf = tip_height - stamp.height + 1
    entries = cli.call("listunspent", minconf, 9999999)
    locate, block_hash = _memo_locate(wallet, max_index), cache(cli.block_hash)  # for this read only: a new read asks again
    by_address: dict[str, AddressCoins] = {}
    for e in entries:
        address = e.get("address")
        derived = locate(address, e.get("desc")) if address else None
        if derived is None:
            _left(left_out, e["amount"])  # coins of another descriptor in the same Core wallet: counted, never silent
            continue
        height = tip_height - int(e["confirmations"]) + 1
        if height > stamp.height:
            continue
        by_address.setdefault(address, AddressCoins(derived)).utxos.append(
            Utxo(e["txid"], int(e["vout"]), to_sat(e["amount"]), height, block_hash(height))
        )
    return _sorted(by_address)


def coins_from_scantxoutset(
    cli: BitcoinCli, wallet: Wallet, stamp: Stamp, *, scan_range: int = 1000, left_out: dict | None = None
) -> list[AddressCoins]:
    """Wallet coins confirmed at the stamp block, via a UTXO-set scan of the wallet descriptors (no Core wallet).

    ``left_out`` as for :func:`coins_from_listunspent`; so is what one call asks and derives only once.
    """
    descriptors = [{"desc": d, "range": [0, scan_range]} for d in wallet.core_descriptors()]
    result = cli.call("scantxoutset", "start", descriptors)
    if not result or not result.get("success"):
        raise RpcError("scantxoutset did not succeed (another scan running?)")
    locate, block_hash = _memo_locate(wallet, scan_range), cache(cli.block_hash)
    by_address: dict[str, AddressCoins] = {}
    for u in result.get("unspents", []):
        if int(u["height"]) > stamp.height:
            continue
        address = _address_of_scriptpubkey(wallet, u["scriptPubKey"])
        derived = locate(address, u.get("desc")) if address else None
        if derived is None:
            _left(left_out, u["amount"])
            continue
        height = int(u["height"])
        by_address.setdefault(derived.address, AddressCoins(derived)).utxos.append(
            Utxo(u["txid"], int(u["vout"]), to_sat(u["amount"]), height, block_hash(height))
        )
    return _sorted(by_address)


def _address_of_scriptpubkey(wallet: Wallet, spk_hex: str) -> str | None:
    from embit.networks import NETWORKS
    from embit.script import Script

    try:
        return Script(bytes.fromhex(spk_hex)).address(NETWORKS[wallet.network])
    except Exception:  # noqa: BLE001
        return None


def _sorted(by_address: dict[str, AddressCoins]) -> list[AddressCoins]:
    coins = sorted(by_address.values(), key=lambda c: (c.derived.branch, c.derived.index))
    for c in coins:
        c.utxos.sort(key=lambda u: (u.height, u.txid, u.vout))
    return coins


# --------------------------------------------------------------------------- #
# the bundle on disk
# --------------------------------------------------------------------------- #


def _multipath(desc: str) -> str:
    """``.../0/*`` or ``.../1/*`` descriptors as one ``<0;1>`` descriptor (checksum dropped)."""
    body = desc.split("#")[0]
    return body.replace("/0/*", "/<0;1>/*").replace("/1/*", "/<0;1>/*")


def wallet_from_node(cli: BitcoinCli, chain: str | None = None, progress=None) -> Wallet:
    """The wallet behind the node's loaded wallet, from ``listdescriptors``.

    Works for descriptor wallets holding one supported descriptor family
    (``wsh(multi/sortedmulti(...))`` or ``wpkh(...)``, receive and change);
    anything else needs ``--descriptor``.  Descriptors of other kinds in the
    same node wallet are not covered; ``progress`` is told which kinds.
    """
    chain = chain or cli.chain()
    try:
        listing = cli.call("listdescriptors")
    except RpcError as exc:
        raise RpcError(f"cannot read the node wallet's descriptors ({exc}); pass --descriptor") from exc
    families: dict[str, Wallet] = {}
    unsupported = []
    for entry in listing.get("descriptors", []):
        text = _multipath(entry["desc"])
        try:
            wallet = Wallet.from_descriptor(text, network=network_of(chain))
        except WalletError:
            unsupported.append(descriptor_kind(entry["desc"]))  # the kind only: the text holds keys
            continue
        families.setdefault(wallet.to_descriptor(), wallet)
    if len(families) == 1:
        if unsupported and progress:
            kinds = ", ".join(sorted(set(unsupported)))
            progress(
                f"the node wallet also holds {len(unsupported)} descriptor(s) of a kind not covered here ({kinds}): their coins are left out"
            )
        return next(iter(families.values()))
    if not families:
        raise RpcError(
            "the node wallet has no wsh(multi/sortedmulti) or wpkh descriptor"
            + (f" (found: {', '.join(unsupported)})" if unsupported else "")
            + "; pass --descriptor"
        )
    raise RpcError("the node wallet holds several descriptor families; pass --descriptor to choose: " + " | ".join(families))


def check_wallet_against_node(cli: BitcoinCli, wallet: Wallet) -> None:
    """Refuse a --descriptor that the node wallet does not contain."""
    try:
        listing = cli.call("listdescriptors")
    except RpcError:
        return  # legacy wallet or no descriptor support: nothing to compare with
    node_descs = {_multipath(e["desc"]) for e in listing.get("descriptors", [])}
    if wallet.to_descriptor(checksum=False) not in node_descs:
        raise RpcError("the given descriptor is not one of the node wallet's descriptors (listdescriptors); wrong wallet or wrong file?")


@dataclass
class Snapshot:
    """What ``snapshot.json`` holds (:meth:`to_dict` is the file).  It is the owner's file: descriptor, branch and index are in it."""

    created_utc: str
    chain: str
    tip_height: int
    stamp: Stamp
    message: str
    wallet_descriptor: str
    policy: str
    addresses: list[dict]
    source: str
    node_wallet: str | None = None  # the node wallet the coins came from; finalize asks it for the spend history
    skipped_proven: int = 0  # outputs left out because an earlier bundle proves their address (--skip-proven)
    left_out: dict = field(default_factory=dict)  # why -> {"outputs": n, "amount_sat": s}, for everything seen and not listed

    @property
    def total_sat(self) -> int:
        return sum(a["total_sat"] for a in self.addresses)

    def to_dict(self) -> dict:
        return {
            "tool": TOOL,
            "created_utc": self.created_utc,
            "chain": self.chain,
            "tip_height_at_snapshot": self.tip_height,
            "stamp": self.stamp.to_dict(),
            "message": self.message,
            "wallet": {"descriptor": self.wallet_descriptor, "policy": self.policy},
            "source": self.source,
            "node_wallet": self.node_wallet,
            "skipped_proven": self.skipped_proven,
            "left_out": self.left_out,
            "addresses": self.addresses,
            "total_sat": self.total_sat,
            "total_btc": btc(self.total_sat),
        }


def take_snapshot(
    cli: BitcoinCli,
    wallet: Wallet,
    template: str,
    *,
    depth: int = DEFAULT_DEPTH,
    source: str = "auto",
    strict_message: bool = True,
    max_index: int = 1000,
    utxo_mode: str = "witness",
    progress=None,
    skip_addresses: set[str] | None = None,
    addresses: list[str] | None = None,
) -> tuple[Snapshot, dict[str, object]]:
    """Build the snapshot and the unsigned PSBTs; returns (snapshot, {address: BIP322PSBT}).

    ``progress`` is an optional callable given a line of text before slow steps.
    ``skip_addresses`` leaves out addresses that earlier bundles already prove
    (see :func:`bip322audit.ledger.proven_addresses`).  ``addresses`` proves
    exactly these addresses of the wallet, whether or not they hold coins yet
    (a change address before the spend is broadcast, a deposit address before
    the deposit); their coins confirmed at the stamp, if any, are listed.

    The stamp, the coins, their heights and block hashes are all read under
    one tip: if a block arrives meanwhile, everything is read again.

    Raises ``ValueError`` for a bad template, message or source and for an
    address that is not the wallet's; ``RpcError`` when the node fails, is on
    another network than the descriptor, keeps moving its tip, or lists no
    coins to prove.
    """
    compose_message(template, Stamp(0, "0" * 64, "1970-01-01T00:00:00Z"))  # a bad template fails here, before the node is asked anything
    chain = cli.chain()
    wallet = _wallet_on_chain(wallet, chain)
    policy = f"{wallet.threshold} of {len(wallet.cosigners)}"
    source, node_wallet = _choose_source(cli, source, progress)
    tip_height, stamp, message, coins, foreign = _read_under_one_tip(
        cli, wallet, template, source, depth=depth, strict_message=strict_message, max_index=max_index, progress=progress
    )
    # recorded in snapshot.json and shown by the command: what the node listed and the bundle does not, and why
    why = f"the node lists them but this descriptor does not derive them (another descriptor of the node wallet, or an index beyond {max_index})"
    left_out = {"other_descriptors": {**foreign, "amount_btc": btc(foreign["amount_sat"]), "why": why}}
    if addresses:
        coins = _only_addresses(wallet, coins, addresses, max_index)
        source = f"{source}, addresses given"
    skipped = 0
    if skip_addresses:
        coins, left_out["proven_earlier"] = _without_proven(coins, skip_addresses)
        skipped = left_out["proven_earlier"]["outputs"]
    if not coins:
        what = "no coins of this wallet" if not skipped else f"no coins of this wallet beyond the {skipped} on already proven addresses"
        raise RpcError(f"{what} confirmed at block {stamp.height} (source {source})")
    entries, psbts = _address_entries(wallet, coins, message, utxo_mode)
    snapshot = Snapshot(
        created_utc=iso_utc(),
        chain=chain,
        tip_height=tip_height,
        stamp=stamp,
        message=message,
        wallet_descriptor=wallet.to_descriptor(),
        policy=policy,
        addresses=entries,
        source=source,
        node_wallet=node_wallet,
        skipped_proven=skipped,
        left_out=left_out,
    )
    return snapshot, psbts


def _wallet_on_chain(wallet: Wallet, chain: str) -> Wallet:
    """The wallet in the node's address encoding; a descriptor for another network is refused."""
    network = network_of(chain)
    if wallet.network == "test" and network in ("regtest", "signet"):
        # tpub keys are shared by every test chain; the node says which one this is
        wallet = Wallet.from_descriptor(wallet.to_descriptor(), network=network, name=wallet.name)
    if wallet.network != network:
        raise RpcError(
            f"the node is on chain {chain!r} but the descriptor's keys are for network {wallet.network!r}: wrong node or wrong descriptor"
        )
    return wallet


def _choose_source(cli: BitcoinCli, source: str, progress) -> tuple[str, str | None]:
    """``auto`` resolved to ``listunspent`` or ``scantxoutset``, and the node wallet's name when there is one to ask."""
    node_wallet = None
    if source in ("auto", "listunspent"):
        # a node with exactly one wallet loaded answers listunspent without -rpcwallet; only fall
        # back to the (minutes-long) UTXO-set scan when the node has no wallet to ask
        try:
            node_wallet = str(cli.call("getwalletinfo")["walletname"])
            source = "listunspent"
        except RpcError as exc:
            if source == "listunspent":
                raise
            if progress:
                progress(f"no wallet available ({exc}); scanning the UTXO set for the descriptor instead")
            source = "scantxoutset"
    if source not in ("listunspent", "scantxoutset"):
        raise ValueError("source must be auto, listunspent or scantxoutset")
    return source, node_wallet


def _read_under_one_tip(
    cli: BitcoinCli, wallet: Wallet, template: str, source: str, *, depth: int, strict_message: bool, max_index: int, progress
) -> tuple[int, Stamp, str, list[AddressCoins], dict]:
    """Tip height, stamp, message, coins and the count of foreign outputs, all as of one tip: read again if a block arrives meanwhile."""
    for _ in range(TIP_RETRIES):
        tip_height, tip_hash = cli.tip()
        stamp = fetch_stamp(cli, depth, tip_height=tip_height)
        message = compose_message(template, stamp)
        lint = lint_message(message.encode("utf-8"))
        if lint and strict_message:
            raise ValueError("a hardware signer may refuse this message: " + "; ".join(lint))
        foreign: dict = {"outputs": 0, "amount_sat": 0}
        if source == "listunspent":
            coins = coins_from_listunspent(cli, wallet, stamp, tip_height, max_index=max_index, left_out=foreign)
        else:
            if progress:
                progress("scantxoutset: scanning the whole UTXO set for the wallet descriptor, this takes minutes on mainnet")
            coins = coins_from_scantxoutset(cli, wallet, stamp, scan_range=max_index, left_out=foreign)
        if cli.tip() == (tip_height, tip_hash):
            return tip_height, stamp, message, coins, foreign  # nothing arrived in between: all of it belongs to this tip
        if progress:
            progress("the tip moved while the coins were being read; reading them again so that everything belongs to one tip")
    raise RpcError(f"the node's tip kept moving while the coins were being read ({TIP_RETRIES} attempts); try again")


def _only_addresses(wallet: Wallet, coins: list[AddressCoins], addresses: list[str], max_index: int) -> list[AddressCoins]:
    """Exactly the given addresses, in the order given and each once, with the coins found for them (none is fine)."""
    by_address = {c.derived.address: c for c in coins}
    find = cache(lambda address: wallet.find_address(address, max_index=max_index))  # an address given twice is searched for once
    chosen: list[AddressCoins] = []
    for address in addresses:
        derived = by_address[address].derived if address in by_address else find(address)
        if derived is None:
            raise ValueError(f"{address} is not an address of this wallet (within index {max_index})")
        if derived.address not in [c.derived.address for c in chosen]:
            chosen.append(by_address.get(derived.address) or AddressCoins(derived, []))
    return chosen


def _without_proven(coins: list[AddressCoins], skip_addresses: set[str]) -> tuple[list[AddressCoins], dict]:
    """The coins on addresses no earlier bundle proves, and the ``left_out`` entry for the rest."""
    kept = [c for c in coins if c.derived.address not in skip_addresses]
    skipped = sum(len(c.utxos) for c in coins) - sum(len(c.utxos) for c in kept)
    amount = sum(c.total_sat for c in coins) - sum(c.total_sat for c in kept)
    why = "on addresses that an earlier bundle already proves (--skip-proven)"
    return kept, {"outputs": skipped, "amount_sat": amount, "amount_btc": btc(amount), "why": why}


def _address_entries(wallet: Wallet, coins: list[AddressCoins], message: str, utxo_mode: str) -> tuple[list[dict], dict[str, object]]:
    """Per address the ``snapshot.json`` entry and the unsigned PSBT, numbered in the order of ``coins``."""
    message_bytes = message.encode("utf-8")
    psbts: dict[str, object] = {}
    entries: list[dict] = []
    for c in coins:
        psbt = create_psbt(c.derived, message_bytes, xpubs=wallet.global_xpubs(), utxo_mode=utxo_mode)
        psbts[c.derived.address] = psbt
        entries.append(
            {
                "address": c.derived.address,
                "branch": c.derived.branch,
                "index": c.derived.index,
                "file": psbt_file_name(len(entries) + 1),
                "utxos": [u.to_dict() for u in c.utxos],
                "total_sat": c.total_sat,
                "to_sign_txid": psbt.tx.txid().hex(),
            }
        )
    return entries, psbts


def psbt_file_name(sequence: int) -> str:
    """``to_sign/to_sign-01.psbt``: what the file is (the BIP-322 to_sign transaction to sign), short on a device screen.

    The address is not in the name: it lives in ``snapshot.json``.
    """
    return f"to_sign/to_sign-{sequence:02d}.psbt"


def write_bundle(directory: Path, snapshot: Snapshot, psbts: dict[str, object]) -> list[Path]:
    """``snapshot.json``, ``message.txt``, ``to_sign/`` with one PSBT per address, an empty ``signed/``; returns the written paths.

    Writing into an earlier bundle's directory replaces these files only, and removes the
    ``to_sign-NN.psbt`` of the earlier run that the new one does not have (they would be for another message).
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    path = directory / "snapshot.json"
    write_text_atomic(path, json.dumps(snapshot.to_dict(), indent=2) + "\n")
    written.append(path)
    path = directory / "message.txt"
    path.write_bytes(snapshot.message.encode("utf-8"))  # exact bytes, no trailing newline
    written.append(path)
    names = {a["address"]: a["file"] for a in snapshot.addresses}
    for stale in sorted((directory / "to_sign").glob("to_sign-*.psbt")):
        if f"to_sign/{stale.name}" not in names.values():
            stale.unlink()
    for address, psbt in psbts.items():
        path = directory / names.get(address, f"to_sign/{address}.psbt")
        path.parent.mkdir(exist_ok=True)
        path.write_text(psbt.to_string() + "\n")
        written.append(path)
    (directory / "signed").mkdir(exist_ok=True)
    return written


def load_snapshot(directory: Path) -> dict:
    """``snapshot.json`` of a bundle directory, as written by :func:`write_bundle`."""
    return json.loads((directory / "snapshot.json").read_text())
