# Changelog

## 0.9.5 (2026-09-17)

- Depends on bip322-core 0.8.1.

## 0.9.4 (2026-09-17)

- Depends on bip322-core 0.8.0.

## 0.9.3 (2026-09-17)

- Depends on bip322-core 0.7.0.

## 0.9.2 (2026-09-17)

- `holdings` prints the script each output is locked to ("locked to" the address, and the script bytes): the link between an output and the address whose control a proof shows.

## 0.9.1 (2026-09-17)

- `holdings` takes outputs (`TXID:VOUT`, a direct lookup) as well as
  addresses (a UTXO-set scan), and prints a labelled block per output with
  lines short enough to quote in a statement (0.9.0 had longer lines).

## 0.8.1 (2026-09-17)

- Depends on bip322-core 0.6.2.

## 0.8.0 (2026-09-17)

- `holdings ADDRESS... [--at HEIGHT|HASH]`: what addresses hold, from a
  UTXO-set scan, and with `--at` the part confirmed by a block. The on-chain
  step a reader of a statement runs; no wallet, no index. Text or `--json`.

## 0.7.1 (2026-09-17)

- Depends on bip322-core 0.6.0: `bip322 audit ...` reaches this package through the core's git-style dispatch.

## 0.7.0 (2026-09-17)

- `prove ADDRESS...`: a bundle for given addresses of the wallet whether or
  not they hold coins yet, for a change address before the spend is
  broadcast or a deposit address before the deposit. An address that is not
  the wallet's is refused.
- `snapshot --skip-proven` now skips by address, not by output: a BIP-322
  proof is about an address, so an address proven once covers every output
  paid to it, before or after the proof. `bip322audit.ledger.proven_addresses`.
- Packaging: `bip322-core` is a direct git dependency at a pinned tag, so
  one `pip install` of a git URL installs the stack; the `kernel` extra
  chains to the core's.

## 0.6.0 (2026-09-17)

- `snapshot --skip-proven DIR`: leave out outputs that a `proofs.json` under
  DIR already lists, so a bundle can cover only newly created outputs (for
  example the change of a spend). With one DIR and no `-o` the new bundle is
  written into it. `snapshot.json` records `skipped_proven`.
- `bip322audit.ledger`: find every proofs document under a directory tree
  and the set of outputs they prove. A ledger is any directory holding
  bundles; bip322-reports reads it.

## 0.5.0 (2026-09-16)

- First release as its own repository and distribution. The history of the
  files, and the changelog entries up to 0.4.0, are in
  [bip322-core](https://github.com/embeddednation/bip322-core), from which
  `bip322audit/`, its tests and the walkthrough were split with history.
- Requires `bip322-core>=0.5,<1`, the first release without the bundled
  audit package.
