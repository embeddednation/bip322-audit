"""Finalize a snapshot bundle into proofs, and verify proofs against a node.

``proofs.json`` is the artifact handed to the auditor: the snapshot (stamp,
addresses, UTXOs, message) plus one BIP-322 signature per address.  ``verify``
re-checks every part of it: the signatures with ``bip322``, the stamp with
``getblockheader``, every listed output with ``gettxout``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from bip322core.core import BIP322Error
from bip322core.engines import available_engines
from bip322core.psbt import BIP322PSBT, combine_psbts, finalize_psbt, parse_psbt, signature_from_psbt
from bip322core.verify import verify_message

from . import TOOL
from .rpc import BitcoinCli, RpcError, btc, to_sat
from .snapshot import Utxo, load_snapshot
from .stamp import Stamp, check_stamp, iso_utc, parse_stamp

__all__ = [
    "EXIT_CODES",
    "RESULT_FAILED",
    "RESULT_INCOMPLETE",
    "RESULT_OK",
    "AuditError",
    "collect_psbts",
    "collect_spends",
    "finalize_bundle",
    "format_report",
    "load_proofs",
    "verify_proofs",
]


class AuditError(Exception):
    """A bundle that cannot be finalized, or a file that is not a proofs document; the message is one line (or one per address)."""


# verify's results: everything checked out / nothing failed but some listed outputs are not shown held at the
# stamp block / a signature, the stamp or the document failed, or the node contradicts a listed output
RESULT_OK, RESULT_INCOMPLETE, RESULT_FAILED = "ok", "incomplete", "failed"
# the exit code of verify for each result; 2 is "could not run" (bad input, no node) and is never a result
EXIT_CODES = {RESULT_OK: 0, RESULT_FAILED: 1, RESULT_INCOMPLETE: 3}
_POLICY_RE = re.compile(r"[0-9]+ of [0-9]+")
_TXID_RE = re.compile(r"[0-9a-f]{64}")


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #


def _scan_psbts(directory: Path) -> tuple[dict[str, list[tuple[Path, BIP322PSBT]]], list[str]]:
    """PSBTs by to_sign txid with the file each came from, and what in ``to_sign/`` and ``signed/`` was not usable."""
    groups: dict[str, list[tuple[Path, BIP322PSBT]]] = {}
    ignored: list[str] = []
    for folder in (directory, directory / "to_sign", directory / "signed"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            name = path.relative_to(directory).as_posix()
            is_psbt = path.suffix.lower() == ".psbt" and path.is_file()
            if folder == directory and not is_psbt:
                continue  # the root holds the bundle's own files and whatever else the owner keeps there
            if not is_psbt:
                ignored.append(f"{name}: not a .psbt file")
                continue
            try:
                psbt = parse_psbt(path.read_bytes())
            except Exception as exc:  # noqa: BLE001 - foreign files in the folder are reported, not fatal
                ignored.append(f"{name}: not a readable PSBT ({str(exc)[:80]})")
                continue
            groups.setdefault(psbt.tx.txid().hex(), []).append((path, psbt))
    return groups, ignored


def collect_psbts(directory: Path) -> dict[str, list[BIP322PSBT]]:
    """Every parseable PSBT in ``to_sign/`` and ``signed/`` (and the bundle root), grouped by to_sign txid."""
    return {txid: [psbt for _, psbt in found] for txid, found in _scan_psbts(directory)[0].items()}


def finalize_bundle(
    directory: Path,
    *,
    lenient: bool = False,
    engines=None,
    cli: BitcoinCli | None = None,
    previous: dict | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Combine and finalize the signed PSBTs of every address; return the proofs document.

    The document is what the auditor gets: the message, the stamp, and per
    address the proof and the outputs.  It is built field by field from a
    fixed list, so nothing about the wallet behind the addresses can go in
    (no descriptor, no derivation paths, no node wallet name, no file names),
    whatever ``snapshot.json`` holds; proofs are in address order, not in
    derivation order.  With ``cli`` (the node wallet the coins came from) the
    document also records which listed outputs have been spent since the
    snapshot and by what, so that a verifier can show they were unspent at
    the snapshot; re-running finalize refreshes that.  Without ``cli`` the
    spends of ``previous`` (the proofs document written earlier for the same
    message) are kept.  ``notes`` collects lines for the owner: files that
    were ignored, spends that were kept.

    Raises :class:`AuditError` when any address has no complete, valid ``smp``
    proof (one line per address); ``RpcError`` when ``cli`` is given and the
    node wallet cannot be asked.  Nothing is written: the caller writes the
    document.
    """
    snapshot = load_snapshot(directory)
    groups, ignored = _scan_psbts(directory)
    notes = notes if notes is not None else []
    message = snapshot["message"].encode("utf-8")
    wanted = {entry["to_sign_txid"] for entry in snapshot["addresses"]}
    for txid, found in groups.items():
        if txid not in wanted:
            ignored += [f"{path.relative_to(directory).as_posix()}: a PSBT for no address of this bundle" for path, _ in found]
    notes += [f"ignored {line}" for line in ignored]
    proofs = []
    missing = []
    for entry in snapshot["addresses"]:
        try:
            proofs.append(
                _finalize_address(directory, entry, groups.get(entry["to_sign_txid"], []), message, lenient=lenient, engines=engines)
            )
        except AuditError as exc:  # every address is tried, so that one run names everything that is missing
            missing.append(str(exc))
    if missing:
        raise AuditError("cannot finalize every address:\n  " + "\n  ".join(missing))
    proofs.sort(key=lambda p: p["address"])
    total = sum(p["total_sat"] for p in proofs)
    policy = (snapshot.get("wallet") or {}).get("policy")
    document = {
        "tool": TOOL,
        "chain": str(snapshot["chain"]),
        "stamp": Stamp.from_dict(snapshot["stamp"]).to_dict(),
        "message": snapshot["message"],
        "message_hex": message.hex(),
        "policy": policy if isinstance(policy, str) and _POLICY_RE.fullmatch(policy) else None,
        "finalized_utc": iso_utc(),
        "proofs": proofs,
        "total_sat": total,
        "total_btc": btc(total),
        "spends": None,
        "spends_utc": None,
    }
    if cli is not None:
        spends = collect_spends(cli, snapshot)
        document["spends"] = spends["spends"]
        document["spends_utc"] = spends["collected_utc"]
    elif previous and isinstance(previous.get("spends"), dict) and previous.get("message_hex") == document["message_hex"]:
        _keep_spends(document, previous, notes)
    return document


def _finalize_address(
    directory: Path, entry: dict, candidates: list[tuple[Path, BIP322PSBT]], message: bytes, *, lenient: bool, engines
) -> dict:
    """The proof of one snapshot address from the PSBTs found for it.

    An :class:`AuditError` says why there is none; it names the address and the files, because one bad file
    must be findable among many.
    """
    address, txid = entry["address"], entry["to_sign_txid"]
    if not candidates:
        raise AuditError(f"{address}: no PSBT found for to_sign {txid[:16]}...")
    files = ", ".join(path.relative_to(directory).as_posix() for path, _ in candidates)
    try:
        combined = combine_psbts([psbt for _, psbt in candidates])
        finalize_psbt(combined, strict=not lenient)
        signature = signature_from_psbt(combined)
    except BIP322Error as exc:
        raise AuditError(f"{address}: {exc} (files: {files})") from exc
    if not signature.startswith("smp"):
        # a full-format proof would carry the whole to_sign transaction; only simple proofs are handed over
        raise AuditError(f"{address}: the proof is not of the simple (smp) variant (files: {files})")
    result = verify_message(address, signature, message, engines=engines or available_engines())
    if not result.ok:
        raise AuditError(f"{address}: finalized proof does not verify: {result.reason} (files: {files})")
    utxos = [Utxo.from_dict(u).to_dict() for u in entry["utxos"]]
    return {"address": address, "utxos": utxos, "total_sat": sum(u["amount_sat"] for u in utxos), "signature": signature, "variant": "smp"}


def _keep_spends(document: dict, previous: dict, notes: list[str]) -> None:
    """Offline now, but an earlier run asked the node wallet: keep what it recorded for the listed outputs rather than erase it."""
    listed = {f"{u['txid']}:{u['vout']}" for p in document["proofs"] for u in p["utxos"]}
    kept = {k: v for k, v in previous["spends"].items() if k in listed and isinstance(v, dict)}
    document["spends"] = {k: {f: v.get(f) for f in ("spent_by", "blockhash", "height")} for k, v in kept.items()}
    document["spends_utc"] = previous.get("spends_utc") if isinstance(previous.get("spends_utc"), str) else None
    notes.append(f"kept the {len(kept)} spend(s) recorded {document['spends_utc'] or 'earlier'} (offline: not refreshed)")


# --------------------------------------------------------------------------- #
# spends: what the owner's wallet knows about outputs spent after the snapshot
# --------------------------------------------------------------------------- #


def collect_spends(cli: BitcoinCli, snapshot: dict) -> dict:
    """For every snapshot output spent since the stamp block, the spending transaction and its block.

    Uses the node wallet's own history (``listsinceblock`` from the stamp block),
    so it runs on the owner's side; the auditor then needs no address index:
    a spend confirmed *after* the stamp block proves the output was unspent at
    the stamp.

    ``snapshot`` is a loaded ``snapshot.json`` or a proofs document: either
    carries the stamp and the listed outputs.  Returns ``{"tool",
    "collected_utc", "stamp", "spends"}``; ``spends`` maps ``TXID:VOUT`` to
    ``{"spent_by", "blockhash", "height"}`` and is what goes into
    ``proofs.json``, with ``collected_utc`` as its ``spends_utc``.
    """
    stamp = Stamp.from_dict(snapshot["stamp"])
    entries = snapshot.get("proofs") or snapshot.get("addresses") or []
    wanted = {(u["txid"], int(u["vout"])) for p in entries for u in p["utxos"]}
    since = cli.call("listsinceblock", stamp.hash)
    spends: dict[str, dict] = {}
    seen: set[str] = set()
    for entry in since.get("transactions", []):
        txid = entry["txid"]
        if txid in seen or int(entry.get("confirmations", 0)) <= 0:
            continue
        seen.add(txid)
        tx = cli.call("gettransaction", txid, True, True)
        for vin in tx.get("decoded", {}).get("vin", []):
            key = (vin.get("txid"), int(vin.get("vout", -1)))
            if key in wanted:
                spends[f"{key[0]}:{key[1]}"] = {"spent_by": txid, "blockhash": tx["blockhash"], "height": int(tx["blockheight"])}
    return {"tool": TOOL, "collected_utc": iso_utc(), "stamp": stamp.to_dict(), "spends": spends}


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _need(condition: bool, what: str) -> None:
    if not condition:
        raise AuditError(f"not a proofs document: {what}")


def _is_hash(value) -> bool:
    return isinstance(value, str) and _TXID_RE.fullmatch(value) is not None


def _validate(document) -> None:
    """Refuse anything that is not shaped like a proofs document, in one line, before any of it is used."""
    _need(isinstance(document, dict), "the top level is not an object")
    _need(isinstance(document.get("proofs"), list), "'proofs' is missing or not a list")
    _validate_message(document.get("message_hex"), document.get("message"))
    stamp = document.get("stamp")
    _need(not stamp or (isinstance(stamp, dict) and {"height", "hash", "time"} <= set(stamp) and _is_int(stamp["height"])), "bad 'stamp'")
    _need(document.get("total_sat") is None or _is_int(document["total_sat"]), "'total_sat' is not an integer")
    for n, proof in enumerate(document["proofs"], 1):
        _validate_proof(n, proof)
    _validate_spends(document.get("spends"))


def _validate_message(hexed, text) -> None:
    _need(hexed is None or isinstance(hexed, str), "'message_hex' is not a string")
    _need(text is None or isinstance(text, str), "'message' is not a string")
    _need(bool(hexed) or text is not None, "it has neither 'message' nor 'message_hex'")
    if hexed:
        try:
            bytes.fromhex(hexed)
        except ValueError:
            _need(False, "'message_hex' is not hex")


def _validate_proof(n: int, proof) -> None:
    _need(isinstance(proof, dict), f"proof {n} is not an object")
    _need(isinstance(proof.get("address"), str) and isinstance(proof.get("signature"), str), f"proof {n} lacks 'address' or 'signature'")
    _need(isinstance(proof.get("utxos"), list), f"proof {n} ({proof['address']}) lacks the list 'utxos'")
    _need(proof.get("total_sat") is None or _is_int(proof["total_sat"]), f"proof {n}: 'total_sat' is not an integer")
    for u in proof["utxos"]:
        ok = (
            isinstance(u, dict)
            and _is_hash(u.get("txid"))
            and all(_is_int(u.get(k)) and u[k] >= 0 for k in ("vout", "amount_sat", "height"))
            and (u.get("blockhash") is None or _is_hash(u["blockhash"]))
        )
        _need(ok, f"proof {n} ({proof['address']}) lists an output without a proper txid, vout, amount_sat or height")


def _validate_spends(spends) -> None:
    _need(spends is None or isinstance(spends, dict), "'spends' is not an object")
    for key, spend in (spends or {}).items():
        # both go to the node as arguments: hashes, nothing else
        ok = isinstance(spend, dict) and all(_is_hash(spend.get(k)) for k in ("spent_by", "blockhash"))
        _need(ok, f"the spend recorded for {str(key)[:80]} lacks a proper 'spent_by' or 'blockhash'")


@dataclass
class _Run:
    """One run of verify: what it checks against, what it has counted so far, and what it already asked the node.

    Nothing here outlives the run.  ``cli`` is None offline, ``stamp`` is None when the message carries none;
    outputs are checked against the node only when both are there.
    """

    message: bytes
    engines: list[str]
    cli: BitcoinCli | None
    stamp: Stamp | None
    spends: dict  # TXID:VOUT -> the spend finalize recorded
    txindex: bool
    problems: list[str]  # what is wrong with the document itself; the report's "document_problems"
    seen_addresses: set[str] = field(default_factory=set)
    seen_outpoints: set[tuple[str, int]] = field(default_factory=set)
    all_proofs_ok: bool = True
    claimed: int = 0  # satoshis: listed / shown held at the stamp block / unspent now
    held: int = 0
    unspent: int = 0
    total_count: int = 0  # outputs: checked against the node / shown held / shown to have existed / contradicted
    verified_count: int = 0
    existed_count: int = 0
    contradictions: int = 0
    headers: dict[str, dict] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.cli is not None

    def header(self, block_hash: str) -> dict:
        """A block's header, asked for once per run: many outputs share a block, and ``gettxout`` answers at the same tip."""
        if block_hash not in self.headers:
            self.headers[block_hash] = self.cli.block_header(block_hash)
        return self.headers[block_hash]


def _creating_tx(run: _Run, utxo: dict):
    """The transaction that created the output: by recorded block hash (no index needed) or via -txindex."""
    if utxo.get("blockhash"):
        return run.cli.call("getrawtransaction", utxo["txid"], True, utxo["blockhash"]), run.header(utxo["blockhash"])
    if run.txindex:
        tx = run.cli.call("getrawtransaction", utxo["txid"], True)
        return tx, (run.header(tx["blockhash"]) if tx.get("blockhash") else None)
    return None, None


def _row(utxo: dict, status: str) -> dict:
    """A report row: the listed output's own fields by name, never the document's dict (it may carry anything)."""
    return {
        "txid": utxo["txid"],
        "vout": utxo["vout"],
        "amount_sat": utxo["amount_sat"],
        "height": utxo["height"],
        "blockhash": utxo.get("blockhash"),
        "status": status,
        "verified": False,
        "contradiction": False,
    }


def _check_utxo(run: _Run, address: str, utxo: dict) -> dict:
    """One listed output against the node.

    ``verified``: the node shows it was held at the stamp block: created at or
    before it with the claimed amount and address, and either unspent now or
    spent by a transaction (recorded in the document by finalize) confirmed
    after it.  ``contradiction``: the node shows something that disagrees with
    the claim.  Neither: the output is spent and nothing shows when
    (``spent_time_unknown``), or this node cannot say anything about it
    (``spent_or_unknown``).
    """
    row = _row(utxo, "unknown")
    out = run.cli.call("gettxout", utxo["txid"], int(utxo["vout"]), False)
    if out:
        return _check_unspent(run, row, out, address, utxo)
    row["status"] = "spent_or_unknown"
    return _check_spent(run, row, address, utxo)


def _check_unspent(run: _Run, row: dict, out, address: str, utxo: dict) -> dict:
    """In the UTXO set now: amount, address and creation height must be the listed ones, the height at or before the stamp.

    The creation height is counted back from the block ``gettxout`` itself answered at, not from a tip read at another moment.
    """
    if not isinstance(out, dict) or not out.get("bestblock"):
        raise RpcError("gettxout: the answer names no best block")
    created = int(run.header(out["bestblock"])["height"]) - int(out["confirmations"]) + 1
    row.update(
        {
            "status": "unspent",
            "node_amount_sat": to_sat(out["value"]),
            "node_address": out.get("scriptPubKey", {}).get("address"),
            "created_height": created,
            "confirmations": int(out["confirmations"]),
        }
    )
    matches = (
        row["node_amount_sat"] == utxo["amount_sat"]
        and row["node_address"] == address
        and created == utxo["height"]
        and created <= run.stamp.height
    )
    row["verified"] = matches
    row["contradiction"] = not matches
    if not matches:
        row["problem"] = "amount, address or creation height differs from the snapshot"
    return row


def _check_spent(run: _Run, row: dict, address: str, utxo: dict) -> dict:
    """Not in the UTXO set now: did the creating transaction make it as listed, and does a recorded spend say when it went?"""
    try:
        tx, header = _creating_tx(run, utxo)
    except RpcError as exc:
        row["note"] = f"cannot fetch the creating transaction: {exc}"
        return row
    if tx is None or header is None:
        row["note"] = "not in the UTXO set now; the snapshot carries no block hash for it and this node has no -txindex"
        return row
    if int(header.get("confirmations", 0)) <= 0:
        row["note"] = "not in the UTXO set now, and the block recorded as creating it is not in the main chain"
        return row
    created = int(header["height"])
    row["created_height"] = created
    outputs = tx.get("vout", [])
    out = outputs[utxo["vout"]] if utxo["vout"] < len(outputs) else None
    matches = (
        out is not None
        and to_sat(out["value"]) == utxo["amount_sat"]
        and out.get("scriptPubKey", {}).get("address") == address
        and created == utxo["height"]
        and created <= run.stamp.height
    )
    if not matches:
        row["status"] = "created_after_snapshot" if created > run.stamp.height else "mismatch"
        row["contradiction"] = True
        row["problem"] = "the creating transaction does not match the snapshot (amount, address or block)"
        return row
    # it existed at the stamp block and is spent now; only the spending transaction says on which side of the stamp
    row["status"] = "spent_time_unknown"
    row["existed_at_snapshot"] = True
    spend = run.spends.get(f"{utxo['txid']}:{utxo['vout']}")
    if not spend:
        row["note"] = (
            "existed at the snapshot block and is spent now; nothing shows when. Re-run bip322-audit finalize on the owner's node "
            "to record the spend and show the output was unspent at the snapshot"
        )
        return row
    return _check_recorded_spend(run, row, utxo, spend)


def _check_recorded_spend(run: _Run, row: dict, utxo: dict, spend: dict) -> dict:
    """The spend the document names: it must spend this output, in a main-chain block after the stamp block."""
    stamp = run.stamp
    try:
        spending = run.cli.call("getrawtransaction", spend["spent_by"], True, spend["blockhash"])
        spend_header = run.header(spend["blockhash"])
    except RpcError as exc:
        row["note"] = f"the document names the spend {spend['spent_by'][:16]}... but the node cannot fetch it: {exc}"
        return row
    spends_it = any(v.get("txid") == utxo["txid"] and int(v.get("vout", -1)) == utxo["vout"] for v in spending.get("vin", []))
    spend_height = int(spend_header["height"])
    if not spends_it:
        row["note"] = "the document names a spending transaction that does not spend this output"
        row["document_problem"] = f"the spend recorded for {utxo['txid'][:16]}...:{utxo['vout']} does not spend that output"
    elif int(spend_header.get("confirmations", 0)) <= 0:
        row["note"] = (
            f"the recorded spending block {spend['blockhash'][:16]}... is no longer in the main chain (a reorganisation); "
            "re-run bip322-audit finalize on the owner's node"
        )
    elif spend_height > stamp.height:
        row.update({"status": "spent_after_snapshot", "verified": True, "unspent_at_snapshot": True})
        row.update({"spent_by": spend["spent_by"], "spent_height": spend_height})
        row["note"] = f"spent at height {spend_height}, after the snapshot block {stamp.height}: it was unspent at the snapshot"
    else:
        row["status"] = "spent_before_snapshot"
        row["contradiction"] = True
        row["problem"] = f"spent at height {spend_height}, at or before the snapshot block {stamp.height}"
    return row


def verify_proofs(document: dict, cli: BitcoinCli | None, *, engines=None, txindex: bool = False) -> dict:
    """Verify a proofs document; ``cli=None`` verifies only what needs no node.

    ``result`` is ``ok``, ``incomplete`` (nothing failed, but some listed output is not shown
    held at the stamp block) or ``failed``.  ``ok`` is true only for ``ok``; ``result`` tells ``incomplete`` from ``failed``.

    The report is a dict of plain values with the keys ``tool``, ``verified_utc``, ``engines``, ``proofs`` (per
    address: ``address``, ``bip322``, ``utxos``, ``claimed_sat``, ``held_at_stamp_sat``, ``unspent_now_sat``),
    ``stamp``, ``node``, ``document_problems``, ``spends_recorded_utc``, ``totals``, ``summary``, ``result`` and
    ``ok``.  Its rows are built field by field from what was checked, never copied from the document.  Raises
    :class:`AuditError` for something that is not a proofs document or is for another chain than the node's,
    ``RpcError`` when the node cannot be asked.  ``EXIT_CODES[report["result"]]`` is the command's exit code.
    Within one call a block's header is asked for once.
    """
    _validate(document)
    engines = list(engines or available_engines())
    message = _signed_message(document)
    stamp = parse_stamp(message)
    problems: list[str] = []
    report: dict = {
        "tool": TOOL,
        "verified_utc": iso_utc(),
        "engines": engines,
        "proofs": [],
        "stamp": None,
        "node": None,
        "document_problems": problems,
    }
    if stamp is not None and document.get("stamp") and Stamp.from_dict(document["stamp"]) != stamp:
        problems.append("the stamp recorded in proofs.json differs from the stamp inside the signed message")
    if not document["proofs"]:
        problems.append("the document lists no proofs: there is nothing to verify")
    report["spends_recorded_utc"] = document.get("spends_utc") if isinstance(document.get("spends_utc"), str) else None
    if cli is not None:
        report["node"] = _node_on_chain(cli, document)
    report["stamp"] = _stamp_check(cli, stamp)

    run = _Run(
        message=message, engines=engines, cli=cli, stamp=stamp, spends=document.get("spends") or {}, txindex=txindex, problems=problems
    )
    for proof in document["proofs"]:
        report["proofs"].append(_verify_proof(run, proof))
    listed = sum(u["amount_sat"] for p in document["proofs"] for u in p["utxos"])
    if document.get("total_sat") is not None and document["total_sat"] != listed:
        problems.append(f"the document's 'total_sat' is {document['total_sat']} but the listed outputs add up to {listed}")

    stamp_ok = bool(report["stamp"] and report["stamp"].get("ok"))
    report["totals"] = _totals(run)
    report["summary"] = _summary(run, stamp_ok)
    report["result"] = _result(run, stamp_ok)
    report["ok"] = report["result"] == RESULT_OK  # strictly the verdict: a reader of this one key must not take incomplete for ok
    return report


def _signed_message(document: dict) -> bytes:
    """The bytes that were signed: ``message_hex`` when given, and then ``message`` must say the same."""
    message = bytes.fromhex(document["message_hex"]) if document.get("message_hex") else document["message"].encode("utf-8")
    if document.get("message_hex") and document.get("message") is not None and message != document["message"].encode("utf-8"):
        raise AuditError("proofs.json is inconsistent: 'message' and 'message_hex' differ")
    return message


def _node_on_chain(cli: BitcoinCli, document: dict) -> dict:
    """The report's ``node``; proofs for another chain than the node's are refused."""
    info = cli.info()  # chain and tip from one answer
    node = {"chain": info["chain"], "tip_height": int(info["blocks"]), "tip_hash": info["bestblockhash"]}
    if document.get("chain") and document["chain"] != info["chain"]:
        raise AuditError(f"proofs are for chain {document['chain']!r}, node is on {info['chain']!r}")
    return node


def _stamp_check(cli: BitcoinCli | None, stamp: Stamp | None) -> dict:
    """The report's ``stamp``."""
    if stamp is None:
        return {"ok": False, "error": "message carries no block stamp"}
    if cli is None:
        return {"stamp": stamp.to_dict(), "ok": None, "note": "not checked (offline)"}
    return check_stamp(cli, stamp)


def _verify_proof(run: _Run, proof: dict) -> dict:
    """The report row of one address: the signature's verdict and a row per listed output; adds its sums to the run's."""
    address = proof["address"]
    if address in run.seen_addresses:
        run.problems.append(f"{address} has more than one proof in the document")
    run.seen_addresses.add(address)
    verdict = verify_message(address, proof["signature"], run.message, engines=run.engines)
    row = {"address": address, "bip322": verdict.to_dict(), "utxos": [], "claimed_sat": 0, "held_at_stamp_sat": 0, "unspent_now_sat": 0}
    row["bip322"].pop("message_utf8", None)
    row["bip322"].pop("message_hex", None)
    run.all_proofs_ok &= verdict.ok
    for utxo in proof["utxos"]:
        row["utxos"].append(_verify_utxo(run, row, address, utxo))
    if proof.get("total_sat") is not None and proof["total_sat"] != sum(u["amount_sat"] for u in proof["utxos"]):
        run.problems.append(f"{address}: 'total_sat' is {proof['total_sat']} but the listed outputs add up to something else")
    run.claimed += row["claimed_sat"]
    run.held += row["held_at_stamp_sat"]
    run.unspent += row["unspent_now_sat"]
    return row


def _verify_utxo(run: _Run, row: dict, address: str, utxo: dict) -> dict:
    """The report row of one listed output; adds what it shows to the address's sums in ``row`` and to the run's counts."""
    outpoint = (utxo["txid"], utxo["vout"])
    if outpoint in run.seen_outpoints:  # shown once, counted once
        run.problems.append(f"the output {utxo['txid'][:16]}...:{utxo['vout']} is listed more than once")
        return _row(utxo, "duplicate")
    run.seen_outpoints.add(outpoint)
    row["claimed_sat"] += utxo["amount_sat"]
    if not run.online or run.stamp is None:
        return _row(utxo, "not checked (offline)" if not run.online else "not checked (no stamp)")
    checked = _check_utxo(run, address, utxo)
    if checked.get("document_problem"):
        run.problems.append(checked.pop("document_problem"))
    run.total_count += 1
    run.verified_count += int(checked["verified"])
    run.contradictions += int(checked["contradiction"])
    run.existed_count += int(checked["verified"] or bool(checked.get("existed_at_snapshot")))
    if checked["verified"]:
        row["held_at_stamp_sat"] += utxo["amount_sat"]
        if checked["status"] == "unspent":
            row["unspent_now_sat"] += checked["node_amount_sat"]
    return checked


def _totals(run: _Run) -> dict:
    online = run.online
    return {
        "claimed_sat": run.claimed,
        "claimed_btc": btc(run.claimed),
        "held_at_stamp_sat": run.held if online else None,
        "held_at_stamp_btc": btc(run.held) if online else None,
        "verified_unspent_sat": run.unspent if online else None,
        "verified_unspent_btc": btc(run.unspent) if online else None,
    }


def _unexplained(run: _Run) -> int:
    """Outputs checked against the node that it neither showed held at the stamp block nor contradicted."""
    return run.total_count - run.verified_count - run.contradictions


def _summary(run: _Run, stamp_ok: bool) -> dict:
    online = run.online
    shown = f"{run.verified_count}/{run.total_count}"
    return {
        "proofs_valid": run.all_proofs_ok,
        "stamp_ok": stamp_ok if online else None,
        "utxos_verified_at_snapshot": shown if online else None,
        "contradictions": run.contradictions if online else None,
        "all_utxos_still_unspent": (run.unspent == run.claimed) if online else None,
        "utxos_shown_unspent_at_snapshot": shown if online else None,
        "utxos_existed_at_snapshot": f"{run.existed_count}/{run.total_count}" if online else None,
        "utxos_unexplained": _unexplained(run) if online else None,
        "document_consistent": not run.problems,
    }


def _result(run: _Run, stamp_ok: bool) -> str:
    """The verdict.

    FAILED: a signature, the stamp or the document does not check out, or the node contradicts a listed output.
    INCOMPLETE: none of that, but the node did not show every listed output held at the stamp block; the verdict
    must not say more than the node showed.  Outputs spent after the stamp with a checked spend are fine.
    """
    if not run.all_proofs_ok or (run.online and not stamp_ok) or run.contradictions or run.problems:
        return RESULT_FAILED
    return RESULT_INCOMPLETE if run.online and _unexplained(run) else RESULT_OK


def _summary_rows(report: dict) -> list[tuple[str, str]]:
    """The checklist at the end of the text report: one row per thing verify checks."""
    s = report["summary"]
    proofs = report["proofs"]
    utxos = [u for p in proofs for u in p["utxos"]]
    online = report.get("node") is not None
    rows = [("signatures", f"{sum(1 for p in proofs if p['bip322']['state'] == 'valid')}/{len(proofs)} valid")]
    st = report.get("stamp") or {}
    if not online:
        rows.append(("stamp block", "not checked (no node)"))
    elif s["stamp_ok"]:
        rows.append(("stamp block", f"ok, {st.get('confirmations', '?')} confirmations"))
    else:
        rows.append(("stamp block", f"FAILED ({st.get('error') or 'mismatch'})"))
    problems = report.get("document_problems", [])
    rows.append(("document", "consistent" if not problems else f"{len(problems)} problem(s), listed above"))
    if online:
        rows.append(
            (
                "outputs at snapshot",
                f"{s['utxos_verified_at_snapshot']} shown held, {s['utxos_unexplained']} spent and not shown held, "
                f"{s['contradictions']} contradiction" + ("" if s["contradictions"] == 1 else "s"),
            )
        )
        still = sum(1 for u in utxos if u.get("status") == "unspent" and u.get("verified"))
        spent = sum(1 for u in utxos if str(u.get("status", "")).startswith("spent"))
        rows.append(("outputs now", f"{still}/{len(utxos)} still unspent" + (f", {spent} spent" if spent else "")))
    else:
        rows.append(("outputs", "not checked (no node)"))
    return rows


def _stamp_line(st: dict) -> str:
    if not st.get("stamp"):
        return "stamp: none"
    s = st["stamp"]
    status = "ok" if st.get("ok") else ("not checked" if st.get("ok") is None else f"FAILED ({st.get('error') or 'mismatch'})")
    confirmations = f", {st['confirmations']} confirmations" if st.get("confirmations") else ""
    return f"stamp: block {s['height']} {s['hash'][:16]}... {s['time']}  ->  {status}" + confirmations


def _utxo_line(u: dict) -> str:
    """``[ok]`` shown held at the stamp block, ``[!!]`` contradicted, ``[??]`` spent and not shown held, ``[--]`` not checked."""
    unexplained = u["status"] in ("spent_time_unknown", "spent_or_unknown")
    mark = "ok" if u.get("verified") else ("!!" if u.get("contradiction") else ("??" if unexplained else "--"))
    if u.get("problem"):
        detail = f"  ({u['problem']})"
    else:
        detail = f"  (spent at {u['spent_height']}, unspent at snapshot)" if u.get("unspent_at_snapshot") else ""
    return f"    [{mark}] {u['txid'][:16]}...:{u['vout']}  {btc(u['amount_sat']):>14} BTC  {u['status']}{detail}"


def _proof_lines(p: dict, online: bool) -> list[str]:
    lines = [f"{p['address']}  bip322: {p['bip322']['state'].upper()}"]
    lines += [_utxo_line(u) for u in p["utxos"]]
    shown = f", shown held at the stamp {btc(p['held_at_stamp_sat'])} BTC, unspent now {btc(p['unspent_now_sat'])} BTC" if online else ""
    lines.append(f"    claimed {btc(p['claimed_sat'])} BTC" + shown)
    return lines


def _totals_line(t: dict, online: bool) -> str:
    if not online:
        return f"totals: claimed {btc(t['claimed_sat'])} BTC"
    return (
        f"totals: claimed {btc(t['claimed_sat'])} BTC"
        f", shown held at the stamp block {btc(t['held_at_stamp_sat'])} BTC, verified unspent now {btc(t['verified_unspent_sat'])} BTC"
    )


def _result_line(report: dict) -> str:
    unexplained = report["summary"].get("utxos_unexplained") or 0
    detail = {
        RESULT_OK: "OK",
        RESULT_FAILED: "FAILED",
        RESULT_INCOMPLETE: f"INCOMPLETE ({unexplained} listed output(s) not shown held at the stamp block; nothing contradicts the claim)",
    }
    return "RESULT: " + detail[report["result"]]


def format_report(report: dict) -> str:
    """The text ``verify`` prints for a report of :func:`verify_proofs`.

    A header (tool, node, stamp), per address the signature's verdict and a line per listed output, the totals,
    the document's problems, the checklist, and last the line ``RESULT: OK``, ``RESULT: FAILED`` or
    ``RESULT: INCOMPLETE (...)``.  No trailing newline.
    """
    node = report.get("node")
    online = bool(node)
    lines = [f"{report['tool']}  verified {report['verified_utc']}  engines: {', '.join(report['engines'])}"]
    if node:
        lines.append(f"node: chain {node['chain']}, tip {node['tip_height']} {node['tip_hash'][:16]}...")
    lines.append(_stamp_line(report.get("stamp") or {}))
    lines.append("")
    for p in report["proofs"]:
        lines += _proof_lines(p, online)
    lines.append("")
    lines.append(_totals_line(report["totals"], online))
    lines += [f"!! document: {problem}" for problem in report.get("document_problems", [])]
    lines.append("")
    lines.append("summary")
    lines += [f"  {label:<20} {value}" for label, value in _summary_rows(report)]
    lines.append(_result_line(report))
    return "\n".join(lines)


def load_proofs(path: Path) -> dict:
    """The proofs document at ``path`` (a ``proofs.json`` or a bundle directory holding one), parsed and not yet checked.

    Raises :class:`AuditError` for a file that is not JSON, ``OSError`` for one that cannot be read.
    """
    path = path / "proofs.json" if path.is_dir() else path
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise AuditError(f"{path}: not valid JSON ({exc})") from exc
