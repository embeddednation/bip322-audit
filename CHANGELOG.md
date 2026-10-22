# Changelog

## 0.25.0 (2026-10-22)

An account's directory is marked by `movements.json`, its own record of
its deposits and withdrawals as bip322-report reads it, where 0.24 said
`ledger.json`: a ledger is the book of accounts, not one account's record,
and the word names a signing-device vendor besides. `ACCOUNT_FILE` replaces
`LEDGER_FILE`.

## 0.24.0 (2026-10-22)

The tree is the declaration. A directory under a holder is a wallet from the
moment it exists: `init -w KEY/NAME` makes `KEY/NAME/`, `mkdir` adds another,
and a statement knows the holder's wallets before any snapshot is taken. The
only directories under a holder that are not wallets are `statements/` and an
account, marked by its `ledger.json` (`LEDGER_FILE`; bip322-report's record of
a custodial account). Every top-level directory is a holder. The signed
message is the holder's name and the stamp's date, `Proof of control, Company
AB, 2026-12-31`, the name the statement's header carries, from `name` in
`[holders.KEY]` (else the key); `--text` gives another. The keys `wallet` and
`text` of `bip322.toml` are no longer read: the wallet is `-w`, the directory
the command runs in, the tree's one wallet, or the node's one loaded wallet;
a file that still has either key is told so (`Config.stale`).

## 0.23.0 (2026-10-21)

The stamp block is chosen, and it is the block the coins are listed at.
`snapshot`, `prove` and `stamp` take `--before WHEN` (the last block before
a date or instant: `--before 2027-01-01` is the last block of 2026, the block
the statement of 2026 closes on), `--at HEIGHT|HASH`, or `--depth N` behind
the tip as before; `snapshot` and `stamp` default to the last block before
today 00:00 UTC, the night before, which a statement to date closes on, and
`prove` keeps six behind the tip. The snapshot lists the wallet's coins at
that block: what `listunspent` has, and from the node wallet's own history
what the wallet has spent since (`listsinceblock` from the stamp block, as
`finalize` finds the spends), so the year-end snapshot can be taken whenever
the year is over; `snapshot.json` and the summary count those under
`spent_since`. A `scantxoutset` cannot see them, and the command says so.
The snapshot's directory is named for the stamp block as before, so the
year-end snapshot reads `2026-12-31-931871`. `take_snapshot` takes `at`,
`before` and `depth` (none: the night before); `stamp` gains `parse_when`,
`header_at`, `header_before`, `choose_stamp`, `start_of_today` and
`Stamp.from_header`, and `holdings.resolve_block` is built on them.

## 0.22.1 (2026-10-20)

The sample `bip322.toml` shows `text` under `[wallets.NAME]` where it showed
`dust`: bip322-report's statement is the holder's and reads `dust`, `theme`,
`heading` and `explorer` from `[holders.KEY]` and the top level, not from a
wallet's table, which carries what is read per wallet.

## 0.22.0 (2026-10-20)

A wallet's directory holds its snapshots and its history, nothing else:
`<holder>/<wallet>/<date>-<height>/` beside `<holder>/<wallet>/history.json`,
where `<holder>/<wallet>/snapshots/<date>-<height>/` was. The wallet's own
`statements/` goes with bip322-report's per-wallet statement; the holder's
`statements/` stays. A wallet is recognised by a snapshot or a history in
it, and is made by its first snapshot; a snapshot's name resolves in the
wallet's directory. A wallet laid out as before, with a `snapshots/`, is
refused with the command that moves its snapshots up
(`mv <wallet>/snapshots/* <wallet>/ && rmdir <wallet>/snapshots`); an old
`<wallet>/statements/` is simply not read. `datadir` loses `snapshots_dir`
and `statements_dir` and gains `HISTORY_FILE`; `find_proofs` looks one level
below a root, no deeper, so copies of proofs files beside old statements are
not found twice.

## 0.21.0 (2026-10-20)

`checksigners [SNAPSHOT]`: bip322-core's device health check over a
snapshot's PSBTs, the snapshot named as `finalize` names it (its name under
the wallet's snapshots/, a directory, or the wallet's newest; the current
directory when run inside one). The PSBTs are read from the snapshot's
`to_sign/`, `signed/` and root, grouped by address and combined, as
`finalize` reads them; one report per address, one RESULT line, `--json`
for the reports with every combination's proof. Exit 0 when every address
passes, 1 when a signer or a combination fails, 2 when the check could not
run. `scan_psbts` is public in `bip322audit.audit`.

## 0.20.0 (2026-10-16)

Settings per holder: a key in a `[holders.KEY]` table (`text`, `cli`,
`timeout`, and what bip322-report reads) applies to the holder's wallets,
between the wallet's own `[wallets.NAME]` table and the top level. A wallet's
holder is the directory it is under; a wallet not in the tree yet takes the
holder it would be made under. `Config.get(..., holder=KEY)` names one
outright; `holder_of(wallet)` is the resolution. The `init` sample shows a
holder's `text`.

## 0.19.0 (2026-10-15)

Holders above wallets: the tree is `<holder>/<wallet>/snapshots/…` and
`<holder>/<wallet>/statements/…`, with the holder's own ledgers in
`<holder>/statements/` (bip322-report). A wallet's name is unique in the
tree, so `-w NAME` finds it under whichever holder it is; a wallet not there
yet is made under `-w KEY/NAME`'s holder, else the holder whose directory the
command runs in, else `holder` in `bip322.toml`, else the tree's one holder.
`bip322 init -w KEY/NAME` (or `--holder KEY`) switches the holder on in the
sample, whose `[holders.KEY]` table now carries only `name`; `holder = KEY`
at the top is the default holder. `Config.default_holder`, `holder_name(key)`;
`holders()`, `wallet_dirs()`, `holder_of_wallet`, `holder_of_cwd`,
`current_holder`, `holder_statements_dir`, `name_wallet`, `use_holder`;
`holders/` and `wallets = […]` are gone.

## 0.18.0 (2026-10-15)

- `[holders.KEY]` in `bip322.toml` groups wallets explicitly: `name` (the
  statements' header; default KEY) and `wallets`, the wallet directories that
  are the holder's; a wallet is listed under one holder only, and the key is
  the holder's directory name under `holders/`. `Config.holders`,
  `holder_of`, `holder_name` (the latter falls back to the `holder` key).
  The sample from `init` shows the table and no longer the `holder` key.

## 0.17.0 (2026-10-15)

- `holders/` in the data directory, beside the wallets, for bip322-report's
  holder ledgers (`holder_dir`). A wallet is a directory holding a
  `snapshots/` or a `statements/`: `wallets()` no longer takes any directory
  written into the data directory for one. The sample's note on `holder`
  says that the wallets with one holder make one holder ledger.

## 0.16.0 (2026-10-15)

- `[addresses]` in `bip322.toml`: the wallet each address of someone else
  belongs to (`"bc1q..." = "Exchange"`), read by bip322-report to name the
  other side of a movement. `bip322 init` shows it in the sample.

## 0.15.1 (2026-10-15)

Needs bip322-core 0.12: `bip322 checksigners signed/*.psbt` checks every
address of a snapshot at once, one block per address and one verdict.

## 0.15.0 (2026-10-13)

Like git: `bip322 init [DIR]` makes a directory the data directory by writing
`bip322.toml` into it (the file was `config.toml`, the directory `~/bip322`),
and every command run in it or below finds it. There is no default directory
any more; `--dir DIR` and `BIP322_DIR` still name one from elsewhere. Inside
a wallet's directory that wallet is the one meant, and inside a snapshot's
directory `finalize` and `verify` mean that snapshot. `init -w NAME` switches
`wallet = NAME` and its table on; `init NAME…` is gone.

Upgrading: `cd ~/bip322 && mv config.toml bip322.toml`.

## 0.14.0 (2026-10-13)

- `init [NAME…]` writes `config.toml` into the data directory with every key
  the tools read: each NAME as a `[wallets.NAME]` table, the first as
  `wallet`, the rest as comments to switch on. An existing file is left
  alone. `bip322 init` runs it too (a `bip322-init` script).

## 0.13.1 (2026-10-12)

- `--dir DIR` puts the tree elsewhere than `~/bip322` (as `BIP322_DIR` does).
- `-w`, `--dir`, `--cli` and `--timeout` go before or after any command;
  `verify -w NAME` and `stamp -w NAME` were refused.

## 0.13.0 (2026-10-12)

One tree, named by the tools: `~/bip322/<wallet>/snapshots/<date>-<height>/`
(`BIP322_DIR` moves it), with `~/bip322/config.toml` for the defaults
(`wallet`, `cli`, `timeout`, `text`; `node` per wallet). `-w` names the
wallet, else `config.toml`, else the node's one loaded wallet.

- `snapshot` and `prove` write into the wallet's `snapshots/` by default;
  `-o DIR` still writes elsewhere. `--skip-proven` is a flag: it reads the
  wallet's own snapshots. `prove --ledger` is gone.
- `finalize` writes `<wallet>-<date>-<height>-proofs.json`, a name the file
  keeps wherever it goes; a `proofs.json` of an earlier version is still read
  and, when present, written to. `finalize` and `verify` take a snapshot's
  name under the wallet's `snapshots/`, a path, or nothing for the newest.
- The words: a snapshot (the directory, whichever command wrote it) and its
  proofs file; "bundle" and "ledger" are gone from the commands, the help
  and the documentation. Module `ledger.py` is now `proofs.py`;
  `finalize_bundle` and `write_bundle` are `finalize_snapshot` and
  `write_snapshot`.
- Requires bip322-core 0.11.

## 0.12.1 (2026-10-06)

First public release.

- `snapshot`: a bundle of BIP-322 PSBTs, one per funded address of the node
  wallet, over a message that ends with a block stamp.
- `prove ADDRESS...`: the same for addresses that hold nothing yet.
- `finalize`: `proofs.json` from the signed PSBTs: addresses, proofs and
  outputs, and nothing about the wallet behind them.
- `verify`: every proof, the stamp block, and every listed output against a
  node. OK, INCOMPLETE or FAILED, in the report and in the exit code.
- `holdings`: what outputs or addresses hold, now or at a block.
- A ledger of bundles: `--skip-proven`, `prove --ledger`.
- Requires bip322-core 0.11.
