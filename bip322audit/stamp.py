"""The block stamp: ``block: HEIGHT HASH ISO-8601-TIME`` as the last line of a message.

The hash makes the message impossible to have written before that block
existed (a *not before* bound); the height and the block's own header time
make it readable and checkable with one ``getblockheader`` call.  All three
come from the block, so the stamp is verifiable as a whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .rpc import BitcoinCli, RpcError

__all__ = ["DEFAULT_DEPTH", "PLACEHOLDERS", "STAMP_RE", "Stamp", "check_stamp", "compose_message", "fetch_stamp", "iso_utc", "parse_stamp"]

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


def fetch_stamp(cli: BitcoinCli, depth: int = DEFAULT_DEPTH, *, tip_height: int | None = None) -> Stamp:
    """The block ``depth`` blocks behind the tip (default 6: safe from reorgs, an hour of slack).

    ``tip_height``: the tip the caller already read, so that the stamp and whatever else is derived from it share one tip.
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")
    if tip_height is None:
        tip_height, _ = cli.tip()
    height = tip_height - depth
    if height < 0:
        raise ValueError(f"chain has only {tip_height + 1} blocks; depth {depth} is too large")
    block_hash = cli.block_hash(height)
    header = cli.block_header(block_hash)
    return Stamp(height=int(header["height"]), hash=block_hash, time=iso_utc(header["time"]))


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
