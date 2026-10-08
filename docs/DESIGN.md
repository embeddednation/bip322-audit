# bip322-audit: design notes

What the tool claims, what it checks, and what it deliberately does not do.
The BIP-322 side (message framing, PSBT construction, finalization, the
verifier and its engines) is documented in bip322-core's `docs/DESIGN.md`;
this package only calls it.

## Trust boundary

`bip322audit` is the only chain-facing code: it reaches the node through
`bitcoin-cli` as a subprocess and nothing else. It never sees private keys.
bip322-core's test suite asserts that the core package never imports this
one, nor subprocesses, sockets or HTTP.

## The workflow

The node is reached only through `bitcoin-cli`, so the user's node, chain and
credentials are what is trusted and the tool holds none. The commands are
`stamp`, `snapshot`, `prove`, `finalize`, `verify` and `holdings`.

* **Stamp.** `block: HEIGHT HASH TIME`, all three from the block `depth`
  behind the tip (default 6). The hash is a *not before* bound that anyone can
  check on any node, now or years later; height and header time make it
  readable and checkable with one `getblockheader`. It is part of the signed
  message, never PSBT metadata. It is the freshness evidence an audit needs:
  a proof stamped after the period's last block shows control after the
  period's end, and it cannot pass for another year. No random nonce is
  needed for that; an auditor may still ask for their engagement reference in
  the text, which costs nothing and documents the request in their file.
* **Snapshot semantics.** The stamp block is the snapshot block; only outputs
  confirmed at or before it are listed, so a bundle means "these coins, at
  that block". Coins come from `listunspent` (a Core wallet with the
  descriptor; its `desc` field gives branch and index) or `scantxoutset`.
  Everything is read under one tip: the tip is read before and after the
  coins, and if it moved, the stamp and the coins are read again, so heights
  counted back from the tip and the block hashes recorded for them cannot
  belong to different moments. `verify` and `holdings` count a creation
  height back from the block `gettxout` itself answered at (`bestblock`),
  never from a tip read earlier. What the node listed and the bundle does
  not (another descriptor's coins, addresses proven earlier) is counted and
  recorded in `snapshot.json` with the reason.
* **`prove`.** A bundle for exactly the addresses given, whether or not they
  hold coins yet: the check to run on a change address before broadcasting.
* **`holdings`.** A reader's lookup, by output (`gettxout`) or by address
  (`scantxoutset`): unspent now, and with `--at` whether confirmed at or
  before that block. It needs no bundle.
* **The ledger.** Any directory tree of bundles. `--skip-proven` reads the
  `proofs.json` files under it to leave out addresses already proven; a file
  it cannot read is reported, never silently taken as "nothing proven".
* **Proof of funds without `pof`.** One `smp` proof per funded address; the
  auditor establishes the coins from the chain. Nothing signed references a
  real output, so the safety of `pof`'s bogus-input construction is never
  relied on. Outpoints are not repeated in the message (control of the script
  covers every output paying to it).
* **Verdict.** The verdict never says more than the node showed. `verify` is
  OK when every signature is valid, the stamp block is in the node's main
  chain with the claimed height and time, the document is consistent (its
  recorded stamp equals the one inside the signed message, `message` and
  `message_hex` agree, `total_sat` equals the sum of the listed outputs, no
  output or address is listed twice, there is at least one proof, a recorded
  spend spends its output), and every listed output is shown held at the
  stamp block. It is FAILED when one of the first three does not hold or the
  node contradicts a listed output (different amount, address or creation
  height; spent at or before the stamp). It is INCOMPLETE when nothing failed
  but some listed output is spent and nothing shows when; such an output is
  not counted as verified or held. Report rows are built from named fields
  and the node's answers, never by copying the document's objects, and the
  totals are computed, not read.
  Outputs no longer in the UTXO set are fetched by the block hash the snapshot
  recorded for their creating transaction (`getrawtransaction TXID true HASH`
  works on any node), which confirms they existed at the snapshot block with
  the claimed amount and address. "Unspent at the snapshot" is then shown by
  the spending transaction: `finalize` records it on the owner's side
  (`listsinceblock` from the stamp block on the node wallet named in
  `snapshot.json`, so no address index anywhere), and `verify` checks that it
  spends the output and was confirmed after the stamp block. Re-running
  `finalize` refreshes the record; the proofs are unchanged. Without it the
  row is `spent_time_unknown` (existence shown, held-at-stamp not) and the
  result is INCOMPLETE; the same when the recorded spending block has left
  the main chain. A spend at or before the stamp is a contradiction.
* **Addresses, not a wallet.** `proofs.json` carries no descriptor, xpub,
  derivation path or node wallet name. `finalize` builds it from a fixed list
  of fields, so a field added to `snapshot.json` later cannot leak by
  default; proofs are in address order, not derivation order, and only `smp`
  proofs are written (a full-format proof would embed the transaction). The claim under audit is about control
  of listed coins at a block, and the signatures plus the chain settle it per
  address; that the addresses share a parent key is bookkeeping, and the
  policy is visible in each witness anyway. The xpubs would let the auditor
  derive every address of the wallet, past and future, and a scan of one
  descriptor would suggest a completeness that it cannot establish (nothing
  rules out a second wallet). Completeness comes from the audited party's
  representation and from reconciling spends between snapshots, which the
  recorded spends support. The owner's `snapshot.json` keeps the descriptor
  and the derivation paths.
* **Device health check.** `bip322 checksigners` takes the devices' PSBT files,
  shows the script behind the input and its mapping back to the address,
  verifies each cosigner's signature alone, and finalizes and verifies one
  proof per threshold-sized combination. `decodesignature` opens any proof
  string into labelled witness elements (taproot-aware).

## Exit codes

Every command: 0 done, 2 could not run (bad input, a file or the node not
reachable, a bundle that cannot be finalized), with one `error:` line on
stderr. `verify` reports its result in the exit code as well: 0 OK, 1 FAILED,
3 INCOMPLETE. The three are also in the report as `result` (`ok`, `failed`,
`incomplete`). The key `ok` is true only for `ok`; a reader who asks whether a bundle's proofs
stand, whatever became of its outputs since (bip322-report), reads `result` and
accepts `incomplete`.

## Files

`proofs.json`, `snapshot.json` and the `--report` file are written beside
their target and renamed over it, so an interrupted run leaves the earlier
file whole.
