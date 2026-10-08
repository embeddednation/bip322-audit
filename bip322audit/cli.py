"""``bip322-audit``: snapshot, prove, finalize, verify, holdings (and stamp, help).

Exit codes, for every command: 0 done (for ``verify``: the result is OK), 2 could not
run (bad input, a file or the node not reachable).  ``verify`` alone also has 1, the
result is FAILED, and 3, the result is INCOMPLETE: nothing failed, but the node did
not show every listed output held at the stamp block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bip322core._version import SPEC
from bip322core.cli import CLIError, add_help_command, emit
from bip322core.core import BIP322Error
from bip322core.wallet import Wallet, wallet_from_file

from . import TOOL
from .audit import EXIT_CODES, AuditError, finalize_bundle, format_report, load_proofs, verify_proofs
from .holdings import format_holdings, holdings
from .ledger import proven_addresses
from .rpc import BitcoinCli, RpcError, btc, network_of
from .snapshot import (
    DEFAULT_DEPTH,
    Snapshot,
    check_wallet_against_node,
    descriptor_kind,
    load_snapshot,
    take_snapshot,
    wallet_from_node,
    write_bundle,
    write_text_atomic,
)
from .stamp import fetch_stamp

DEFAULT_TEMPLATE = "Proof of control {date}"
EXIT_HELP = "Exit codes: 0 done, 2 could not run (bad input, a file or the node not reachable)."


def _opt(args, name: str):
    """A node option given after the subcommand wins over the same option given before it."""
    after = getattr(args, f"{name}_sub", None)
    return after if after is not None else getattr(args, name, None)  # "" is a value: Core's default wallet is named ""


def _cli(args, wallet: str | None = None) -> BitcoinCli:
    """The node as the options describe it; ``wallet`` is the node wallet to use when no -w was given."""
    cli = BitcoinCli(_opt(args, "cli") or "bitcoin-cli")
    chosen = _opt(args, "wallet")
    wallet = chosen if chosen is not None else wallet
    if wallet is not None:
        cli.argv.append(f"-rpcwallet={wallet}")
    if _opt(args, "timeout") is not None:
        cli.timeout = float(_opt(args, "timeout"))
    return cli


def _add_node_args(p: argparse.ArgumentParser, wallet: bool = True) -> None:
    p.add_argument("--cli", dest="cli_sub", metavar="CMD", help="how to reach the node (may also be given before the command)")
    p.add_argument("--timeout", dest="timeout_sub", type=float, metavar="SECONDS", help="give up on a node call after this long")
    if wallet:
        p.add_argument("--wallet", "-w", dest="wallet_sub", metavar="NAME", help="the node wallet (may also be given before the command)")


def _wallet(args, cli: BitcoinCli, notes: list[str]) -> Wallet:
    """The wallet: from --descriptor (file or text, cross-checked against the node) or from the node wallet itself.

    ``notes`` collects what the owner must be told about the choice (descriptors of the node wallet that are not covered).
    """
    chain = cli.chain()
    network = network_of(chain)
    if args.descriptor:
        text = args.descriptor
        wallet = wallet_from_file(text, network=network) if Path(text).is_file() else Wallet.from_descriptor(text, network=network)
        check_wallet_against_node(cli, wallet)
        return wallet
    return wallet_from_node(cli, chain, progress=notes.append)


def _progress(line: str) -> None:
    print(line, file=sys.stderr)


def _bundle_directory(output: str | None, force: bool, parent: Path, name: str) -> Path:
    """Where the new bundle goes: ``output`` when given, else ``name`` under ``parent``.

    A default name never lands in an existing bundle: two runs within one block get two directories.
    """
    if output:
        directory = Path(output)
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            _check_may_rewrite(directory, force)
        return directory
    directory, n = parent / name, 1
    while directory.exists():
        n += 1
        directory = parent / f"{name}-{n}"
    return directory


def _check_may_rewrite(directory: Path, force: bool) -> None:
    """Refuse to write a bundle into a directory that holds something, unless --force and nothing signed is in it."""
    if not force:
        raise CLIError(f"{directory} exists and is not empty (--force rewrites a bundle that has not been signed yet)")
    # --force replaces snapshot.json, message.txt and to_sign/ only; signatures and proofs are for the old message
    if (directory / "proofs.json").exists():
        raise CLIError(f"{directory} holds a proofs.json: a finalized bundle is not written over; use a new directory")
    if (directory / "signed").is_dir() and any((directory / "signed").iterdir()):
        raise CLIError(f"{directory}/signed holds signed PSBTs: they are not written over; use a new directory")


def _joined_ledger(skip_proven: list[str] | None) -> Path:
    """Where a bundle goes by default: into the one ledger it was checked against, else the current directory."""
    if skip_proven and len(skip_proven) == 1 and Path(skip_proven[0]).is_dir():
        return Path(skip_proven[0])  # a new bundle joins the ledger it was checked against
    return Path()


def _proven(skip_proven: list[str] | None) -> set[str] | None:
    """The addresses that bundles under the --skip-proven directories already prove; None when the option was not given."""
    if not skip_proven:
        return None
    for root in skip_proven:
        if not Path(root).exists():  # a mistyped ledger must not quietly mean "nothing is proven yet"
            raise CLIError(f"--skip-proven {root}: does not exist")
    unreadable: list = []
    proven = proven_addresses(skip_proven, skipped=unreadable)
    for path, reason in unreadable:
        print(f"warning: {path} is not counted as proving anything: {reason}", file=sys.stderr)
    return proven


def cmd_stamp(args) -> int:
    print(fetch_stamp(_cli(args), args.depth).line())
    return 0


def cmd_snapshot(args) -> int:
    """Every funded address of the wallet, except those ``--skip-proven`` finds proven; the coins come from ``--source``."""
    return _new_bundle(args, source=args.source, addresses=None, skip_proven=args.skip_proven, ledger=None)


def cmd_prove(args) -> int:
    """Exactly the addresses given, funded or not: nothing is skipped, the node wallet is asked when there is one, ``--ledger`` says where."""
    return _new_bundle(args, source="auto", addresses=args.addresses, skip_proven=None, ledger=args.ledger)


def _new_bundle(args, *, source: str, addresses: list[str] | None, skip_proven: list[str] | None, ledger: str | None) -> int:
    """Take the snapshot, write the bundle, say what was written: all that ``snapshot`` and ``prove`` share."""
    proven = _proven(skip_proven)
    cli = _cli(args)
    notes: list[str] = []
    wallet = _wallet(args, cli, notes)
    snapshot, psbts = take_snapshot(
        cli,
        wallet,
        args.text,
        depth=args.depth,
        source=source,
        strict_message=not args.allow_any_message,
        max_index=args.max_index,
        utxo_mode=args.utxo,
        progress=_progress,
        skip_addresses=proven,
        addresses=addresses,
    )
    parent = Path(ledger) if ledger else _joined_ledger(skip_proven)
    directory = _bundle_directory(args.output, args.force, parent, f"snapshot-{snapshot.stamp.time[:10]}-{snapshot.stamp.height}")
    write_bundle(directory, snapshot, psbts)
    print(json.dumps(_bundle_summary(directory, snapshot, notes), indent=2), file=sys.stderr)
    print(str(directory))
    return 0


def _bundle_summary(directory: Path, snapshot: Snapshot, notes: list[str]) -> dict:
    """What the owner is shown on stderr after a bundle is written; the directory alone goes to stdout."""
    return {
        "directory": str(directory),
        "stamp": snapshot.stamp.to_dict(),
        "source": snapshot.source,
        "policy": snapshot.policy,
        "descriptor": f"{descriptor_kind(snapshot.wallet_descriptor)} {snapshot.policy}",  # which family was chosen
        "notes": notes,
        "addresses": len(snapshot.addresses),
        "utxos": sum(len(a["utxos"]) for a in snapshot.addresses),
        "skipped_proven": snapshot.skipped_proven,
        "left_out": snapshot.left_out,
        "total_sat": snapshot.total_sat,
        "total_btc": btc(snapshot.total_sat),
        "message": snapshot.message,
        "psbts": {a["file"]: f"{a['address']} ({btc(a['total_sat'])} BTC)" for a in snapshot.addresses},
    }


def cmd_holdings(args) -> int:
    result = holdings(_cli(args), args.targets, at=args.at)
    emit(json.dumps(result, indent=2) if args.json else format_holdings(result), args.output)
    return 0


def cmd_finalize(args) -> int:
    directory = Path(args.directory)
    cli = _spend_history_node(args, load_snapshot(directory))
    out = Path(args.output) if args.output else directory / "proofs.json"
    previous = _recorded_earlier(out) if cli is None else None
    notes: list[str] = []
    try:
        document = finalize_bundle(directory, lenient=args.lenient, cli=cli, previous=previous, notes=notes)
    except RpcError as exc:
        raise RpcError(f"{exc}; pass --offline to write proofs.json without asking the node wallet") from exc
    for note in notes:
        print(note, file=sys.stderr)
    write_text_atomic(out, json.dumps(document, indent=2) + "\n")
    total = sum(p["total_sat"] for p in document["proofs"])
    summary = {
        "proofs": len(document["proofs"]),
        "total_sat": total,
        "total_btc": btc(total),
        "written": str(out),
        "outputs_spent_since_snapshot": len(document["spends"]) if document["spends"] is not None else "not recorded",
        "ignored_files": [n.removeprefix("ignored ") for n in notes if n.startswith("ignored ")],
    }
    print(json.dumps(summary, indent=2), file=sys.stderr)
    print(str(out))
    return 0


def _spend_history_node(args, snapshot: dict) -> BitcoinCli | None:
    """The node wallet finalize asks for the spend history; None, with a line saying why, when it is not asked."""
    node_wallet = _opt(args, "wallet") if _opt(args, "wallet") is not None else snapshot.get("node_wallet")
    if args.offline:
        print(
            "offline: the node wallet is not asked for the spend history; outputs spent since cannot be shown held at the stamp",
            file=sys.stderr,
        )
        return None
    if node_wallet is None:  # None only: Core's default wallet is named ""
        print("no node wallet known for this snapshot (it came from a UTXO-set scan); the spend history is not recorded", file=sys.stderr)
        return None
    cli = _cli(args, wallet=str(node_wallet))
    try:
        cli.call("getwalletinfo")
    except (RpcError, OSError) as exc:
        raise CLIError(
            f"cannot reach the node wallet for the spend history ({exc}); pass --offline to write proofs.json without it"
        ) from exc
    return cli


def _recorded_earlier(out: Path) -> dict | None:
    """The proofs document an earlier run wrote to ``out``, if any: it may have recorded spends, and a run without the node keeps them."""
    if not out.is_file():
        return None
    try:
        previous = load_proofs(out)
    except AuditError as exc:
        print(f"warning: the existing {out} is not readable, so no recorded spends are kept ({exc})", file=sys.stderr)
        return None
    return previous if isinstance(previous, dict) else None


def cmd_verify(args) -> int:
    document = load_proofs(Path(args.proofs))
    cli = None if args.offline else _cli(args)
    engines = args.engines.split(",") if args.engines else None
    report = verify_proofs(document, cli, engines=engines, txindex=args.txindex)
    if args.report:
        write_text_atomic(Path(args.report), json.dumps(report, indent=2, default=str) + "\n")
    emit(json.dumps(report, indent=2, default=str) if args.json else format_report(report), args.output)
    return EXIT_CODES[report["result"]]


def _add_global_args(parser: argparse.ArgumentParser) -> None:
    """The node options that may be given before the command; :func:`_add_node_args` adds them after it too."""
    parser.add_argument(
        "--cli",
        default=None,
        metavar="CMD",
        help='how to reach the node, e.g. "bitcoin-cli -signet" or "bitcoin-cli -rpcconnect=10.0.0.5" (default: bitcoin-cli). '
        "CMD is split like a shell command line, so quote a path that contains spaces inside it: "
        """--cli "'/opt/my node/bitcoin-cli' -signet\"""",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="give up on a node call after this long (default 600; a UTXO-set scan the tool gave up on keeps running on the node)",
    )
    parser.add_argument(
        "--wallet",
        "-w",
        default=None,
        metavar="NAME",
        help="the node wallet (bitcoin-cli -rpcwallet=NAME); required when several are loaded. Its descriptor is read from the node. May also follow the command.",
    )


def _add_wallet_and_message_args(p: argparse.ArgumentParser, *, descriptor: str, text: str, depth: str) -> None:
    """``--descriptor``, ``--text`` and ``--depth``, the same for snapshot and prove; each words the help for its own case."""
    p.add_argument("--descriptor", "-d", metavar="FILE|DESC", help=descriptor)
    p.add_argument("--text", default=DEFAULT_TEMPLATE, help=text)
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help=depth)


def _add_psbt_args(p: argparse.ArgumentParser, *, max_index: str) -> None:
    """``--max-index``, ``--utxo`` and ``--allow-any-message``, the same for snapshot and prove."""
    p.add_argument("--max-index", type=int, default=1000, help=max_index)
    p.add_argument("--utxo", choices=["witness", "both"], default="witness", help="UTXO fields to embed in the PSBTs")
    p.add_argument(
        "--allow-any-message", action="store_true", help="do not insist on the display rules of hardware signers (bip322 lint-message)"
    )


def _add_destination_args(p: argparse.ArgumentParser, *, output: str) -> None:
    """``--output`` and ``--force``, the same for snapshot and prove."""
    p.add_argument("--output", "-o", metavar="DIR", help=output)
    p.add_argument("--force", action="store_true", help="with -o: rewrite a bundle that holds no signed PSBTs and no proofs.json yet")


def _add_stamp_parser(sub) -> None:
    p = sub.add_parser(
        "stamp",
        help="print the block stamp line for a message",
        description="Print `block: HEIGHT HASH TIME` for the block DEPTH blocks behind the node's tip. No wallet is needed.",
    )
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help=f"blocks behind the tip (default {DEFAULT_DEPTH})")
    _add_node_args(p, wallet=False)
    p.set_defaults(func=cmd_stamp, examples=["stamp", "--cli 'bitcoin-cli -signet' stamp --depth 3"])


def _add_snapshot_parser(sub) -> None:
    p = sub.add_parser(
        "snapshot",
        help="stamp, funded addresses, message and one PSBT per address into a directory",
        description=(
            "Take the snapshot: read the wallet descriptor from the node wallet (or --descriptor), choose the stamp block "
            "(tip - DEPTH), find the wallet's coins confirmed at that block "
            "(listunspent on the node wallet, or a scantxoutset of the descriptor), compose the message from the template "
            "plus the stamp line, and write snapshot.json, message.txt and to_sign-NN.psbt per funded address (the address of each is in snapshot.json). "
            "Sign the PSBTs on the cosigners' devices and put the results in <dir>/signed/."
        ),
    )
    _add_node_args(p)
    _add_wallet_and_message_args(
        p,
        descriptor="use this descriptor (a file or the text) instead of the node wallet's own; it must be one of the wallet's descriptors",
        text="message template; {date} {time} {height} {hash} are filled from the stamp block (default: '%(default)s')",
        depth=f"stamp/snapshot block is this many blocks behind the tip (default {DEFAULT_DEPTH})",
    )
    p.add_argument(
        "--source",
        choices=["auto", "listunspent", "scantxoutset"],
        default="auto",
        help="where to find the coins (auto: the node's wallet via listunspent when one is loaded, else a scantxoutset of the descriptor)",
    )
    _add_psbt_args(p, max_index="derivation range to consider per branch (default %(default)s)")
    p.add_argument(
        "--skip-proven",
        metavar="DIR",
        action="append",
        help="leave out addresses that a proofs.json under DIR (a ledger of earlier bundles) already proves; may repeat. "
        "With one DIR and no -o, the new bundle is written into it",
    )
    _add_destination_args(p, output="bundle directory (default snapshot-<date>-<height>, with -2, -3 ... if that exists)")
    p.set_defaults(
        func=cmd_snapshot,
        examples=[
            "-w treasury snapshot --text 'Annual audit {date}'",
            "--cli 'bitcoin-cli -signet' -w watch snapshot",
            "snapshot -d wallet.desc --source scantxoutset -o audit-2026",
            "-w treasury snapshot --text 'Proof of control {date}' --skip-proven ledger   # only addresses no earlier bundle proves",
        ],
    )


def _add_holdings_parser(sub) -> None:
    p = sub.add_parser(
        "holdings",
        help="what outputs (TXID:VOUT) or addresses hold now; with --at, whether that was held at a block",
        description=(
            "By output, a direct lookup (gettxout): the address, the amount, the block it was confirmed in, unspent or not. "
            "By address, a scan of the whole UTXO set (scantxoutset; minutes on mainnet; no wallet, no index needed either way). "
            "With --at HEIGHT|HASH an output confirmed at or before that block and unspent now was held at that block, which is "
            "how a reader checks a statement's holdings at its closing block. Coins spent since cannot show here; the owner's "
            "records name them."
        ),
    )
    _add_node_args(p, wallet=False)
    p.add_argument("targets", metavar="TXID:VOUT|ADDRESS", nargs="+")
    p.add_argument("--at", metavar="HEIGHT|HASH", help="the block the statement is about")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("--output", "-o", metavar="FILE", help="write here instead of stdout")
    p.set_defaults(func=cmd_holdings, examples=["holdings 7a1b...:0 3c9d...:1 --at 912345", "holdings bc1q... --json"])


def _add_prove_parser(sub) -> None:
    p = sub.add_parser(
        "prove",
        help="a bundle for given addresses of the wallet, whether or not they hold coins yet",
        description=(
            "Like snapshot, for exactly the addresses given: a stamp, the message, one PSBT per address, and the coins each holds "
            "at the stamp block if any. For a change address before the spend is broadcast, or a deposit address before the "
            "deposit: the proof shows the wallet controls the address, and later outputs to it are covered by it. "
            "An address that is not the wallet's (within --max-index) is refused, which is the point of checking before sending."
        ),
    )
    _add_node_args(p)
    p.add_argument("addresses", metavar="ADDRESS", nargs="+", help="addresses of the wallet")
    _add_wallet_and_message_args(
        p,
        descriptor="use this descriptor instead of the node wallet's own",
        text="message template (default: '%(default)s')",
        depth=f"stamp block is this many blocks behind the tip (default {DEFAULT_DEPTH})",
    )
    _add_psbt_args(p, max_index="derivation range to search per branch (default %(default)s)")
    p.add_argument("--ledger", metavar="DIR", help="write the bundle into this ledger directory (as snapshot-<date>-<height>)")
    _add_destination_args(p, output="bundle directory (default snapshot-<date>-<height>, under --ledger if given)")
    p.set_defaults(
        func=cmd_prove,
        examples=["-w treasury prove bc1q... --text 'Proof of control {date}' --ledger ledger"],
    )


def _add_finalize_parser(sub) -> None:
    p = sub.add_parser(
        "finalize",
        help="combine and finalize the signed PSBTs of a bundle into proofs.json",
        description=(
            "Read every PSBT in <dir>/to_sign/, <dir>/signed/ and <dir> itself, group them by address, combine, finalize, self-verify, "
            "and write proofs.json; files in to_sign/ and signed/ that were not used are listed. Unless --offline, also ask the node wallet the coins came from (recorded in snapshot.json, or -w) which "
            "listed outputs have been spent since the snapshot and record the spending transactions: a spend confirmed after the "
            "stamp block lets the auditor show the output was unspent at the snapshot. Re-run finalize before handing proofs.json "
            "over if coins have moved. With --offline the spends an earlier run recorded are kept. " + EXIT_HELP
        ),
    )
    p.add_argument("directory", metavar="DIR", help="the snapshot bundle directory")
    _add_node_args(p)
    p.add_argument("--offline", action="store_true", help="do not ask the node wallet for the spend history")
    p.add_argument("--lenient", action="store_true", help="skip invalid partial signatures instead of failing")
    p.add_argument("--output", "-o", metavar="FILE", help="proofs file (default DIR/proofs.json)")
    p.set_defaults(
        func=cmd_finalize,
        examples=[
            "finalize snapshot-2026-09-14-912345",
            "finalize snapshot-2026-09-14-912345 --offline",
            "-w treasury finalize snapshot-2026-09-14-912345",
        ],
    )


def _add_verify_parser(sub) -> None:
    p = sub.add_parser(
        "verify",
        help="auditor side: verify proofs.json against the node and report",
        description=(
            "Verify every BIP-322 signature, check the stamp block with getblockheader, check every listed output with "
            "gettxout (amount, address, creation height), and print a report. Outputs spent since the snapshot are fetched by "
            "their recorded block hash and, with the spends finalize recorded, shown to have been unspent at the snapshot. "
            "Exit codes: 0 the result is OK (signatures, stamp and document check out, and every listed output is shown held at the "
            "stamp block); 1 FAILED (one of those does not check out, or the node contradicts a listed output); 3 INCOMPLETE (nothing "
            "failed, but a listed output is spent and nothing shows when: the owner re-runs finalize); 2 verify could not run."
        ),
    )
    p.add_argument("proofs", metavar="PROOFS", help="proofs.json or the bundle directory")
    _add_node_args(p, wallet=False)
    p.add_argument("--offline", action="store_true", help="verify the signatures only (no node)")
    p.add_argument(
        "--txindex",
        action="store_true",
        help="also use -txindex on the node for outputs whose creating block is not recorded in the snapshot",
    )
    p.add_argument("--engines", default=None, help="comma separated bip322 engines (default: all installed)")
    p.add_argument("--json", action="store_true", help="print the JSON report instead of the summary")
    p.add_argument("--report", metavar="FILE", help="also write the JSON report here")
    p.add_argument("--output", "-o", metavar="FILE", help="write the printed output here instead of stdout")
    p.set_defaults(
        func=cmd_verify,
        examples=[
            "verify snapshot-2026-09-14-912345",
            "verify proofs.json --json --report audit-report.json",
            "verify proofs.json --offline",
        ],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bip322-audit",
        description=f"Proof of control of a wallet's coins at a point in time: BIP-322 proofs plus on-chain checks through bitcoin-cli ({SPEC}).",
        epilog=EXIT_HELP + " verify: 0 the result is OK, 1 FAILED, 3 INCOMPLETE (nothing failed, but some listed output is not shown "
        "held at the stamp block).",
    )
    parser.add_argument("--version", action="version", version=f"{TOOL} ({SPEC})")
    _add_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    for add in (_add_stamp_parser, _add_snapshot_parser, _add_holdings_parser, _add_prove_parser, _add_finalize_parser, _add_verify_parser):
        add(sub)  # in the order the commands are listed by --help
    add_help_command("bip322-audit", sub, {"Workflow": ["stamp", "snapshot", "prove", "finalize", "verify", "holdings", "help"]})
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CLIError, BIP322Error, RpcError, AuditError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc.strerror or exc}: {exc.filename}" if getattr(exc, "filename", None) else f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
