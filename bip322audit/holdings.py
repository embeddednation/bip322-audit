"""What addresses hold, from a UTXO-set scan: the on-chain step of verifying a statement.

``holdings`` answers "what do these addresses hold now, and how much of that
was already there at block N": every unspent output paying them, from
``scantxoutset``, optionally kept to those confirmed at or before a block.
No wallet, no index.  Coins spent after block N cannot show here; the owner's
records (``report.json`` of a statement, the spends a ``proofs.json``
carries) name them, and ``verify`` checks those.
"""

from __future__ import annotations

from datetime import datetime, timezone

from embit.networks import NETWORKS
from embit.script import Script

from .rpc import BitcoinCli, RpcError, btc, to_sat


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_block(cli: BitcoinCli, at: str | int) -> dict:
    """A block by height or hash: ``{height, hash, time}``."""
    text = str(at).strip()
    block_hash = cli.block_hash(int(text)) if text.isdigit() else text
    header = cli.block_header(block_hash)
    if int(header.get("confirmations", 0)) <= 0:
        raise RpcError(f"block {block_hash} is not in the main chain")
    return {"height": int(header["height"]), "hash": header.get("hash", block_hash), "time": _iso(header["time"])}


def holdings(cli: BitcoinCli, addresses: list[str], *, at: str | int | None = None) -> dict:
    """Unspent outputs of the addresses now, and their part confirmed at or before ``at`` when given."""
    ordered = list(dict.fromkeys(a.strip() for a in addresses if a.strip()))
    if not ordered:
        raise ValueError("no addresses")
    block = resolve_block(cli, at) if at is not None else None
    result = cli.call("scantxoutset", "start", [{"desc": f"addr({a})"} for a in ordered])
    if not result or not result.get("success"):
        raise RpcError("scantxoutset did not succeed (another scan running?)")
    scan_height, scan_hash = int(result["height"]), result["bestblock"]
    headers: dict[int, str] = {}
    network = NETWORKS.get(
        {"main": "main", "test": "test", "regtest": "regtest", "signet": "signet"}.get(cli.chain(), "main"), NETWORKS["main"]
    )

    def when(height: int) -> str:
        if height not in headers:
            headers[height] = _iso(cli.block_header(cli.block_hash(height))["time"])
        return headers[height]

    per: dict[str, list[dict]] = {a: [] for a in ordered}
    for u in result.get("unspents", []):
        desc = str(u.get("desc", ""))
        address = desc[5 : desc.index(")")] if desc.startswith("addr(") and ")" in desc else None
        if address is None:
            try:
                address = Script(bytes.fromhex(u["scriptPubKey"])).address(network)
            except Exception:  # noqa: BLE001 - an output this tool cannot name is not one of the addresses asked for
                continue
        if address not in per:
            continue
        height = int(u["height"])
        per[address].append(
            {
                "txid": u["txid"],
                "vout": int(u["vout"]),
                "amount_sat": to_sat(u["amount"]),
                "amount_btc": btc(to_sat(u["amount"])),
                "height": height,
                "time_utc": when(height),
            }
        )
    rows = []
    for address in ordered:
        outputs = sorted(per[address], key=lambda o: (o["height"], o["txid"], o["vout"]))
        counted = [o for o in outputs if block is None or o["height"] <= block["height"]]
        later = [o for o in outputs if block is not None and o["height"] > block["height"]]
        total = sum(o["amount_sat"] for o in counted)
        rows.append(
            {
                "address": address,
                "total_sat": total,
                "total_btc": btc(total),
                "outputs": counted,
                "later_outputs": later,
                "later_sat": sum(o["amount_sat"] for o in later),
            }
        )
    return {
        "scan": {"height": scan_height, "hash": scan_hash},
        "at": block,
        "addresses": rows,
        "total_sat": sum(r["total_sat"] for r in rows),
        "total_btc": btc(sum(r["total_sat"] for r in rows)),
    }


def format_holdings(result: dict) -> str:
    """The text the command prints: one line per address, its outputs beneath, a total."""
    at = result["at"]
    lines = []
    for r in result["addresses"]:
        n = len(r["outputs"])
        qualifier = f"confirmed by block {at['height']} ({at['time']})" if at else f"unspent at block {result['scan']['height']}"
        lines.append(f"{r['address']}  {r['total_btc']} BTC  {n} output{'s' if n != 1 else ''} {qualifier}")
        for o in r["outputs"]:
            lines.append(f"  {o['txid']}:{o['vout']}  {o['amount_btc']}  block {o['height']} ({o['time_utc']})")
        if r["later_outputs"]:
            lines.append(
                f"  + {btc(r['later_sat'])} BTC in {len(r['later_outputs'])} output(s) confirmed after block {at['height']}, not counted"
            )
    if len(result["addresses"]) > 1:
        lines.append(f"total  {result['total_btc']} BTC")
    if at:
        lines.append(f"(unspent outputs as of block {result['scan']['height']}; coins spent since block {at['height']} do not show)")
    return "\n".join(lines)


def holdings_command(addresses: list[str], at: str | int | None) -> str:
    """The command line a reader runs to get the same answer."""
    return "bip322 audit holdings " + " ".join(addresses) + (f" --at {at}" if at is not None else "")
