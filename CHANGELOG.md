# Changelog

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
