"""``bip322-audit``: snapshot, prove, finalize, verify, holdings (and stamp, help).

Exit codes, for every command: 0 done (for ``verify``: the result is OK), 2 could not
run (bad input, a file or the node not reachable).  ``verify`` alone also has 1, the
result is FAILED, and 3, the result is INCOMPLETE: nothing failed, but the node did
not show every listed output held at the stamp block.

Everything is written under ``~/bip322/<wallet>/snapshots/`` (``BIP322_DIR`` moves
the tree); ``-w`` names the wallet, else ``config.toml`` does, else the node's one
loaded wallet is it.  ``-o`` writes elsewhere.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from bip322core._version import SPEC
from bip322core.cli import CLIError, add_help_command, emit
from bip322core.core import BIP322Error
from bip322core.wallet import Wallet, wallet_from_file

from . import TOOL
from .audit import EXIT_CODES, AuditError, finalize_snapshot, format_report, load_proofs, verify_proofs
from .datadir import (
    Config,
    ConfigError,
    data_dir,
    only_wallet,
    proofs_file_in,
    proofs_file_name,
    snapshot_directory,
    snapshot_name,
    snapshots_dir,
    wallet_of_directory,
)
from .holdings import format_holdings, holdings
from .proofs import proven_addresses
from .rpc import BitcoinCli, RpcError, btc, network_of
from .snapshot import (
    DEFAULT_DEPTH,
    Snapshot,
    check_wallet_against_node,
    descriptor_kind,
    load_snapshot,
    take_snapshot,
    wallet_from_node,
    write_snapshot,
    write_text_atomic,
)
from .stamp import fetch_stamp

DEFAULT_TEMPLATE = "Proof of control {date}"
EXIT_HELP = "Exit codes: 0 done, 2 could not run (bad input, a file or the node not reachable)."


def _opt(args, name: str):
    """A node option given after the subcommand wins over the same option given before it."""
    after = getattr(args, f"{name}_sub", None)
    return after if after is not None else getattr(args, name, None)  # "" is a value: Core's default wallet is named ""


def _wallet_name(args, config: Config) -> str | None:
    """The wallet as named by the owner: ``-w``, else ``wallet`` in config.toml; None when neither says."""
    given = _opt(args, "wallet")
    if given is not None:
        return str(given)
    named = config.get("wallet")
    return str(named) if named is not None else None


def _cli(args, config: Config, *, node_wallet: str | None = None, wallet: str | None = None) -> BitcoinCli:
    """The node as the options and config.toml describe it; ``node_wallet`` is passed as ``-rpcwallet`` when given."""
    cli = BitcoinCli(_opt(args, "cli") or config.get("cli", wallet) or "bitcoin-cli")
    if node_wallet is not None:
        cli.argv.append(f"-rpcwallet={node_wallet}")
    timeout = _opt(args, "timeout") if _opt(args, "timeout") is not None else config.get("timeout", wallet)
    if timeout is not None:
        cli.timeout = float(timeout)
    return cli


def _add_node_args(p: argparse.ArgumentParser, wallet: bool = True) -> None:
    p.add_argument("--cli", dest="cli_sub", metavar="CMD", help="how to reach the node (may also be given before the command)")
    p.add_argument("--timeout", dest="timeout_sub", type=float, metavar="SECONDS", help="give up on a node call after this long")
    if wallet:
        p.add_argument("--wallet", "-w", dest="wallet_sub", metavar="NAME", help="the wallet (may also be given before the command)")


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


def _snapshot_destination(output: str | None, force: bool, parent: Path | None, name: str) -> Path:
    """Where the new snapshot goes: ``output`` when given, else ``name`` under ``parent``.

    A default name never lands in an existing snapshot: two runs within one block get two directories.
    """
    if output:
        directory = Path(output)
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            _check_may_rewrite(directory, force)
        return directory
    if parent is None:
        raise CLIError("no wallet to file the snapshot under: name it with -w NAME (or wallet = NAME in config.toml), or give -o DIR")
    directory, n = parent / name, 1
    while directory.exists():
        n += 1
        directory = parent / f"{name}-{n}"
    return directory


def _check_may_rewrite(directory: Path, force: bool) -> None:
    """Refuse to write a snapshot into a directory that holds something, unless --force and nothing signed is in it."""
    if not force:
        raise CLIError(f"{directory} exists and is not empty (--force rewrites a snapshot that has not been signed yet)")
    # --force replaces snapshot.json, message.txt and to_sign/ only; signatures and proofs are for the old message
    if proofs_file_in(directory) is not None:
        raise CLIError(f"{directory} holds a proofs file: a finalized snapshot is not written over; use a new directory")
    if (directory / "signed").is_dir() and any((directory / "signed").iterdir()):
        raise CLIError(f"{directory}/signed holds signed PSBTs: they are not written over; use a new directory")


def _proven(wallet: str | None, skip_proven: bool) -> set[str] | None:
    """The addresses the wallet's earlier snapshots prove; None when --skip-proven was not given."""
    if not skip_proven:
        return None
    if wallet is None:
        raise CLIError("--skip-proven reads the wallet's snapshots: name the wallet with -w NAME (or wallet = NAME in config.toml)")
    unreadable: list = []
    proven = proven_addresses([snapshots_dir(wallet)], skipped=unreadable)
    for path, reason in unreadable:
        print(f"warning: {path} is not counted as proving anything: {reason}", file=sys.stderr)
    return proven


def cmd_stamp(args) -> int:
    print(fetch_stamp(_cli(args, Config.load()), args.depth).line())
    return 0


def cmd_snapshot(args) -> int:
    """Every funded address of the wallet, except those ``--skip-proven`` finds proven; the coins come from ``--source``."""
    return _new_snapshot(args, source=args.source, addresses=None, skip_proven=args.skip_proven)


def cmd_prove(args) -> int:
    """Exactly the addresses given, funded or not: nothing is skipped, the node wallet is asked when there is one."""
    return _new_snapshot(args, source="auto", addresses=args.addresses, skip_proven=False)


def _new_snapshot(args, *, source: str, addresses: list[str] | None, skip_proven: bool) -> int:
    """Take the snapshot, write it, say what was written: all that ``snapshot`` and ``prove`` share."""
    config = Config.load()
    name = _wallet_name(args, config)
    cli = _cli(args, config, node_wallet=config.node_wallet(name) if name is not None else None, wallet=name)
    if name is None and not args.output:  # the node's one loaded wallet names the directory; a UTXO-set scan has none to offer
        with contextlib.suppress(RpcError, KeyError, TypeError):
            name = config.wallet_of_node(str(cli.call("getwalletinfo")["walletname"]))
    parent = snapshots_dir(name) if name is not None and not args.output else None
    proven = _proven(name, skip_proven)
    notes: list[str] = []
    wallet = _wallet(args, cli, notes)
    snapshot, psbts = take_snapshot(
        cli,
        wallet,
        args.text if args.text is not None else str(config.get("text", name, DEFAULT_TEMPLATE)),
        depth=args.depth,
        source=source,
        strict_message=not args.allow_any_message,
        max_index=args.max_index,
        utxo_mode=args.utxo,
        progress=_progress,
        skip_addresses=proven,
        addresses=addresses,
    )
    directory = _snapshot_destination(args.output, args.force, parent, snapshot_name(snapshot.stamp.to_dict()))
    write_snapshot(directory, snapshot, psbts)
    print(json.dumps(_snapshot_summary(directory, snapshot, notes), indent=2), file=sys.stderr)
    print(str(directory))
    return 0


def _snapshot_summary(directory: Path, snapshot: Snapshot, notes: list[str]) -> dict:
    """What the owner is shown on stderr after a snapshot is written; the directory alone goes to stdout."""
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
    result = holdings(_cli(args, Config.load()), args.targets, at=args.at)
    emit(json.dumps(result, indent=2) if args.json else format_holdings(result), args.output)
    return 0


def _snapshot_to_work_on(args, config: Config, *, finalized: bool | None) -> tuple[Path, str | None]:
    """The snapshot directory a command was pointed at (or the wallet's newest), and the wallet it belongs to if known."""
    name = _wallet_name(args, config)
    given = getattr(args, "snapshot", None)
    if name is None and not (given and Path(given).exists()):
        name = only_wallet()  # the one wallet in the data directory needs no naming
    directory = snapshot_directory(given, name, finalized=finalized)
    return directory, name if _opt(args, "wallet") is not None else (wallet_of_directory(directory) or name)


def cmd_finalize(args) -> int:
    config = Config.load()
    directory, name = _snapshot_to_work_on(args, config, finalized=None)
    snapshot = load_snapshot(directory)
    if name is None and snapshot.get("node_wallet") is not None:
        name = config.wallet_of_node(str(snapshot["node_wallet"]))
    cli = _spend_history_node(args, config, snapshot, name)
    out = (
        Path(args.output) if args.output else proofs_file_in(directory) or directory / proofs_file_name(name, directory, snapshot["stamp"])
    )
    previous = _recorded_earlier(out) if cli is None else None
    notes: list[str] = []
    try:
        document = finalize_snapshot(directory, lenient=args.lenient, cli=cli, previous=previous, notes=notes)
    except RpcError as exc:
        raise RpcError(f"{exc}; pass --offline to write the proofs file without asking the node wallet") from exc
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


def _spend_history_node(args, config: Config, snapshot: dict, name: str | None) -> BitcoinCli | None:
    """The node wallet finalize asks for the spend history; None, with a line saying why, when it is not asked.

    ``-w`` names it; else the snapshot's own record; else the wallet the snapshot was filed under.
    """
    if _opt(args, "wallet") is not None:
        node_wallet = config.node_wallet(str(_opt(args, "wallet")))
    elif snapshot.get("node_wallet") is not None:
        node_wallet = str(snapshot["node_wallet"])
    else:
        node_wallet = None
    if args.offline:
        print(
            "offline: the node wallet is not asked for the spend history; outputs spent since cannot be shown held at the stamp",
            file=sys.stderr,
        )
        return None
    if node_wallet is None:  # None only: Core's default wallet is named ""
        print("no node wallet known for this snapshot (it came from a UTXO-set scan); the spend history is not recorded", file=sys.stderr)
        return None
    cli = _cli(args, config, node_wallet=node_wallet, wallet=name)
    try:
        cli.call("getwalletinfo")
    except (RpcError, OSError) as exc:
        raise CLIError(
            f"cannot reach the node wallet for the spend history ({exc}); pass --offline to write the proofs file without it"
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
    config = Config.load()
    if args.snapshot and Path(args.snapshot).is_file():
        path = Path(args.snapshot)
    else:
        path, _ = _snapshot_to_work_on(args, config, finalized=True)
    document = load_proofs(path)
    cli = None if args.offline else _cli(args, config)  # the auditor's node has no wallet: none is named
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
        help='how to reach the node, e.g. "bitcoin-cli -signet" or "bitcoin-cli -rpcconnect=10.0.0.5" (default: bitcoin-cli, or cli in config.toml). '
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
        help=f"the wallet: its directory {data_dir()}/NAME, and the node wallet of that name (or the one its [wallets.NAME] table "
        "names as node). Default: wallet in config.toml, else the node's one loaded wallet. May also follow the command.",
    )


def _add_wallet_and_message_args(p: argparse.ArgumentParser, *, descriptor: str, text: str, depth: str) -> None:
    """``--descriptor``, ``--text`` and ``--depth``, the same for snapshot and prove; each words the help for its own case."""
    p.add_argument("--descriptor", "-d", metavar="FILE|DESC", help=descriptor)
    p.add_argument("--text", default=None, help=text)
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help=depth)


def _add_psbt_args(p: argparse.ArgumentParser, *, max_index: str) -> None:
    """``--max-index``, ``--utxo`` and ``--allow-any-message``, the same for snapshot and prove."""
    p.add_argument("--max-index", type=int, default=1000, help=max_index)
    p.add_argument("--utxo", choices=["witness", "both"], default="witness", help="UTXO fields to embed in the PSBTs")
    p.add_argument(
        "--allow-any-message", action="store_true", help="do not insist on the display rules of hardware signers (bip322 lint-message)"
    )


def _add_destination_args(p: argparse.ArgumentParser) -> None:
    """``--output`` and ``--force``, the same for snapshot and prove."""
    p.add_argument(
        "--output", "-o", metavar="DIR", help="write the snapshot here instead of under the wallet's snapshots/ as <date>-<height>"
    )
    p.add_argument("--force", action="store_true", help="with -o: rewrite a snapshot that holds no signed PSBTs and no proofs file yet")


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
        help="stamp, funded addresses, message and one PSBT per address, into the wallet's snapshots/",
        description=(
            "Take the snapshot: read the wallet descriptor from the node wallet (or --descriptor), choose the stamp block "
            "(tip - DEPTH), find the wallet's coins confirmed at that block "
            "(listunspent on the node wallet, or a scantxoutset of the descriptor), compose the message from the template "
            f"plus the stamp line, and write snapshot.json, message.txt and to_sign/to_sign-NN.psbt per funded address into "
            f"{data_dir()}/<wallet>/snapshots/<date>-<height>/ (the address of each PSBT is in snapshot.json). "
            "Sign the PSBTs on the cosigners' devices and put the results in its signed/."
        ),
    )
    _add_node_args(p)
    _add_wallet_and_message_args(
        p,
        descriptor="use this descriptor (a file or the text) instead of the node wallet's own; it must be one of the wallet's descriptors",
        text=f"message template; {{date}} {{time}} {{height}} {{hash}} are filled from the stamp block "
        f"(default: text in config.toml, else '{DEFAULT_TEMPLATE}')",
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
        action="store_true",
        help="leave out the addresses that the wallet's earlier snapshots already prove (their proofs files under snapshots/)",
    )
    _add_destination_args(p)
    p.set_defaults(
        func=cmd_snapshot,
        examples=[
            "snapshot",
            "-w treasury snapshot --text 'Annual audit {date}'",
            "--cli 'bitcoin-cli -signet' -w watch snapshot",
            "snapshot -d wallet.desc --source scantxoutset -o audit-2026",
            "-w treasury snapshot --skip-proven   # only addresses no earlier snapshot proves",
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
        help="a snapshot for given addresses of the wallet, whether or not they hold coins yet",
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
        text=f"message template (default: text in config.toml, else '{DEFAULT_TEMPLATE}')",
        depth=f"stamp block is this many blocks behind the tip (default {DEFAULT_DEPTH})",
    )
    _add_psbt_args(p, max_index="derivation range to search per branch (default %(default)s)")
    _add_destination_args(p)
    p.set_defaults(func=cmd_prove, examples=["prove bc1q...", "-w treasury prove bc1q... --text 'Proof of control {date}'"])


def _add_finalize_parser(sub) -> None:
    p = sub.add_parser(
        "finalize",
        help="combine and finalize the signed PSBTs of a snapshot into its proofs file",
        description=(
            "Read every PSBT in the snapshot's to_sign/, signed/ and root, group them by address, combine, finalize, self-verify, "
            "and write <wallet>-<date>-<height>-proofs.json beside them; files in to_sign/ and signed/ that were not used are listed. "
            "Unless --offline, also ask the node wallet the coins came from (recorded in snapshot.json, or -w) which "
            "listed outputs have been spent since the snapshot and record the spending transactions: a spend confirmed after the "
            "stamp block lets the auditor show the output was unspent at the snapshot. Re-run finalize before handing the proofs file "
            "over if coins have moved. With --offline the spends an earlier run recorded are kept. " + EXIT_HELP
        ),
    )
    p.add_argument(
        "snapshot",
        metavar="SNAPSHOT",
        nargs="?",
        help="the snapshot: its name under the wallet's snapshots/ (2026-09-14-912345), or a directory; default: the wallet's newest",
    )
    _add_node_args(p)
    p.add_argument("--offline", action="store_true", help="do not ask the node wallet for the spend history")
    p.add_argument("--lenient", action="store_true", help="skip invalid partial signatures instead of failing")
    p.add_argument("--output", "-o", metavar="FILE", help="write the proofs file here instead of into the snapshot")
    p.set_defaults(
        func=cmd_finalize,
        examples=[
            "finalize",
            "finalize 2026-09-14-912345",
            "-w treasury finalize 2026-09-14-912345 --offline",
        ],
    )


def _add_verify_parser(sub) -> None:
    p = sub.add_parser(
        "verify",
        help="auditor side: verify a proofs file against the node and report",
        description=(
            "Verify every BIP-322 signature, check the stamp block with getblockheader, check every listed output with "
            "gettxout (amount, address, creation height), and print a report. Outputs spent since the snapshot are fetched by "
            "their recorded block hash and, with the spends finalize recorded, shown to have been unspent at the snapshot. "
            "Exit codes: 0 the result is OK (signatures, stamp and document check out, and every listed output is shown held at the "
            "stamp block); 1 FAILED (one of those does not check out, or the node contradicts a listed output); 3 INCOMPLETE (nothing "
            "failed, but a listed output is spent and nothing shows when: the owner re-runs finalize); 2 verify could not run."
        ),
    )
    p.add_argument(
        "snapshot",
        metavar="PROOFS",
        nargs="?",
        help="a proofs file, a snapshot directory, or a snapshot's name under the wallet's snapshots/; default: the wallet's newest finalized",
    )
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
            "verify treasury-2026-09-14-912345-proofs.json",
            "verify treasury-2026-09-14-912345-proofs.json --json --report audit-report.json",
            "verify treasury-2026-09-14-912345-proofs.json --offline",
            "verify   # the owner: the wallet's newest finalized snapshot",
        ],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bip322-audit",
        description=f"Proof of control of a wallet's coins at a point in time: BIP-322 proofs plus on-chain checks through bitcoin-cli ({SPEC}). "
        f"Snapshots and their proofs files live under {data_dir()}/<wallet>/snapshots/ (BIP322_DIR moves the tree); "
        "config.toml there holds the defaults.",
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
    except (CLIError, ConfigError, BIP322Error, RpcError, AuditError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc.strerror or exc}: {exc.filename}" if getattr(exc, "filename", None) else f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
