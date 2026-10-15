"""The data directory: one tree for every wallet, in which the tools name everything.

``bip322 init`` makes a directory the data directory by writing ``bip322.toml`` into
it, and a command run in it or anywhere below finds it, as git finds its repository;
``--dir`` or ``BIP322_DIR`` name one from elsewhere.  There is no default place::

    <dir>/
      bip322.toml                           the mark of the directory, and the one configuration file
      <wallet>/snapshots/<date>-<height>/   one round of proofs: snapshot.json, message.txt, to_sign/, signed/,
                                            and once finalized <wallet>-<date>-<height>-proofs.json
      <wallet>/statements/<label>/          a statement: <wallet>-<label>.pdf (or .html), .json, .csv,
                                            and a copy of each proofs file it cites

A directory is named for its place: the tree supplies the wallet and the
kind, so the names stay bare.  A file that leaves the tree, a proofs file or
a statement's file, is named for its life, ``<wallet>-<when>-<kind>``, and
keeps that one name at home, in a statement and at the auditor's.  Inside a
wallet's directory that wallet is the one meant (:func:`wallet_of_cwd`).

``bip322.toml`` holds defaults for every wallet at the top level and, in a
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
from pathlib import Path

__all__ = [
    "Config",
    "ConfigError",
    "data_dir",
    "find_data_dir",
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
    "wallet_of_cwd",
    "wallets",
    "write_config",
]

CONFIG_FILE = "bip322.toml"


class ConfigError(Exception):
    """``bip322.toml`` cannot be read or says something the tools cannot use; no data directory; or a name that fits no directory."""


_chosen: Path | None = None  # --dir


def find_data_dir() -> Path | None:
    """``--dir``, else ``$BIP322_DIR``, else the nearest directory at or above the current one with a ``bip322.toml``; None when there is none."""
    if _chosen is not None:
        return _chosen
    env = os.environ.get("BIP322_DIR")
    if env:
        return Path(env).expanduser()
    here = Path.cwd()
    return next((p for p in (here, *here.parents) if (p / CONFIG_FILE).is_file()), None)


def data_dir() -> Path:
    """The data directory a command works in; an error when there is none."""
    found = find_data_dir()
    if found is None:
        raise ConfigError(
            f"not in a bip322 directory: no {CONFIG_FILE} in {Path.cwd()} or above (bip322 init makes one; --dir DIR names one elsewhere)"
        )
    return found


def use_data_dir(path: str | os.PathLike | None) -> None:
    """``--dir`` for the rest of the process; None leaves it to the environment."""
    global _chosen
    _chosen = Path(path).expanduser() if path is not None else None


class Config:
    """``bip322.toml``, parsed: top-level keys are defaults for every wallet, ``[wallets.NAME]`` keys for one."""

    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.data = data or {}
        self.path = path
        wallets = self.data.get("wallets", {})
        if not isinstance(wallets, dict) or not all(isinstance(v, dict) for v in wallets.values()):
            raise ConfigError(f"{path or CONFIG_FILE}: [wallets] must hold one [wallets.NAME] table per wallet")
        addresses = self.data.get("addresses", {})
        if not isinstance(addresses, dict) or not all(isinstance(v, str) for v in addresses.values()):
            raise ConfigError(f"{path or CONFIG_FILE}: [addresses] must map each address to the name of the wallet it belongs to")

    @classmethod
    def load(cls, directory: Path | None = None) -> Config:
        """The data directory's ``bip322.toml``; an empty configuration when there is no file, or no data directory."""
        root = directory or find_data_dir()
        path = root / CONFIG_FILE if root is not None else None
        if path is None or not path.is_file():
            return cls({}, path)
        try:
            return cls(tomllib.loads(path.read_text()), path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"{path}: {exc}") from exc

    @property
    def wallets(self) -> dict[str, dict]:
        return self.data.get("wallets", {})

    @property
    def addresses(self) -> dict[str, str]:
        """``[addresses]``: the wallet each address of someone else belongs to, which names the other side of a movement."""
        return self.data.get("addresses", {})

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


def check_wallet_name(wallet: str) -> str:
    """``wallet`` when it is a plain directory name; an error otherwise (Core's default wallet is named "")."""
    if not wallet or wallet in (".", "..") or wallet.startswith(".") or "/" in wallet or "\\" in wallet:
        raise ConfigError(
            f"{wallet!r} is not usable as a directory name: name the wallet with -w NAME and map it to the node wallet "
            f"with node = {wallet!r} in its [wallets.NAME] table of {CONFIG_FILE}"
        )
    return wallet


def wallet_dir(wallet: str) -> Path:
    """``<dir>/<wallet>``."""
    return data_dir() / check_wallet_name(wallet)


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
    root = find_data_dir()
    directory = directory.resolve()
    if root is not None and directory.parent.name == "snapshots" and directory.parent.parent.parent == root.resolve():
        return directory.parent.parent.name
    return None


def wallet_of_cwd() -> str | None:
    """The wallet whose directory the current directory is, or is under (``<dir>/<wallet>/...``); None at the top or outside."""
    root = find_data_dir()
    if root is None:
        return None
    try:
        parts = Path.cwd().resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None
    return parts[0] if parts and not parts[0].startswith(".") else None


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
        raise ConfigError(f"which snapshot? name one, or the wallet with -w NAME (or wallet = NAME in {CONFIG_FILE}) for its newest")
    return newest_snapshot(wallet, finalized=finalized)


_SAMPLE_HEAD = """\
# bip322.toml: this is the bip322 directory, one directory per wallet inside;
# a command run here or below finds it. Top-level keys are defaults for every
# wallet; a [wallets.NAME] table overrides them for one; the command line beats both.
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


def sample_config(wallet: str | None = None) -> str:
    """``bip322.toml`` with every key the tools read, and ``[addresses]``: ``wallet`` and its table switched on when one is named, else every line a comment."""
    names, on = ([wallet], True) if wallet is not None else (["treasury", "personal"], False)
    lines = [_SAMPLE_HEAD, _row(f"wallet = {json.dumps(names[0])}", "the default -w: the directory, and the node wallet of that name", on)]
    lines += [_row(*item) if item else "" for item in _SAMPLE_KEYS]
    for name in names:
        lines += [
            "",
            f"{'' if on else '# '}[wallets.{_key(name)}]",
            _row(f'node = "{name}-2of3-main"', "the node wallet, when its name is not the directory's"),
            _row('holder = "Company AB"', "a key given here beats the one above, for this wallet"),
        ]
    lines += [
        "",
        _row("[addresses]", "the other side of a movement, named in the statement's CSV"),
        _row('"bc1q..." = "Exchange"', "an address of theirs = the wallet it belongs to"),
    ]
    return "\n".join(lines) + "\n"


def write_config(directory: Path, wallet: str | None = None) -> Path:
    """Make ``directory`` the data directory: write :func:`sample_config` as its ``bip322.toml``, creating the directory if need be.

    An existing file is left alone, and a directory inside a data directory already is refused.
    """
    if wallet is not None:
        check_wallet_name(wallet)
    directory = directory.resolve()
    path = directory / CONFIG_FILE
    if path.exists():
        raise ConfigError(f"{path} exists already: edit it, or move it away first")
    above = next((p for p in directory.parents if (p / CONFIG_FILE).is_file()), None)
    if above is not None:
        raise ConfigError(f"{directory} is inside the bip322 directory {above} already")
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        f.write(sample_config(wallet))
    return path
