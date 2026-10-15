# Changelog

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
