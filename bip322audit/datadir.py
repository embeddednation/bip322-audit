"""The data directory: one tree for every wallet, in which the tools name everything.

``~/bip322``, unless ``--dir`` or ``BIP322_DIR`` says otherwise (or ``~/bip322`` is a symlink)::

    ~/bip322/
      config.toml                           the one configuration file
      <wallet>/snapshots/<date>-<height>/   one round of proofs: snapshot.json, message.txt, to_sign/, signed/,
                                            and once finalized <wallet>-<date>-<height>-proofs.json
      <wallet>/statements/<label>/          a statement: <wallet>-<label>.pdf (or .html), .json, .csv,
                                            and a copy of each proofs file it cites

A directory is named for its place: the tree supplies the wallet and the
kind, so the names stay bare.  A file that leaves the tree, a proofs file or
a statement's file, is named for its life, ``<wallet>-<when>-<kind>``, and
keeps that one name at home, in a statement and at the auditor's.

``config.toml`` holds defaults for every wallet at the top level and, in a
``[wallets.NAME]`` table, for one; the command line beats both.  ``init``
writes it with every key shown (:func:`write_config`)::

    wallet = "treasury"           # the default -w
    cli = "bitcoin-cli"
    text = "Proof of control {date}"
    dust = 1000

    [wallets.treasury]
    holder = "Company AB"
    node = "treasury-2of3-main"   # the node wallet, when its name is not the directory's
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "Config",
    "ConfigError",
    "data_dir",
    "newest_snapshot",
    "only_wallet",
    "proofs_file_in",
    "proofs_file_name",
    "sample_config",
    "snapshot_directory",
    "snapshots_dir",
    "statements_dir",
    "use_data_dir",
    "wallet_dir",
    "wallets",
    "write_config",
]

DEFAULT_DIR = "~/bip322"
CONFIG_FILE = "config.toml"


class ConfigError(Exception):
    """``config.toml`` cannot be read or says something the tools cannot use; or a name that fits no directory."""


_chosen: Path | None = None  # --dir


def data_dir() -> Path:
    """``--dir``, else ``$BIP322_DIR``, else ``~/bip322``; it need not exist yet."""
    return _chosen or Path(os.environ.get("BIP322_DIR") or DEFAULT_DIR).expanduser()


def use_data_dir(path: str | os.PathLike | None) -> None:
    """``--dir`` for the rest of the process; None leaves it to the environment."""
    global _chosen
    _chosen = Path(path).expanduser() if path is not None else None


class Config:
    """``config.toml``, parsed: top-level keys are defaults for every wallet, ``[wallets.NAME]`` keys for one."""

    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.data = data or {}
        self.path = path
        wallets = self.data.get("wallets", {})
        if not isinstance(wallets, dict) or not all(isinstance(v, dict) for v in wallets.values()):
            raise ConfigError(f"{path or CONFIG_FILE}: [wallets] must hold one [wallets.NAME] table per wallet")

    @classmethod
    def load(cls, directory: Path | None = None) -> Config:
        """The data directory's ``config.toml``; an empty configuration when there is none."""
        path = (directory or data_dir()) / CONFIG_FILE
        if not path.is_file():
            return cls({}, path)
        try:
            return cls(tomllib.loads(path.read_text()), path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"{path}: {exc}") from exc

    @property
    def wallets(self) -> dict[str, dict]:
        return self.data.get("wallets", {})

    def get(self, key: str, wallet: str | None = None, default=None):
        """A setting: the wallet's own table first, then the top level, then ``default``."""
        if wallet is not None and key in self.wallets.get(wallet, {}):
            return self.wallets[wallet][key]
        if key != "wallets" and key in self.data:
            return self.data[key]
        return default

    def node_wallet(self, wallet: str) -> str:
        """The node wallet behind a wallet directory: ``node`` in its table, else the same name."""
        node = self.wallets.get(wallet, {}).get("node")
        return str(node) if node is not None else wallet

    def wallet_of_node(self, node_wallet: str) -> str:
        """The wallet directory of a node wallet: the table that names it as ``node``, else the same name."""
        for name, table in self.wallets.items():
            if str(table.get("node", name)) == node_wallet:
                return name
        return node_wallet


def wallet_dir(wallet: str) -> Path:
    """``~/bip322/<wallet>``; refuses a name that is not a plain directory name (Core's default wallet is named "")."""
    if not wallet or wallet in (".", "..") or wallet.startswith(".") or "/" in wallet or "\\" in wallet:
        raise ConfigError(
            f"{wallet!r} is not usable as a directory name: name the wallet with -w NAME and map it to the node wallet "
            f"with node = {wallet!r} in its [wallets.NAME] table of {CONFIG_FILE}"
        )
    return data_dir() / wallet


def snapshots_dir(wallet: str) -> Path:
    return wallet_dir(wallet) / "snapshots"


def statements_dir(wallet: str) -> Path:
    return wallet_dir(wallet) / "statements"


def wallets() -> list[str]:
    """The wallets that have a directory in the data directory."""
    root = data_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []


def only_wallet() -> str | None:
    """The one wallet in the data directory, when there is exactly one: what a command means when nothing names a wallet."""
    found = wallets()
    return found[0] if len(found) == 1 else None


def proofs_file_name(wallet: str | None, directory: Path, stamp: dict) -> str:
    """``<wallet>-<date>-<height>-proofs.json``; without a wallet, ``<date>-<height>-proofs.json``.

    ``<date>-<height>`` is the snapshot directory's own name when the tools named it (so a second
    snapshot in the same block, ``<date>-<height>-2``, gets its own proofs file name), else the stamp's.
    """
    when = snapshot_name(stamp)
    if directory.name.startswith(when):
        when = directory.name
    return f"{wallet}-{when}-proofs.json" if wallet else f"{when}-proofs.json"


def proofs_file_in(directory: Path) -> Path | None:
    """The proofs file of a snapshot directory, ``*-proofs.json`` (or ``proofs.json``, as earlier versions named it); None before finalize."""
    found = [p for p in directory.glob("*-proofs.json") if p.is_file()]
    if found:
        return max(found, key=lambda p: (p.stat().st_mtime, p.name))
    legacy = directory / "proofs.json"
    return legacy if legacy.is_file() else None


def snapshot_name(stamp: dict) -> str:
    """``<date>-<height>``: a snapshot directory's name under ``snapshots/``."""
    return f"{str(stamp['time'])[:10]}-{stamp['height']}"


def wallet_of_directory(directory: Path) -> str | None:
    """The wallet a snapshot directory belongs to by its place in the tree (``<wallet>/snapshots/<name>``); None elsewhere."""
    directory = directory.resolve()
    if directory.parent.name == "snapshots" and directory.parent.parent.parent == data_dir().resolve():
        return directory.parent.parent.name
    return None


def _stamp_height(directory: Path) -> tuple[int, str]:
    try:
        return int(json.loads((directory / "snapshot.json").read_text())["stamp"]["height"]), directory.name
    except (OSError, ValueError, KeyError, TypeError):
        return -1, directory.name


def newest_snapshot(wallet: str, *, finalized: bool | None = None) -> Path:
    """The snapshot with the highest stamp under the wallet's ``snapshots/``; with ``finalized``, among those with (or without) a proofs file."""
    root = snapshots_dir(wallet)
    found = [p for p in root.iterdir() if (p / "snapshot.json").is_file()] if root.is_dir() else []
    if finalized is not None:
        found = [p for p in found if (proofs_file_in(p) is not None) == finalized]
    if not found:
        what = "finalized snapshot" if finalized else "snapshot"
        raise ConfigError(f"no {what} under {root}")
    return max(found, key=_stamp_height)


def snapshot_directory(given: str | None, wallet: str | None, *, finalized: bool | None = None) -> Path:
    """The snapshot a command means: a path, a name under the wallet's ``snapshots/``, or nothing for the newest one there."""
    if given is not None:
        path = Path(given)
        if path.exists():
            return path
        if wallet is not None and (snapshots_dir(wallet) / given).is_dir():
            return snapshots_dir(wallet) / given
        raise ConfigError(f"{given}: no such snapshot" + (f" (not a path, and not under {snapshots_dir(wallet)})" if wallet else ""))
    if wallet is None:
        raise ConfigError("which snapshot? name one, or the wallet with -w NAME (or wallet = NAME in config.toml) for its newest")
    return newest_snapshot(wallet, finalized=finalized)


_SAMPLE_HEAD = """\
# bip322: one configuration file for every wallet under this directory.
# Top-level keys are defaults for every wallet; a [wallets.NAME] table
# overrides them for one wallet; the command line beats both.
"""
_SAMPLE_KEYS = (
    ('cli = "bitcoin-cli"', 'e.g. "bitcoin-cli -rpcconnect=10.0.0.5" or "bitcoin-cli -signet"'),
    ("timeout = 600", "seconds to wait for a node call"),
    ('text = "Proof of control {date}"', "the message; {date} {time} {height} {hash} of the stamp block"),
    None,
    ('holder = "Company AB"', "named in the statement's header"),
    ("dust = 1000", "satoshi; received outputs at or below this are left out of the statement"),
    ('theme = "journal"', "journal, paper, light or dark"),
    ('heading = "underline"', "underline, tab, number or redrule; default per theme"),
    ('explorer = "https://mempool.space"', '"" for no links; mainnet only by default'),
)


def _row(setting: str, note: str, on: bool = False) -> str:
    return f"{'' if on else '# '}{setting}".ljust(38) + f"# {note}"


def _key(name: str) -> str:
    """``name`` as a TOML key: bare when it can be, quoted otherwise."""
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def sample_config(wallets: Sequence[str] = ()) -> str:
    """``config.toml`` with every key the tools read: ``wallet`` and a table per name in ``wallets`` switched on, the rest comments to switch on."""
    names, on = (list(wallets), True) if wallets else (["treasury", "personal"], False)
    lines = [_SAMPLE_HEAD, _row(f"wallet = {json.dumps(names[0])}", "the default -w: the directory, and the node wallet of that name", on)]
    lines += [_row(*item) if item else "" for item in _SAMPLE_KEYS]
    for name in names:
        lines += [
            "",
            f"{'' if on else '# '}[wallets.{_key(name)}]",
            _row(f'node = "{name}-2of3-main"', "the node wallet, when its name is not the directory's"),
            _row('holder = "Company AB"', "a key given here beats the one above, for this wallet"),
        ]
    return "\n".join(lines) + "\n"


def write_config(wallets: Sequence[str] = ()) -> Path:
    """Write :func:`sample_config` as ``config.toml`` into the data directory (created if need be); an existing file is left alone."""
    for name in wallets:
        wallet_dir(name)  # a usable directory name, or ConfigError
    path = data_dir() / CONFIG_FILE
    if path.exists():
        raise ConfigError(f"{path} exists already: edit it, or move it away first")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        f.write(sample_config(wallets))
    return path
