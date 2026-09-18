"""What outputs or addresses hold, from the node: the on-chain step of verifying a statement.

By output (``TXID:VOUT``) the check is a direct lookup, ``gettxout``: instant,
and it confirms the address and the amount the statement gives.  By address
it is a scan of the whole UTXO set, ``scantxoutset``: minutes on mainnet,
because Bitcoin Core keeps no index from address to outputs.  ``--at`` says
which block the statement is about: an output confirmed at or before it and
unspent now was unspent at that block.  Coins spent after that block cannot
show here; the owner's records (``report.json`` of a statement, the spends a
``proofs.json`` carries) name them, and ``verify`` checks those.
"""

from __future__ import annotations

from datetime import datetime, timezone

from embit.networks import NETWORKS
from embit.script import Script

from .rpc import BitcoinCli, RpcError, btc, to_sat


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_outpoint(text: str) -> bool:
    txid, sep, vout = text.partition(":")
    return sep == ":" and len(txid) == 64 and all(c in "0123456789abcdefABCDEF" for c in txid) and vout.isdigit()


def resolve_block(cli: BitcoinCli, at: str | int) -> dict:
    """A block by height or hash: ``{height, hash, time}``."""
    text = str(at).strip()
    block_hash = cli.block_hash(int(text)) if text.isdigit() else text
    header = cli.block_header(block_hash)
    if int(header.get("confirmations", 0)) <= 0:
        raise RpcError(f"block {block_hash} is not in the main chain")
    return {"height": int(header["height"]), "hash": header.get("hash", block_hash), "time": _iso(header["time"])}


def holdings(cli: BitcoinCli, targets: list[str], *, at: str | int | None = None) -> dict:
    """Outputs (``TXID:VOUT``) or addresses: what each holds now, and whether that was already there at ``at``."""
    ordered = list(dict.fromkeys(t.strip() for t in targets if t.strip()))
    if not ordered:
        raise ValueError("no outputs or addresses given")
    modes = {is_outpoint(t) for t in ordered}
    if len(modes) != 1:
        raise ValueError("give outputs (TXID:VOUT) or addresses, not both")
    block = resolve_block(cli, at) if at is not None else None
    tip_height, tip_hash = cli.tip()
    headers: dict[int, str] = {}

    def when(height: int) -> str:
        if height not in headers:
            headers[height] = _iso(cli.block_header(cli.block_hash(height))["time"])
        return headers[height]

    outputs = _by_outpoint(cli, ordered, tip_height, when) if modes == {True} else _by_address(cli, ordered, when)
    for o in outputs:
        o["counted"] = bool(o["unspent"]) and (block is None or (o["height"] is not None and o["height"] <= block["height"]))
    total = sum(o["amount_sat"] for o in outputs if o["counted"])
    return {
        "mode": "outputs" if modes == {True} else "addresses",
        "tip": {"height": tip_height, "hash": tip_hash},
        "at": block,
        "outputs": outputs,
        "total_sat": total,
        "total_btc": btc(total),
    }


def _by_outpoint(cli: BitcoinCli, outpoints: list[str], tip_height: int, when) -> list[dict]:
    rows = []
    for text in outpoints:
        txid, _, vout = text.partition(":")
        txid, vout = txid.lower(), int(vout)
        out = cli.call("gettxout", txid, vout, False)
        if not out:
            rows.append(
                {
                    "txid": txid,
                    "vout": vout,
                    "address": None,
                    "script": None,
                    "amount_sat": 0,
                    "amount_btc": None,
                    "height": None,
                    "time_utc": None,
                    "unspent": False,
                }
            )
            continue
        height = tip_height - int(out["confirmations"]) + 1
        rows.append(
            {
                "txid": txid,
                "vout": vout,
                "address": out.get("scriptPubKey", {}).get("address"),
                "script": out.get("scriptPubKey", {}).get("hex"),
                "amount_sat": to_sat(out["value"]),
                "amount_btc": btc(to_sat(out["value"])),
                "height": height,
                "time_utc": when(height),
                "unspent": True,
            }
        )
    return rows


def _by_address(cli: BitcoinCli, addresses: list[str], when) -> list[dict]:
    result = cli.call("scantxoutset", "start", [{"desc": f"addr({a})"} for a in addresses])
    if not result or not result.get("success"):
        raise RpcError("scantxoutset did not succeed (another scan running?)")
    network = NETWORKS.get(
        {"main": "main", "test": "test", "regtest": "regtest", "signet": "signet"}.get(cli.chain(), "main"), NETWORKS["main"]
    )
    rows = []
    for u in result.get("unspents", []):
        desc = str(u.get("desc", ""))
        address = desc[5 : desc.index(")")] if desc.startswith("addr(") and ")" in desc else None
        if address is None:
            try:
                address = Script(bytes.fromhex(u["scriptPubKey"])).address(network)
            except Exception:  # noqa: BLE001 - an output this tool cannot name is not one of the addresses asked for
                continue
        if address not in addresses:
            continue
        height = int(u["height"])
        rows.append(
            {
                "txid": u["txid"],
                "vout": int(u["vout"]),
                "address": address,
                "script": u.get("scriptPubKey"),
                "amount_sat": to_sat(u["amount"]),
                "amount_btc": btc(to_sat(u["amount"])),
                "height": height,
                "time_utc": when(height),
                "unspent": True,
            }
        )
    order = {a: i for i, a in enumerate(addresses)}
    rows.sort(key=lambda o: (order[o["address"]], o["height"], o["txid"], o["vout"]))
    return rows


def format_holdings(result: dict) -> str:
    """The text the command prints: what each output is locked to, its amount, when it was confirmed, and its status.

    For a single output the outpoint is not repeated (it is the argument) and
    there is no total; for several, each block starts with its outpoint and a
    total follows.
    """
    at = result["at"]
    tip = result["tip"]["height"]
    single = len(result["outputs"]) == 1
    lines = []
    for o in result["outputs"]:
        if lines:
            lines.append("")
        if not single:
            lines.append(f"output     {o['txid']}:{o['vout']}")
        if not o["unspent"]:
            lines.append(f"status     not in the UTXO set at block {tip}: spent, or never existed")
            continue
        lines.append(f"locked to  {o['script'] or o['address']}")
        lines.append(f"amount     {o['amount_btc']} BTC")
        lines.append(f"confirmed  block {o['height']}, {o['time_utc']}")
        if at is None:
            lines.append(f"status     unspent at block {tip}")
        elif o["counted"]:
            later = f", still unspent at block {tip}" if tip != at["height"] else ""
            lines.append(f"status     held at block {at['height']} ({at['time']}){later}")
        else:
            lines.append(f"status     confirmed after block {at['height']}, not counted")
    if not single:
        what = f"held at block {at['height']}" if at else f"unspent at block {tip}"
        lines.append("")
        lines.append(f"total      {result['total_btc']} BTC {what}, {sum(1 for o in result['outputs'] if o['counted'])} output(s)")
    return "\n".join(lines)


def holdings_command(targets: list[str], at: str | int | None) -> str:
    """The command line a reader runs to get the same answer."""
    return "bip322 audit holdings " + " ".join(targets) + (f" --at {at}" if at is not None else "")
