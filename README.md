# bip322-audit

Proof of control of a wallet's coins at a block: one BIP-322 proof per funded
address, a block stamp inside the signed message, and the on-chain checks an
auditor runs on their own node. Built on
[bip322-core](https://github.com/embeddednation/bip322-core), which does the
BIP-322 work and never talks to a node; everything that does lives here, and
only through `bitcoin-cli`.

Two roles:

* **Owner**: `snapshot` (or `prove`, for given addresses), sign on the
  cosigners' devices, `finalize`. Needs a node with the wallet loaded
  (watch-only is enough).
* **Auditor**: `verify`, against any node that has the chain; `holdings` to
  look up single outputs or addresses. No wallet, no index, no key material,
  and nothing about the owner's wallet beyond the proven addresses.

The commands: `stamp`, `snapshot`, `prove`, `finalize`, `verify`, `holdings`,
`help`. With bip322-core installed they are also reachable as `bip322 audit ...`.

## Install

One line, into a fresh venv, with bip322-core pulled in:

```sh
python3 -m venv ~/.bip322 && ~/.bip322/bin/pip install "bip322-audit[kernel]"
export PATH="$HOME/.bip322/bin:$PATH"
```

Python 3.11 or newer (on Ubuntu 22.04: `apt install python3.12 python3.12-venv`
from the deadsnakes PPA, then `python3.12 -m venv ~/.bip322`); leave out
`[kernel]` on anything but CPython 3.12 / Linux x86_64.

For a reproducible, hash-pinned install (what an auditor should do), clone and use the setup script:

```sh
git clone https://github.com/embeddednation/bip322-audit.git && cd bip322-audit
./setup.sh                          # venv, hash-pinned dependencies, bip322-core at the pinned tag, tests
export PATH="$PWD/.venv/bin:$PATH"
```

`setup.sh` installs `bip322-core` from its git repository at the tag named
by `CORE_REF` in the script. `./setup.sh --core ../bip322-core` installs a local
checkout instead, which is the development setup and also the auditor's: clone both
repositories at the tags the owner names, check the commit hashes out of
band, and run `verify`.

The whole yearly flow across the three packages is in bip322-report's [handbook](https://github.com/embeddednation/bip322-report/blob/main/bip322report/handbook.md).

## Workflow

For "we controlled these coins as of block N", repeatable whenever coins move:

```sh
bip322-audit -w treasury snapshot --text "Annual audit {date}"      # the node wallet's own descriptor
#   -> snapshot-2026-09-14-912345/: snapshot.json, message.txt, to_sign/to_sign-01.psbt ... one per funded address
#   sign every PSBT on the cosigners' devices, put the results into snapshot-.../signed/
bip322-audit finalize snapshot-2026-09-14-912345           # -> proofs.json (hand this to the auditor)
bip322-audit verify snapshot-2026-09-14-912345 --report audit-report.json
```

`finalize` also asks the node wallet the coins came from (recorded in
`snapshot.json`) which listed outputs have been spent since, and records the
spending transactions in `proofs.json`. If coins move between the snapshot and
the audit, re-run `finalize` before handing `proofs.json` over; the proofs
themselves do not change. `--offline` skips that step and keeps the spends an
earlier run recorded. A PSBT that cannot be used is reported with its address
and file names, and files in `to_sign/` and `signed/` that were not used are
listed.

The node is reached with `--cli CMD` (default `bitcoin-cli`). `CMD` is split
like a shell command line, so a path with spaces is quoted inside it:
`--cli "'/opt/my node/bitcoin-cli' -signet"`. `--timeout SECONDS` bounds every
node call (default 600); a UTXO-set scan the tool gave up on keeps running on
the node. Chains: `main`, `test`, `testnet4`, `signet`, `regtest`.

`snapshot` reads the wallet's descriptor from the node wallet (`-w NAME`,
`listdescriptors`; `--descriptor FILE` overrides and is cross-checked against
the node), takes the block six behind the tip (`--depth`) as the stamp *and*
the snapshot height: the message ends with `block: HEIGHT HASH TIME` taken
from that block, and only outputs confirmed at that block are listed. Coins
come from `listunspent` on the node wallet (the only one loaded, or `-w NAME`)
or, when the node has no wallet, from a
`scantxoutset` of the descriptor (minutes on mainnet; the command says so
before it starts; `--source` forces either). The stamp, the coins, their
heights and block hashes are read under one tip: if a block arrives
meanwhile, they are read again. The template accepts `{date}`, `{time}`,
`{height}`, `{hash}` (date and time are the stamp block's header time, UTC);
anything else in braces is refused, as is a template with a `block:` line of
its own, and the message is checked against hardware signers' message rules.

`snapshot` covers one descriptor (`wsh(multi/sortedmulti)` or `wpkh`). Its
summary and `snapshot.json` say which (`descriptor`), and under `left_out`
how many outputs and how much the node listed that the bundle does not, and
why: coins of another descriptor in the same node wallet, coins on addresses
an earlier bundle proves. Descriptors of a kind it does not cover are named
in `notes`. The default directory is `snapshot-<date>-<height>`, with `-2`,
`-3` ... when that exists; `-o DIR --force` rewrites a bundle that holds no
signed PSBTs and no `proofs.json` yet, and nothing else.

`proofs.json` names addresses, not a wallet: the message, the stamp, and per
address the proof and its outputs, plus the policy string (`2 of 3`). It is
built from a fixed list of fields (`tool`, `chain`, `stamp`, `message`,
`message_hex`, `policy`, `finalized_utc`, `total_sat`, `total_btc`, `spends`,
`spends_utc`, and per proof `address`, `signature`, `variant`, `total_sat`
and the outputs' `txid`, `vout`, `amount_sat`, `height`, `blockhash`), with
the proofs in address order and only `smp` proofs. No
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
before the stamp is a contradiction (`spent_before_snapshot`). A spent output
with no recorded spend, or with one the node cannot find or whose block left
the main chain, is `spent_time_unknown`: it existed, and nothing shows when
it was spent, so it is not counted as held. The report's rows are built from
what the node answered; nothing else in `proofs.json` is copied into them.
Totals are computed from the listed outputs: `claimed`, `held at the stamp`
(shown by the node) and `unspent now`. A `total_sat` that disagrees, an
output or an address listed twice, a recorded spend that does not spend its
output, and a document with no proofs are problems of the document.

The result is one of three:

* **OK**: the signatures and the stamp check out, the document is consistent,
  and every listed output is shown held at the stamp block (unspent now, or
  spent after the stamp by a checked spend).
* **INCOMPLETE**: nothing failed and nothing is contradicted, but some listed
  output is not shown held at the stamp block. The owner re-runs `finalize`
  on the node wallet to record the spends.
* **FAILED**: a signature, the stamp or the document does not check out, or
  the node contradicts a listed output.

In the JSON report the verdict is `result` (`ok`, `incomplete`, `failed`).
The key `ok` is true only for `ok`; `totals.held_at_stamp_sat` is what the node
showed held.

`--offline` verifies signatures and the document only.

## Exit codes

| command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `verify` | result OK | result FAILED | could not run | result INCOMPLETE |
| `stamp`, `snapshot`, `prove`, `finalize`, `holdings`, `help` | done | not used | could not run | not used |

"Could not run" is bad input (an unknown placeholder, a file that is not a
proofs document, a directory that exists), a node that cannot be reached or
answers something that is not JSON, or a bundle that cannot be finalized;
the message is one line on stderr starting with `error:`.

Completeness (that the listed addresses are all the holdings in scope) is not
something a key or a scan can establish, since nothing rules out a second
wallet. It comes from the audited party's representation and from
reconciling one year's spends to the next year's proofs, which the recorded
spends make possible.

`examples/audit_walkthrough.sh [DIR]` runs the whole thing on a throwaway
regtest node, including spending a coin after the snapshot (INCOMPLETE until
`finalize` is re-run). It writes into a new directory and refuses one that
exists and is not empty.

## Checking holdings on chain

```sh
bip322-audit holdings 7a1b...:0 3c9d...:1 --at 912345    # by output: a direct lookup, instant
bip322-audit holdings bc1q... --at 912345                 # by address: a UTXO-set scan, minutes on mainnet
```

For each output: the scriptPubKey it is locked to, its amount, and whether
it is unspent; with `--at`, whether it was held at that block (an output
confirmed at or before the block and unspent now was; `gettxout` only finds
unspent outputs). The JSON adds the address and the confirmation block and
time. This is how a reader of a statement checks its closing holdings. Coins spent since cannot
show; the owner's records name them and `verify` checks those. A bech32
address may be given in upper case.

## Bundles over time: a ledger

Keep every bundle in one directory tree and it becomes the wallet's ledger of
proofs. Two ways to add to it:

```sh
bip322-audit -w treasury snapshot --text "Proof of control {date}" --skip-proven ledger
#   -> ledger/snapshot-<date>-<height>/ with only the outputs no earlier bundle proves (new change, new deposits)
bip322-audit -w treasury snapshot --text "Proof of control, audit FY2026, {date}" -o ledger/audit-2026
#   -> every output the wallet holds, after the period's end, under a message that names the audit
```

The block stamp in every message is the timestamp an audit needs: it shows
the proof was made after that block, it can be checked on any node at any
later date, and it cannot pass for another year. The auditor's request is
therefore simply "a full bundle with a stamp after the period's last block";
their engagement reference may go in the text if they want it in their file.

A third way, for an address that holds nothing yet:

```sh
bip322-audit -w treasury prove bc1q...change... --text "Proof of control {date}" --ledger ledger
#   -> a bundle for exactly that address; refused if it is not the wallet's
```

That is the check to run on the change address of a spend before broadcasting
it: sign, `finalize`, and a valid proof means the quorum controls where the
change goes. A proof is about an address, so every output paid to it later
is covered too, and `--skip-proven` skips addresses proven that way.

`--skip-proven DIR` must exist, and a `proofs.json` under it that cannot be
read (a truncated file) is named in a warning, since it proves nothing.

Sign and `finalize` each as usual. `bip322-audit verify` checks one bundle;
[bip322-report](https://github.com/embeddednation/bip322-report) reads the
whole ledger to produce balance reports in which every output is backed by a
verified proof.

## Layout

```
bip322audit/stamp.py      the block stamp: fetch, parse, compose the message, check
bip322audit/snapshot.py   coins at the stamp block (listunspent / scantxoutset), PSBT per address, the bundle (snapshot, prove)
bip322audit/audit.py      finalize (combine, finalize, self-verify, record spends) and verify (the report)
bip322audit/holdings.py   what outputs or addresses hold now, and at a block (holdings)
bip322audit/ledger.py     a directory tree of bundles: which addresses and outputs earlier proofs cover
bip322audit/rpc.py        bitcoin-cli as a subprocess
bip322audit/cli.py        the bip322-audit command
bip322audit/testing.py    a throwaway regtest node for the regtest test and the walkthrough
tests/                    fake-node tests (test_audit.py, test_review_fixes.py), the pin test, and an end-to-end test on a real regtest Bitcoin Core
examples/audit_walkthrough.sh   the whole flow on a throwaway regtest node
```

The regtest test and the walkthrough need a Bitcoin Core binary: set
`BITCOIN_CORE_DIR`, or run `refcheck/fetch.sh` in the bip322-core checkout
the venv was set up from. Without one the test skips.

CI checks out bip322-core at the pinned tag.
