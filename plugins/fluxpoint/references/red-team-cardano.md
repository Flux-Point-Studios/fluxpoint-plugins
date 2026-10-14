# Cardano attack surfaces

Read for Aiken/Plutus validators, transaction builders, hosted services and their deployment
parameters. Map each applicable class to the actual redeemers and invariants;
record why an omitted class cannot reach this protocol. A transaction builder
is not a security boundary when an attacker can construct the transaction.

| Class | Attack |
|---|---|
| Double satisfaction | Reuse one payout for multiple script inputs, owners or fees; batch unlike actions. |
| Value and fees | Underpay, exploit floor/ceiling direction, zero/negative quantities, unit errors or partial-fill accounting. |
| Mint integrity | Wrong asset names/policies, extra mint, missing burn, net-zero mint tricks or escaped state tokens. |
| Authority | Missing signer, owner/operator confusion, key/script credential confusion or permissionless stake-script authorization. |
| Index binding | Reorder inputs/outputs; target a different entry or out-of-range index; validate what the selected entry represents. |
| Continuation | Change destination, immutable datum fields, value, asset identity or state-token cardinality. |
| Economic terms | Change price or terms while preserving owner/pair/value, then exploit another permitted action. |
| Receipt integrity | Forge, duplicate or replay a receipt; mismatch claimed and enforced amounts or attributes. |
| Reference scripts | Substitute script/policy references, exploit input/reference disjointness or parameter/hash mismatch. |
| Composed transactions | Mix redeemers, collide indices, share payouts or shift losses between positions. |
| Direction and reserves | Reverse a trade, exploit asymmetric pricing/rounding, drain one reserve or bypass a minimum. |
| Datum decoding | Wrong types/version, inline/hash confusion, malformed continuation and fail-open handling. |
| Time | Open bounds, inclusive/exclusive edges, expired actions, partial fills and replay across windows. |
| Oracle and premium | Wrong/stale feed/network, replay, signature/rotation gaps, wrong vault or rounded-away payment. |
| Liveness and cost | Dust/token bundles, datum growth, contention, minimum ADA, execution budgets and recovery from partial failure. |

For each PoC, preserve the real spent UTxO and datum constraints; enumerate
the transaction components the attacker can actually choose. A synthetic
datum or impossible ledger state cannot establish a production exploit.
Use valid controls and identify the actual rejecting condition. Check script
hashes, parameters, network and supported deployment credentials separately
from permissionless protocol attacks. Test restrictions used to exclude a
configuration. Multi-transaction and cross-redeemer composition remain a
separate pass even after all individual classes have results.

Transaction builders also need phase-1 acceptance checks, independently of
validator logic. The TypeScript taxonomy includes these named obligations:

| Class | Builder check |
|---|---|
| `attack_fee_below_ledger_minimum` | Recompute minimum fees from the final encoded transaction, integrity hash, witnesses, execution units and every relevant reference script with ledger tiering. Validate the calculation to the lovelace against landed transaction data such as Koios `tx_info`. |
| `attack_script_purpose_without_witness` | Account for each spend, mint, withdrawal and certificate script in the submitted witness set or named reference inputs; include zero withdrawals and observer scripts. |
| `attack_missing_collateral` | Require eligible, sufficient collateral for every Plutus purpose, including hosted calls without a settler, and valid returns for excess collateral. |
| `attack_batch_shares_inputs` | Prove independent bodies spend disjoint inputs or chain predecessor change in dependency order. Submit the batch to a mempool fake that rejects a double spend. |

Phase-2 evaluation (`aiken tx simulate`, Ogmios `evaluateTransaction`, Blockfrost
`/utils/txs/evaluate`) establishes script execution only; phase-1 ledger acceptance
needs its own check for fees, witnesses, collateral and input availability.

Python provider-facing services have a third surface. The Python taxonomy names
`attack_outsider_row_decode`, `attack_whole_read_fail_closed`,
`attack_unbounded_literal`, `attack_read_amplification`, `attack_event_loop_block`
and `attack_shared_pool_exhaustion`. Exercise Byron outputs, absent/null script
bytes, deep timelocks, CBOR depth and oversized JSON literals among honest rows.
State per-item and process-wide bounds, test each limit and one past it, and
flood each route while unrelated routes use the event loop, threadpool, cache
and sqlite writer. A per-request cap alone does not bound sustained provider
traffic; moving blocking work to a shared pool alone does not isolate routes.

Method and taxonomy adapted from Flux Point Studios'
[aiken-validator-redteam](https://github.com/Flux-Point-Studios/cardano-agent-skills/blob/c4dc5de6/skills/aiken-validator-redteam/SKILL.md)
and [audit framework](https://github.com/Flux-Point-Studios/cardano-agent-skills/blob/c4dc5de6/skills/aiken-dex-security-audit/references/audit-framework.md).
The source's [MIT notice](cardano-agent-skills-LICENSE.txt) is retained.
This adaptation does not use that workflow's verdict reducer. Missing agents,
unchecked cases and unresolved reproduction block; build failures and generic
crashes do not establish that an attack was defended.
