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

Everything chain-facing lives in a separate package with its own command,
`bip322-audit`; a test asserts that `bip322core/` never imports it, nor
`subprocess`, sockets or HTTP. The node is reached only through `bitcoin-cli`,
so the user's node, chain and credentials are what is trusted and the tool
holds none.

* **Stamp.** `block: HEIGHT HASH TIME`, all three from the block `depth`
  behind the tip (default 6). The hash is a *not before* bound; height and
  header time make it readable and checkable with one `getblockheader`. It is
  part of the signed message, never PSBT metadata. It is not replay
  protection: a counterparty wanting freshness supplies a nonce.
* **Snapshot semantics.** The stamp block is the snapshot block; only outputs
  confirmed at or before it are listed, so a bundle means "these coins, at
  that block". Coins come from `listunspent` (a Core wallet with the
  descriptor; its `desc` field gives branch and index) or `scantxoutset`.
* **Proof of funds without `pof`.** One `smp` proof per funded address; the
  auditor establishes the coins from the chain. Nothing signed references a
  real output, so the safety of `pof`'s bogus-input construction is never
  relied on. Outpoints are not repeated in the message (control of the script
  covers every output paying to it).
* **Verdict.** `verify` is OK when every signature is valid, the stamp block
  is in the node's main chain with the claimed height and time, the document
  is consistent (its recorded stamp equals the one inside the signed message,
  `message` and `message_hex` agree), and no listed output is contradicted by
  the node (different amount, address or creation height).
  Outputs no longer in the UTXO set are fetched by the block hash the snapshot
  recorded for their creating transaction (`getrawtransaction TXID true HASH`
  works on any node), which confirms they existed at the snapshot block with
  the claimed amount and address. "Unspent at the snapshot" is then shown by
  the spending transaction: `finalize` records it on the owner's side
  (`listsinceblock` from the stamp block on the node wallet named in
  `snapshot.json`, so no address index anywhere), and `verify` checks that it
  spends the output and was confirmed after the stamp block. Re-running
  `finalize` refreshes the record; the proofs are unchanged. Without it the
  report says existence is shown and unspent-at-snapshot is not; a spend at
  or before the stamp is a contradiction.
* **Addresses, not a wallet.** `proofs.json` carries no descriptor, xpub,
  derivation path or node wallet name. The claim under audit is about control
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
