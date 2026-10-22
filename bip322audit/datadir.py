"""The data directory: one tree, holders above wallets, in which the tools name everything.

``bip322 init`` makes a directory the data directory by writing ``bip322.toml`` into
it, and a command run in it or anywhere below finds it, as git finds its repository;
``--dir`` or ``BIP322_DIR`` name one from elsewhere.  There is no default place::

    <dir>/
      bip322.toml                           the mark of the directory, and the one configuration file
      <holder>/statements/<label>/          a statement of the holder over its wallets (bip322-report): <holder>-<label>.pdf
                                            (or .html), .json, .csv, -history.pdf, and a copy of each proofs file it cites
      <holder>/<wallet>/<date>-<height>/    one round of proofs: snapshot.json, message.txt, to_sign/, signed/,
                                            and once finalized <wallet>-<date>-<height>-proofs.json
      <holder>/<wallet>/history.json        the wallet's history as bip322-report last read it from the node
      <holder>/<account>/ledger.json        a custodial account's own record of its deposits and withdrawals (bip322-report)

The tree is the declaration: a top-level directory is a holder, and a
directory under it is one of the holder's wallets, from the moment it exists
(``bip322 init -w KEY/NAME`` makes the first, ``mkdir`` makes another), so a
statement knows its wallets before any proof is taken.  Two kinds of
directory under a holder are not wallets: ``statements/``, and an account,
marked by its ``ledger.json``.  A wallet's directory holds its snapshots and
its history, nothing else.  A directory is named for its place: the tree
supplies the holder, the wallet and the kind, so the names stay bare.  A
wallet's name is unique in the tree, so ``-w NAME`` finds it under whichever
holder it is; a new wallet is made under ``-w KEY/NAME``'s holder, else the
holder of the current directory, else ``holder`` in ``bip322.toml``, else
the tree's one holder.  A file that leaves the tree, a proofs file or a
statement's file, is named for its life, ``<wallet>-<when>-<kind>``, and
keeps that one name at home, in a statement and at the auditor's.  Inside a
wallet's directory that wallet is the one meant (:func:`wallet_of_cwd`).
Earlier versions kept a wallet's snapshots under ``<wallet>/snapshots/``:
such a directory is refused with the command that moves them up.

``bip322.toml`` holds defaults for every wallet at the top level, for a
holder's wallets in its ``[holders.KEY]`` table and for one wallet in its
``[wallets.NAME]`` table; the nearer wins, and the command line beats all
(:meth:`Config.get`).  ``init`` writes it with every key shown (:func:`write_config`)::

    holder = "company"            # the default holder: a new -w NAME is made in its directory
    cli = "bitcoin-cli"
    dust = 1000

    [holders.company]
    name = "Company AB"           # the statements' header, and the signed message: "Proof of control, Company AB, <date>"

    [wallets.treasury]
    node = "treasury-2of3-main"   # the node wallet, when its name is not the directory's

The keys ``wallet`` (a default ``-w``) and ``text`` (a message template) of
earlier versions are no longer read: the wallet is ``-w``, the directory the
command runs in, or the tree's one; the message is the holder's name and
the date (``--text`` for another), so that the name on the statement and
the name in the signed message are one.  A file that still has them is told.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

__all__ = [
    "HISTORY_FILE",
    "LEDGER_FILE",
    "STALE_KEYS",
    "Config",
    "ConfigError",
    "data_dir",
    "find_data_dir",
    "holder_dir",
    "holder_of",
    "holder_of_cwd",
    "holder_of_wallet",
    "holder_statements_dir",
    "holders",
    "name_wallet",
    "newest_snapshot",
    "only_wallet",
    "proofs_file_in",
    "proofs_file_name",
    "sample_config",
    "snapshot_directory",
    "use_data_dir",
    "use_holder",
    "wallet_dir",
    "wallet_dirs",
    "wallet_of_cwd",
    "wallets",
    "write_config",
]

CONFIG_FILE = "bip322.toml"
_TABLES = ("holders", "wallets", "addresses")  # the tables of bip322.toml: not settings
RESERVED = ("snapshots", "statements")  # statements/ is the holder's, snapshots/ was the wallet's: no wallet or holder takes them
HISTORY_FILE = "history.json"  # in a wallet's directory: its history as bip322-report last read it from the node
SNAPSHOT_FILE = "snapshot.json"  # in a snapshot's directory: what marks one
LEDGER_FILE = "ledger.json"  # in an account's directory: what marks one (a custodial account's record, read by bip322-report)
STALE_KEYS = {
    "wallet": "the wallet is -w NAME, the directory the command runs in, or the tree's one",
    "text": "the message is the holder's name and the date (--text for another)",
}
_NAME = re.compile(r"[A-Za-z0-9_.-]+")


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
        self._check_holders(path)

    def _check_holders(self, path: Path | None) -> None:
        """``holder`` (the default holder) is a directory name; a ``[holders.KEY]`` table's ``name`` is a string."""
        where = path or CONFIG_FILE
        default = self.data.get("holder")
        if default is not None and (not isinstance(default, str) or not _NAME.fullmatch(default) or default in RESERVED):
            raise ConfigError(f"{where}: holder = {default!r}: the default holder is its directory's name (letters, digits, - _ .)")
        holders = self.data.get("holders", {})
        if not isinstance(holders, dict) or not all(isinstance(v, dict) for v in holders.values()):
            raise ConfigError(f"{where}: [holders] must hold one [holders.KEY] table per holder, KEY its directory's name")
        for key, table in holders.items():
            if not _NAME.fullmatch(key) or key in RESERVED:
                raise ConfigError(f"{where}: [holders.{key}]: the key is the holder's directory name (letters, digits, - _ .)")
            if not isinstance(table.get("name", key), str):
                raise ConfigError(f"{where}: [holders.{key}]: name must be a string, the holder's name as the statements put it")

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
    def holders(self) -> dict[str, dict]:
        """``[holders.KEY]`` tables as they are: ``name``, and defaults for the holder's wallets."""
        return self.data.get("holders", {})

    @property
    def default_holder(self) -> str | None:
        """``holder``: the holder a new wallet is made under when nothing else says."""
        value = self.data.get("holder")
        return str(value) if value is not None else None

    def holder_name(self, key: str | None) -> str | None:
        """The holder's name as the statements put it, and as the signed message names it: ``name`` in its table, else the key."""
        if key is None:
            return None
        return str(self.holders.get(key, {}).get("name", key))

    def stale(self) -> list[str]:
        """Keys of earlier versions that are no longer read, each with where it stands and what replaced it: for a note to the owner."""
        found = []
        for key, instead in STALE_KEYS.items():
            places = ["bip322.toml"] if key in self.data else []
            places += [f"[holders.{k}]" for k, t in self.holders.items() if isinstance(t, dict) and key in t]
            places += [f"[wallets.{k}]" for k, t in self.wallets.items() if isinstance(t, dict) and key in t]
            found += [f"{key} in {place} is no longer read: {instead}" for place in places]
        return found

    @property
    def addresses(self) -> dict[str, str]:
        """``[addresses]``: the wallet each address of someone else belongs to, which names the other side of a movement."""
        return self.data.get("addresses", {})

    def get(self, key: str, wallet: str | None = None, default=None, *, holder: str | None = None):
        """A setting: the wallet's own table, then its holder's, then the top level, then ``default``.

        The holder is ``holder`` when given, else the wallet's (:func:`holder_of`): the directory it is under, or
        the one a new wallet would be made under.
        """
        if wallet is not None and key in self.wallets.get(wallet, {}):
            return self.wallets[wallet][key]
        if holder is None:
            holder = holder_of(wallet)
        if holder is not None and key in self.holders.get(holder, {}):
            return self.holders[holder][key]
        if key not in _TABLES and key in self.data:
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
    if not wallet or wallet in (".", "..") or wallet.startswith(".") or "/" in wallet or "\\" in wallet or wallet in RESERVED:
        raise ConfigError(
            f"{wallet!r} is not usable as a directory name: name the wallet with -w NAME and map it to the node wallet "
            f"with node = {wallet!r} in its [wallets.NAME] table of {CONFIG_FILE}"
        )
    return wallet


_holder: str | None = None  # -w KEY/NAME: the holder a new wallet is made under, for the rest of the process


def use_holder(key: str | None) -> None:
    global _holder
    _holder = check_wallet_name(key) if key is not None else None


def name_wallet(given: str) -> str:
    """``-w [KEY/]NAME``: the wallet's name, after taking the holder before the slash, if any, for :func:`use_holder`."""
    holder, slash, wallet = given.rpartition("/")
    if slash:
        use_holder(holder)
    return wallet


def _tree_parts(directory: Path) -> tuple[str, ...] | None:
    """The path of ``directory`` inside the data directory, as parts; None outside it or without one."""
    root = find_data_dir()
    if root is None:
        return None
    try:
        return directory.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None


def _is_snapshot_dir(path: Path) -> bool:
    return path.is_dir() and (path / SNAPSHOT_FILE).is_file()


def _is_wallet_dir(path: Path) -> bool:
    """A wallet's directory: any directory under a holder that is not reserved (``statements/``) and not an account (``ledger.json``)."""
    if not path.is_dir() or path.name.startswith(".") or path.name in RESERVED:
        return False
    return not (path / LEDGER_FILE).is_file()


def _check_layout(path: Path) -> None:
    """A wallet directory laid out as earlier versions did, its snapshots under ``snapshots/``, is refused with the command that mends it."""
    old = path / "snapshots"
    if not old.is_dir():
        return
    mend = f"mv {old}/* {path}/ && rmdir {old}" if any(old.iterdir()) else f"rmdir {old}"
    raise ConfigError(f"{old}: earlier versions kept the wallet's snapshots there; they now lie in {path} itself. Move them up: {mend}")


def holders() -> list[str]:
    """The holders in the data directory: its top-level directories that hold a directory (a wallet, an account or
    ``statements/``); one holding none declares nothing yet, and a data directory of its own is another tree.  None outside a tree."""
    root = find_data_dir()
    if root is None or not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name not in RESERVED
        and not (p / CONFIG_FILE).is_file()
        and any(q.is_dir() for q in p.iterdir())
    )


def wallet_dirs() -> dict[str, Path]:
    """Every wallet in the tree, ``<dir>/<holder>/<wallet>``, by name; a name under two holders is an error."""
    found: dict[str, Path] = {}
    for holder in holders():
        for path in sorted((data_dir() / holder).iterdir()):
            if _is_wallet_dir(path):
                _check_layout(path)
                if path.name in found:
                    raise ConfigError(
                        f"wallet {path.name} is under two holders, {found[path.name].parent.name} and {holder}: name them apart"
                    )
                found[path.name] = path
    return found


def wallets() -> list[str]:
    """The wallets in the data directory, whichever holder they are under."""
    return sorted(wallet_dirs())


def holder_of_wallet(wallet: str) -> str | None:
    """The holder whose directory holds the wallet; None for a wallet not in the tree yet."""
    path = wallet_dirs().get(wallet)
    return path.parent.name if path is not None else None


def holder_of(wallet: str | None) -> str | None:
    """The holder a wallet's settings come from: the directory it is under, else the one a new wallet is made under
    (:func:`current_holder`); None when nothing says, or outside a tree."""
    found = holder_of_wallet(wallet) if wallet is not None else None
    if found is not None:
        return found
    try:
        return current_holder()
    except ConfigError:
        return None


def holder_of_cwd() -> str | None:
    """The holder whose directory the current directory is, or is under; None at the top or outside."""
    parts = _tree_parts(Path.cwd())
    return parts[0] if parts and not parts[0].startswith(".") else None


def current_holder() -> str:
    """The holder a new wallet is made under: ``-w KEY/NAME``, else the current directory's holder, else ``holder`` in
    ``bip322.toml``, else the tree's one holder; otherwise an error that says how to name one."""
    if _holder is not None:
        return _holder
    here = holder_of_cwd()
    if here is not None:
        return here
    named = Config.load().default_holder
    if named is not None:
        return named
    found = holders()
    if len(found) == 1:
        return found[0]
    raise ConfigError(
        "which holder? a wallet lives under one: -w KEY/NAME, or holder = KEY in bip322.toml, or run inside the holder's directory"
        + (f" (holders here: {', '.join(found)})" if found else "")
    )


def holder_dir(key: str) -> Path:
    """``<dir>/<holder>``."""
    return data_dir() / check_wallet_name(key)


def holder_statements_dir(key: str) -> Path:
    """``<dir>/<holder>/statements``: the holder's ledgers (bip322-report)."""
    return holder_dir(key) / "statements"


def wallet_dir(wallet: str) -> Path:
    """``<dir>/<holder>/<wallet>``: where the wallet is, or where it will be made (:func:`current_holder`)."""
    check_wallet_name(wallet)
    found = wallet_dirs().get(wallet)
    return found if found is not None else data_dir() / current_holder() / wallet


def only_wallet() -> str | None:
    """The one wallet in the data directory, when there is exactly one: what a command means when nothing names a wallet."""
    data_dir()  # a command that needs a wallet needs the tree: outside one, say so rather than "which wallet?"
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
    """``<date>-<height>``: a snapshot directory's name in the wallet's directory."""
    return f"{str(stamp['time'])[:10]}-{stamp['height']}"


def wallet_of_directory(directory: Path) -> str | None:
    """The wallet a snapshot directory belongs to by its place in the tree (``<holder>/<wallet>/<name>``); None elsewhere."""
    parts = _tree_parts(directory)
    if not parts or len(parts) != 3 or parts[0].startswith(".") or parts[1].startswith(".") or parts[1] in RESERVED:
        return None
    return parts[1]


def wallet_of_cwd() -> str | None:
    """The wallet whose directory the current directory is, or is under (``<dir>/<holder>/<wallet>/...``); None elsewhere."""
    parts = _tree_parts(Path.cwd())
    if not parts or len(parts) < 2 or parts[0].startswith(".") or parts[1].startswith(".") or parts[1] in RESERVED:
        return None
    return parts[1]


def _stamp_height(directory: Path) -> tuple[int, str]:
    try:
        return int(json.loads((directory / "snapshot.json").read_text())["stamp"]["height"]), directory.name
    except (OSError, ValueError, KeyError, TypeError):
        return -1, directory.name


def newest_snapshot(wallet: str, *, finalized: bool | None = None) -> Path:
    """The snapshot with the highest stamp in the wallet's directory; with ``finalized``, among those with (or without) a proofs file."""
    root = wallet_dir(wallet)
    found = [p for p in root.iterdir() if _is_snapshot_dir(p)] if root.is_dir() else []
    if finalized is not None:
        found = [p for p in found if (proofs_file_in(p) is not None) == finalized]
    if not found:
        what = "finalized snapshot" if finalized else "snapshot"
        raise ConfigError(f"no {what} under {root}")
    return max(found, key=_stamp_height)


def snapshot_directory(given: str | None, wallet: str | None, *, finalized: bool | None = None) -> Path:
    """The snapshot a command means: a path, a name in the wallet's directory, or nothing for the newest one there."""
    if given is not None:
        path = Path(given)
        if path.exists():
            return path
        if wallet is not None and (wallet_dir(wallet) / given).is_dir():
            return wallet_dir(wallet) / given
        raise ConfigError(f"{given}: no such snapshot" + (f" (not a path, and not under {wallet_dir(wallet)})" if wallet else ""))
    if wallet is None:
        raise ConfigError(f"which snapshot? name one, or the wallet with -w NAME (or wallet = NAME in {CONFIG_FILE}) for its newest")
    return newest_snapshot(wallet, finalized=finalized)


_SAMPLE_HEAD = """\
# bip322.toml: this is the bip322 directory: one directory per holder, one per
# wallet inside it; a command run here or below finds it. Top-level keys are
# defaults for every wallet; a [holders.KEY] table overrides them for the
# holder's wallets, a [wallets.NAME] table for one; the command line beats all.
"""
_SAMPLE_KEYS = (
    ('cli = "bitcoin-cli"', 'e.g. "bitcoin-cli -rpcconnect=10.0.0.5" or "bitcoin-cli -signet"'),
    ("timeout = 600", "seconds to wait for a node call"),
    None,
    ("dust = 1000", "satoshi; received outputs at or below this are left out of the statement"),
    ('theme = "journal"', "journal, paper, light or dark"),
    ('heading = "underline"', "underline, tab, number or redrule; default per theme"),
    ('explorer = "https://mempool.space"', '"" for no links; mainnet only by default'),
)


def _row(setting: str, note: str, on: bool = False) -> str:
    return f"{'' if on else '# '}{setting}".ljust(37) + f" # {note}"


def _key(name: str) -> str:
    """``name`` as a TOML key: bare when it can be, quoted otherwise."""
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def sample_config(holder: str | None = None, wallet: str | None = None) -> str:
    """``bip322.toml`` with every key the tools read, ``[holders.KEY]`` and ``[addresses]``: ``holder`` and its table, and the
    wallet's table, switched on when given, else every line a comment."""
    key, holder_on = (holder, True) if holder is not None else ("company", False)
    names, on = ([wallet], True) if wallet is not None else (["treasury", "personal"], False)
    lines = [
        _SAMPLE_HEAD,
        _row(f"holder = {json.dumps(key)}", "the default holder: the directory a new -w NAME is made in", holder_on),
    ]
    lines += [_row(*item) if item else "" for item in _SAMPLE_KEYS]
    lines += [
        "",
        f"{'' if holder_on else '# '}[holders.{_key(key)}]".ljust(38) + "# the holder: bip322 report, one statement over its wallets",
        _row('name = "Company AB"', "the statements' header, and the signed message: Proof of control, Company AB, <date>"),
    ]
    for name in names:
        lines += [
            "",
            f"{'' if on else '# '}[wallets.{_key(name)}]",
            _row(f'node = "{name}-2of3-main"', "the node wallet, when its name is not the directory's"),
        ]
    lines += [
        "",
        _row("[addresses]", "the other side of a movement, named in the statement's CSV"),
        _row('"bc1q..." = "Exchange"', "an address of theirs = the wallet it belongs to"),
    ]
    return "\n".join(lines) + "\n"


def write_config(directory: Path, holder: str | None = None, wallet: str | None = None) -> Path:
    """Make ``directory`` the data directory: write :func:`sample_config` as its ``bip322.toml``, creating the directory if need be,
    and the holder's directory and the wallet's under it when they are named, since a directory is a wallet from the moment it exists.

    An existing file is left alone, and a directory inside a data directory already is refused.
    """
    if holder is not None:
        check_wallet_name(holder)
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
        f.write(sample_config(holder, wallet))
    if holder is not None:
        (directory / holder / wallet if wallet is not None else directory / holder).mkdir(parents=True, exist_ok=True)
    return path
