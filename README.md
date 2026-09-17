# bip322-audit

Proof of control of a wallet's coins at a block: one BIP-322 proof per funded
address, a block stamp inside the signed message, and the on-chain checks an
auditor runs on their own node. Built on
[bip322-core](https://github.com/embeddednation/bip322-core), which does the
BIP-322 work and never talks to a node; everything that does lives here, and
only through `bitcoin-cli`.

Two roles:

* **Owner**: `snapshot`, sign on the cosigners' devices, `finalize`. Needs a
  node with the wallet loaded (watch-only is enough).
* **Auditor**: `verify`, against any node that has the chain. No wallet, no
  index, no key material, and nothing about the owner's wallet beyond the
  proven addresses.

## Install

```sh
git clone git@github.com:embeddednation/bip322-audit.git && cd bip322-audit
./setup.sh                          # venv, hash-pinned dependencies, bip322-core at the pinned tag, tests
export PATH="$PWD/.venv/bin:$PATH"
```

`setup.sh` installs `bip322-core` from its git repository at the tag named
by `CORE_REF` in the script (SSH access to that repository is needed while it
is not on PyPI). `./setup.sh --core ../bip322-core` installs a local checkout
instead, which is the development setup and also the auditor's: clone both
repositories at the tags the owner names, check the commit hashes out of
band, and run `verify`.

## Workflow

For "we controlled these coins as of block N", repeatable whenever coins move:

```sh
bip322-audit -w treasury snapshot --text "Annual audit {date}"      # the node wallet's own descriptor
#   -> snapshot-2026-09-14-912345/: snapshot.json, message.txt, to_sign/to_sign-01.psbt ... one per funded address
#   sign every PSBT on two Coldcards, put the results into snapshot-.../signed/
bip322-audit finalize snapshot-2026-09-14-912345           # -> proofs.json (hand this to the auditor)
bip322-audit verify snapshot-2026-09-14-912345 --report audit-report.json
```

`finalize` also asks the node wallet the coins came from (recorded in
`snapshot.json`) which listed outputs have been spent since, and records the
spending transactions in `proofs.json`. If coins move between the snapshot and
the audit, re-run `finalize` before handing `proofs.json` over; the proofs
themselves do not change. `--offline` skips that step.

`snapshot` reads the wallet's descriptor from the node wallet (`-w NAME`,
`listdescriptors`; `--descriptor FILE` overrides and is cross-checked against
the node), takes the block six behind the tip (`--depth`) as the stamp *and*
the snapshot height: the message ends with `block: HEIGHT HASH TIME` taken
from that block, and only outputs confirmed at that block are listed. Coins
come from `listunspent` on the node wallet (the only one loaded, or `-w NAME`)
or, when the node has no wallet, from a
`scantxoutset` of the descriptor (minutes on mainnet; the command says so
before it starts; `--source` forces either). The template accepts
`{date}`, `{time}`, `{height}`, `{hash}`, and is checked against Coldcard's
message rules.

`proofs.json` names addresses, not a wallet: the message, the stamp, and per
address the proof and its outputs, plus the policy string (`2 of 3`). No
descriptor, no xpubs, no derivation paths and no node wallet name go in. The
proofs stand per address, and the xpubs would let the auditor derive every
address of the wallet, past and future, which no check of the claim needs.
`snapshot.json`, the owner's copy, keeps all of it.

`verify` re-checks everything on the auditor's node: each BIP-322 signature,
the stamp block (`getblockheader`: height, time, in main chain), that the
document is consistent with the signed message, and each listed output.
An output still unspent is checked with `gettxout` (amount, address,
creation height at or before the stamp), which also shows it was unspent at
the stamp. An output spent since is fetched by the block hash the snapshot
recorded (no `-txindex` needed): that shows it existed at the stamp with the
claimed amount and address. Whether it was still *unspent* at the stamp needs
the spending transaction, which only an address index or the owner's wallet
knows; `finalize` records it in `proofs.json` from the node wallet's history
(`listsinceblock` from the stamp block), and `verify` checks that it really
spends the output and was confirmed after the stamp block. A spend at or
before the stamp is a contradiction. The result is OK
when the signatures and the stamp check out, the document is consistent, and
the node contradicts nothing; coins spent since the snapshot are reported,
not failures. `--offline` verifies signatures only.

Completeness (that the listed addresses are all the holdings in scope) is not
something a key or a scan can establish, since nothing rules out a second
wallet. It comes from the audited party's representation and from
reconciling one year's spends to the next year's proofs, which the recorded
spends make possible.

`examples/audit_walkthrough.sh` runs the whole thing on a throwaway regtest
node, including spending a coin after the snapshot.

## Bundles over time: a ledger

Keep every bundle in one directory tree and it becomes the wallet's ledger of
proofs. Two ways to add to it:

```sh
bip322-audit -w treasury snapshot --text "Proof of control {date}" --skip-proven ledger
#   -> ledger/snapshot-<date>-<height>/ with only the outputs no earlier bundle proves (new change, new deposits)
bip322-audit -w treasury snapshot --text "Audit 2026, Firm X ref 1234, {date}" -o ledger/audit-2026
#   -> every output the wallet holds, for the auditor's own message
```

Sign and `finalize` each as usual. `bip322-audit verify` checks one bundle;
[bip322-reports](https://github.com/embeddednation/bip322-reports) reads the
whole ledger to produce balance reports in which every output is backed by a
verified proof.

## Layout

```
bip322audit/stamp.py      the block stamp: fetch, parse, compose the message, check
bip322audit/snapshot.py   coins at the stamp block (listunspent / scantxoutset), PSBT per address, the bundle
bip322audit/audit.py      finalize (combine, finalize, self-verify, record spends) and verify (the report)
bip322audit/rpc.py        bitcoin-cli as a subprocess
bip322audit/cli.py        the bip322-audit command
tests/                    fake-node tests and an end-to-end test on a real regtest Bitcoin Core
examples/audit_walkthrough.sh   the whole flow on a throwaway regtest node
```

The regtest test and the walkthrough need a Bitcoin Core binary: set
`BITCOIN_CORE_DIR`, or run `refcheck/fetch.sh` in the bip322-core checkout
the venv was set up from. Without one the test skips.

CI checks out bip322-core at the pinned tag over SSH; the repository secret
`BIP322_CORE_DEPLOY_KEY` must hold a read-only deploy key of that repository.
