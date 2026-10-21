"""The block stamp: ``block: HEIGHT HASH ISO-8601-TIME`` as the last line of a message.

The hash makes the message impossible to have written before that block
existed (a *not before* bound); the height and the block's own header time
make it readable and checkable with one ``getblockheader`` call.  All three
come from the block, so the stamp is verifiable as a whole.

Which block: the one a holder names (:func:`header_at`), the last block
before an instant (:func:`header_before`: ``--before 2027-01-01`` is the
last block of 2026, the block a statement of 2026 closes on), or a block
behind the tip (:func:`fetch_stamp`).  Given none, :func:`choose_stamp`
takes the last block before today 00:00 UTC, the night before: a statement
to date closes on the same block, so a snapshot and a statement taken on
the same day agree without a word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .rpc import BitcoinCli, RpcError

__all__ = [
    "DEFAULT_DEPTH",
    "PLACEHOLDERS",
    "STAMP_RE",
    "Stamp",
    "check_stamp",
    "choose_stamp",
    "compose_message",
    "fetch_stamp",
    "header_at",
    "header_before",
    "iso_utc",
    "parse_stamp",
    "parse_when",
    "start_of_today",
]

# [0-9], not \d: \d also matches the digits of other scripts, which int() would then read as a height
STAMP_RE = re.compile(r"^block: ([0-9]+) ([0-9a-f]{64}) ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)$", re.M)
PLACEHOLDERS = ("date", "time", "height", "hash")
_BRACES_RE = re.compile(r"\{([^{}]*)\}")
_OWN_STAMP_RE = re.compile(r"^\s*block:", re.M | re.I)
DEFAULT_DEPTH = 6


def iso_utc(unix_time: int | None = None) -> str:
    """A time as every file and report of this tool writes it: ISO 8601, UTC, to the second, ``Z``.

    ``unix_time`` is a block header's time; without it, now.
    """
    when = datetime.now(UTC) if unix_time is None else datetime.fromtimestamp(int(unix_time), UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Stamp:
    """A block as a message names it; two stamps are equal when height, hash and time all are."""

    height: int
    hash: str
    time: str  # ISO 8601 UTC of the block header time

    def line(self) -> str:
        return f"block: {self.height} {self.hash} {self.time}"

    def to_dict(self) -> dict:
        return {"height": self.height, "hash": self.hash, "time": self.time}

    @classmethod
    def from_dict(cls, d: dict) -> Stamp:
        return cls(int(d["height"]), str(d["hash"]), str(d["time"]))

    @classmethod
    def from_header(cls, header: dict) -> Stamp:
        """From a block as :func:`header_at` and :func:`header_before` give it: height, hash and the header time in seconds."""
        return cls(int(header["height"]), str(header["hash"]), iso_utc(header["time"]))


def parse_when(text: str) -> datetime:
    """``YYYY-MM-DD`` (midnight UTC) or an ISO 8601 instant; a time without a zone is UTC."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def start_of_today() -> datetime:
    """Today 00:00 UTC: the instant the night before ended, which the default stamp and a statement to date lie before."""
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _header(cli: BitcoinCli, height: int, block_hash: str | None = None) -> dict:
    """A block as the choosers hand it on: ``{"height", "hash", "time"}``, the time in seconds as the header carries it."""
    block_hash = block_hash or cli.block_hash(height)
    header = cli.block_header(block_hash)
    return {"height": int(header["height"]), "hash": block_hash, "time": int(header["time"])}


def header_at(cli: BitcoinCli, at: str | int) -> dict:
    """The block named by height or hash, which must be in the main chain."""
    text = str(at).strip()
    block_hash = cli.block_hash(int(text)) if text.isdigit() else text
    header = cli.block_header(block_hash)
    if int(header.get("confirmations", 0)) <= 0:
        raise RpcError(f"block {block_hash} is not in the main chain")
    return {"height": int(header["height"]), "hash": block_hash, "time": int(header["time"])}


def header_before(cli: BitcoinCli, when: datetime, *, tip_height: int | None = None) -> dict:
    """The last block before ``when``: the highest block whose header time is before it, found by a binary search over the
    headers; the chain's first block when even that is not before it, the tip when ``when`` lies ahead of the chain.

    Header times need not rise with the height, so this is the natural boundary rather than a unique one; the height is what
    makes it definite, and every file names it.  ``tip_height``: search no higher than this, the tip the caller read, so that
    the stamp and whatever else is derived from that tip belong together.
    """
    ts = int(when.timestamp())
    if tip_height is None:
        tip_height, _ = cli.tip()
    first = _header(cli, 0)
    if first["time"] >= ts:
        return first
    lo, hi = 0, tip_height
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _header(cli, mid)["time"] < ts:
            lo = mid
        else:
            hi = mid - 1
    return _header(cli, lo)


def fetch_stamp(cli: BitcoinCli, depth: int = DEFAULT_DEPTH, *, tip_height: int | None = None) -> Stamp:
    """The block ``depth`` blocks behind the tip (6: safe from reorgs, an hour of slack; what ``prove`` takes).

    ``tip_height``: the tip the caller already read, so that the stamp and whatever else is derived from it share one tip.
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")
    if tip_height is None:
        tip_height, _ = cli.tip()
    height = tip_height - depth
    if height < 0:
        raise ValueError(f"chain has only {tip_height + 1} blocks; depth {depth} is too large")
    return Stamp.from_header(_header(cli, height))


def choose_stamp(
    cli: BitcoinCli,
    *,
    at: str | int | None = None,
    before: datetime | None = None,
    depth: int | None = None,
    tip_height: int | None = None,
) -> Stamp:
    """The stamp block, chosen one way: ``at`` a height or hash, ``before`` an instant (its last block), ``depth`` blocks
    behind the tip; none of them, the last block before today 00:00 UTC, the night before.

    ``tip_height`` as for :func:`fetch_stamp`; a block above it (one that arrived since the caller read the tip) is refused.
    """
    if sum(x is not None for x in (at, before, depth)) > 1:
        raise ValueError("choose the stamp block one way: at, before or depth")
    if depth is not None:
        return fetch_stamp(cli, depth, tip_height=tip_height)
    header = header_at(cli, at) if at is not None else header_before(cli, before or start_of_today(), tip_height=tip_height)
    if tip_height is not None and header["height"] > tip_height:
        raise RpcError(f"block {header['height']} is above the tip ({tip_height}) this snapshot is read at; try again")
    return Stamp.from_header(header)


def parse_stamp(message: bytes | str) -> Stamp | None:
    """The stamp on the message's last line, or None."""
    text = message.decode("utf-8", errors="replace") if isinstance(message, bytes) else message
    last = text.rstrip("\n").rsplit("\n", 1)[-1]
    match = STAMP_RE.match(last)
    if not match:
        return None
    return Stamp(int(match.group(1)), match.group(2), match.group(3))


def compose_message(template: str, stamp: Stamp) -> str:
    """``template`` with ``{date}``/``{time}``/``{height}``/``{hash}`` filled in, then the stamp line.

    ``{date}`` and ``{time}`` are the stamp block's header time in UTC.  Nothing else in braces is
    substituted (no ``str.format``: that would read attributes), and a template with a ``block:`` line
    of its own is refused: the parser takes the last line, a reader might take the first.
    """
    values = {"date": stamp.time[:10], "time": stamp.time, "height": str(stamp.height), "hash": stamp.hash}
    unknown = sorted({m.group(0) for m in _BRACES_RE.finditer(template) if m.group(1) not in values})
    if unknown:
        raise ValueError(f"unknown placeholder {', '.join(unknown)} in the message template (known: {{date}} {{time}} {{height}} {{hash}})")
    if _OWN_STAMP_RE.search(template):
        raise ValueError("the message template has a 'block:' line of its own; the stamp line is added by the tool")
    text = _BRACES_RE.sub(lambda m: values[m.group(1)], template).rstrip()
    return f"{text}\n{stamp.line()}" if text else stamp.line()


def check_stamp(cli: BitcoinCli, stamp: Stamp) -> dict:
    """Compare the stamp with the node's view of that block.

    Returns ``{"stamp", "ok"}`` and, when the node knows the block, ``node_height``, ``node_time``,
    ``confirmations``, ``in_main_chain``, ``height_matches`` and ``time_matches``; otherwise ``error``.
    ``ok`` is true only when the block is in the main chain with the stamp's height and time.  Never raises
    for a node that does not know the block: that is a result.
    """
    result = {"stamp": stamp.to_dict(), "ok": False}
    try:
        header = cli.block_header(stamp.hash)
    except Exception as exc:  # noqa: BLE001 - unknown hash is the interesting outcome, not a crash
        result["error"] = f"block hash unknown to this node: {exc}"
        try:  # a node that has not caught up cannot know the block yet: that is not evidence against the stamp
            info = cli.info()
            if info.get("initialblockdownload"):
                result["error"] = f"this node is still syncing (at block {info['blocks']}); it cannot check the stamp block yet"
        except (RpcError, TypeError, KeyError):
            pass
        return result
    confirmations = int(header.get("confirmations", -1))
    result.update(
        {
            "node_height": int(header["height"]),
            "node_time": iso_utc(header["time"]),
            "confirmations": confirmations,
            "in_main_chain": confirmations > 0,
            "height_matches": int(header["height"]) == stamp.height,
            "time_matches": iso_utc(header["time"]) == stamp.time,
        }
    )
    result["ok"] = result["in_main_chain"] and result["height_matches"] and result["time_matches"]
    return result
