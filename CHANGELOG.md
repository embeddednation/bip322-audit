# Changelog

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
