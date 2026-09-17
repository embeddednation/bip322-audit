# Changelog

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
