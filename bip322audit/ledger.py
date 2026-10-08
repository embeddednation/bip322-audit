"""A ledger: any directory tree that holds bundles written by ``snapshot`` and ``finalize``.

Bundles accumulate over time (one per audit, one per batch of newly created
outputs); the ledger is just where they are kept.  Nothing here talks to a
node.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

__all__ = ["Outpoint", "addresses_in", "find_proofs", "outpoints_in", "proven_addresses", "proven_outpoints"]

Outpoint = tuple[str, int]


def find_proofs(roots: Iterable[Path | str], *, max_depth: int = 3, skipped: list | None = None) -> list[tuple[Path, dict]]:
    """Every ``proofs.json`` under the roots, oldest stamp first.

    A root may be a ledger directory, a single bundle directory, or a
    ``proofs.json`` file.  Files that do not parse as a proofs document are
    skipped; pass a list as ``skipped`` to learn which, as ``(path, reason)``
    (a truncated file must not silently mean "nothing proven").  Returns ``(path, document)`` pairs,
    each file once however many roots reach it, ordered by stamp height, then ``finalized_utc``, then
    path.  The documents are as read, not verified: a caller that needs them to stand runs ``verify_proofs``.
    """
    found: dict[Path, dict] = {}
    for root in roots:
        root = Path(root)
        candidates = [root] if root.is_file() else _walk(root, max_depth)
        for path in candidates:
            if path.name != "proofs.json":
                continue
            try:
                document = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                if skipped is not None:
                    skipped.append((path, f"not readable as JSON ({str(exc)[:60]})"))
                continue
            if isinstance(document, dict) and isinstance(document.get("proofs"), list) and document.get("stamp"):
                found[path.resolve()] = document
            elif skipped is not None:
                skipped.append((path, "not a proofs document (no proofs list or no stamp)"))
    return sorted(found.items(), key=lambda item: (int(item[1]["stamp"]["height"]), item[1].get("finalized_utc") or "", str(item[0])))


def _walk(root: Path, max_depth: int) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for path in sorted(root.iterdir()):
        if path.is_file():
            out.append(path)
        elif path.is_dir() and max_depth > 0 and not path.name.startswith("."):
            out.extend(_walk(path, max_depth - 1))
    return out


def outpoints_in(document: dict) -> set[Outpoint]:
    """Every output a proofs document lists, as ``(txid, vout)``."""
    return {(u["txid"], int(u["vout"])) for p in document.get("proofs", []) for u in p.get("utxos", [])}


def proven_outpoints(roots: Iterable[Path | str], *, skipped: list | None = None) -> set[Outpoint]:
    """Every output listed in any proofs document under the roots."""
    out: set[Outpoint] = set()
    for _, document in find_proofs(roots, skipped=skipped):
        out |= outpoints_in(document)
    return out


def addresses_in(document: dict) -> set[str]:
    """Every address a proofs document carries a proof for."""
    return {p["address"] for p in document.get("proofs", [])}


def proven_addresses(roots: Iterable[Path | str], *, skipped: list | None = None) -> set[str]:
    """Every address a proofs document under the roots carries a proof for.

    A BIP-322 proof is about an address: whoever made it controls every output
    paid to that address, before or after the proof.  So an address proven
    once needs no new proof for later outputs.
    """
    out: set[str] = set()
    for _, document in find_proofs(roots, skipped=skipped):
        out |= addresses_in(document)
    return out
