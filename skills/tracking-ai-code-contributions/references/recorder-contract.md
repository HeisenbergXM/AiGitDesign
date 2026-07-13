# Recorder Contract

## Contents

- Scope and trust boundary
- Generation-skill lifecycle
- CLI contract
- Event envelope
- Classification precedence
- Counting contract
- Failure and recovery
- Acceptance examples

## Scope and trust boundary

The recorder is a lightweight client embedded in or invoked by the code-generation skill. It snapshots the local Git worktree, delimits AI apply transactions, appends a hash-chained local ledger, and optionally uploads events to one ordinary server application.

It does not intercept model tokens and does not require the model department, Git hosting, or CI/CD to cooperate. A server receipt proves only that the server received a local claim at a given time; it does not prove what the model emitted.

The recorder is designed to raise the cost of common attribution fraud and expose evidence gaps. It does not claim perfect authorship detection.

## Generation-skill lifecycle

The generation skill must perform these operations automatically:

1. Run `aigit status --json` once per task and retain the health result.
2. Generate the proposed patch in memory without editing the repository. Automatically write HMAC-only prompt-code evidence plus one strict proposed-hunk entry binding action, HMACed path/old-path, 0-based half-open old/new coordinates, and HMACed old/new lines. This requires no developer action and no Git, CI/CD, hosting, gateway, or model-platform cooperation.
3. Before the first code edit, run `aigit begin` with the existing `--prompt-evidence` flag and retain `transaction_id`.
4. Apply only the evidenced patch produced for the current invocation. Existing dirty changes stay outside the transaction. Use one transaction and a fresh evidence file per actual apply operation, including a follow-up fix.
5. After each successful apply, run `aigit end` with the transaction ID and validation status.
6. When the model produced no applied change, run `aigit abort`.
7. Do not wait for remote upload. The local queue and uploader own retries.
8. When a commit becomes available, run `aigit link-commit`; do not rewrite attribution from commit metadata.

If `aigit` or its configuration is absent, continue the coding task and report `provenance: unavailable`. Never synthesize an event from a commit message.

## CLI contract

The implementation plan defines a Python console application named `aigit`. Its stable public commands are:

```text
aigit status --repo <path> --json
aigit begin --repo <path> --session <id> --prompt-evidence <json-file> --json
aigit end --repo <path> --transaction <id> --validation <passed|failed|not-run> --json
aigit abort --repo <path> --transaction <id> --reason <text> --json
aigit link-commit --repo <path> --commit <sha> --json
aigit upload --repo <path> --once --json
aigit report --repo <path> --rev <git-revision> --json
```

`--prompt-evidence` keeps its existing name and contains prompt-supplied code evidence plus strict proposed-hunk evidence generated automatically from the exact patch. It contains no raw path, prompt text, or code. Every real separate or equals-form evidence path is deleted after `begin` succeeds, fails, or is rejected during parsing; a separate option followed by another option has no path and must not unlink an option-named file.

The JSON object contains exactly these fields. Each fingerprint is a lowercase 64-hex HMAC-SHA256 value; each counts array must match its nested line-fingerprint blocks and normalized total. Empty evidence uses empty arrays and zero totals.

```json
{
  "fingerprints": [],
  "counts": [],
  "line_fingerprints": [],
  "normalized_line_count": 0,
  "normalized_token_count": 0,
  "proposed_patch_hunks": [
    {
      "action": "ADDED",
      "path_hmac": "64-lowercase-hex",
      "old_path_hmac": null,
      "old_start": 1,
      "old_end": 1,
      "new_start": 1,
      "new_end": 2,
      "old_line_fingerprints": [],
      "new_line_fingerprints": ["64-lowercase-hex"]
    }
  ]
}
```

Path fingerprints are `HMAC-SHA256(key, "path\\0" + normalized-posix-path)` and line fingerprints are `HMAC-SHA256(key, "line\\0" + normalized-line)`. Hunk objects and top-level objects reject unknown fields. Coordinates are non-negative 0-based half-open ranges whose lengths exactly equal their fingerprint-list lengths. `ADDED` has no old lines, `DELETED` has no new lines, `MOVED` requires `old_path_hmac`, and `REPLACED`/`FORMATTED` require both sides.

Every command returns JSON with `ok`, `status`, and an error code when degraded. `begin` additionally returns `transaction_id`; `end` returns `event_ids` and local queue status. Missing remote acknowledgement is not a command failure.

## Event envelope

Every local event is append-only and contains at least:

```json
{
  "schema_version": 1,
  "event_id": "uuid-or-ulid",
  "event_type": "patch_applied",
  "repo_id": "stable-local-repository-id",
  "session_id": "agent-task-id",
  "transaction_id": "apply-transaction-id",
  "sequence": 12,
  "observed_at": "RFC3339 timestamp",
  "head_before": "git-object-id",
  "dirty_diff_hash_before": "sha256:...",
  "patch_hash": "sha256:...",
  "before_blob": "git-object-id-or-content-hash",
  "after_blob": "git-object-id-or-content-hash",
  "classification": "AI_SKILL",
  "normalized_lines": 35,
  "prompt_code_overlap_lines": 0,
  "previous_event_hash": "sha256:...",
  "event_hash": "sha256:..."
}
```

Canonicalize JSON before hashing. Hash-chain order is per repository and recorder identity. Raw prompts, secrets, source files, and full model output are excluded from server uploads by default.

Minimum event types are `session_started`, `transaction_started`, `patch_applied`, `transaction_aborted`, `heartbeat`, `commit_linked`, `upload_acknowledged`, and `recovery_detected`.

## Classification precedence

First gate transaction attribution by proposed-hunk evidence. Consume evidence hunks and observed spans globally and deterministically one-to-one. A pure added span may split losslessly only at bound coordinates/fingerprints. `REPLACED`, `FORMATTED`, `MOVED`, and `DELETED` require one complete unique action/path/range/old/new match; otherwise the whole span is `UNKNOWN`. Missing evidence never defaults to AI merely because the edit occurred inside a transaction.

Apply rules in this order to each applied-evidence-matched span:

1. Direct overlap with complete prompt code or patch: `USER_SUPPLIED`.
2. Exact move with source removed, or format-only edit: retain prior content origin; record `edit_actor=AI`.
3. Patch actually applied inside the AI transaction: `AI_SKILL`.
4. New-location exact/near copy or structural imitation of pre-transaction repository code: refine to `AI_REUSED`.
5. Observer-healthy edit outside all AI transactions: `MANUAL_CANDIDATE`.
6. Observer gap, ambiguous concurrent write, failed matching, or missing evidence: `UNKNOWN`.
7. Content present before recorder adoption: `LEGACY_UNKNOWN`.

`AI_DERIVED` may be used for later surviving code whose AI origin remains traceable after mixed edits. It remains within the AI stock while confidence and derivation are reported separately.

For the first implementation, classify an exact normalized copy only for blocks of at least 3 non-empty lines. Classify a near copy when a block has at least 5 non-empty lines and normalized token similarity is at least 0.85. Lower-confidence structural resemblance stays `AI_SKILL`; this changes only the reuse subcategory, not total AI contribution.

## Counting contract

Use normalized non-empty physical code lines for the primary metric. Exclude configured generated files, vendored dependencies, lockfiles, minified outputs, and pure whitespace/comment-only churn when the repository policy says so.

For current stock:

```text
A = AI_SKILL + AI_REUSED + surviving AI_DERIVED
M = MANUAL_CANDIDATE
S = USER_SUPPLIED
U = UNKNOWN + LEGACY_UNKNOWN
N = A + M + S + U
AI stock ratio = A / N
manual-candidate stock ratio = M / N
coverage = (A + M + S) / N
```

For action metrics, report added, replaced, moved, formatted, and deleted spans separately. A replacement is an action against old content plus surviving new content; do not sum both as current stock. Exact moves, formatting, and deletions record `edit_actor=AI` but add no new stock. A copy does create a new instance.

## Failure and recovery

- Recorder unavailable: fail open, make no attribution claim, report `provenance: unavailable`.
- Begin append/queue half-failure: retain the deterministic start plan; a same-session retry repairs and returns the same transaction/event, while another session receives `ACTIVE_TRANSACTION`.
- End claim atomically persists a deterministic generic `UNKNOWN` recovery fallback, a random durable claim token, a monotonically increasing claim generation, and a 5-second UTC lease before capture/diff. While that bounded lease is live, same-operation racers return `TERMINAL_OPERATION_IN_PROGRESS` and cannot execute the fallback ahead of the fresh owner. After expiry, a same-operation retry atomically rotates the token, increments the generation, renews the lease, and executes the same persisted fallback; no developer action or sleep is required. Exact or degraded fallback replacement uses token-plus-generation compare-and-set, so a stale owner converges to the current persisted/completed plan and cannot append exact events after takeover. Capture/diff failure, or failure while storing a more specific degradation plan, executes the persisted fallback; retries return that same result and later edits cannot be absorbed.
- Server unavailable: append locally, report `provenance: local-only`, retry after 5, 15, 60, and 300 seconds, then every 300 seconds with jitter, using idempotent event IDs.
- Heartbeat or sequence gap: emit `recovery_detected`; affected content is `UNKNOWN` unless stronger pre-existing evidence resolves it.
- Duplicate upload: server returns the existing acknowledgement without duplicating the event.
- Out-of-order upload: accept idempotently, flag the gap, and keep report coverage degraded until resolved.
- Local ledger damage: preserve the file for audit, start a new chain with an explicit recovery event, and never silently rewrite history.
- Concurrent external edit during a transaction: split only when the patch can be matched exactly; otherwise classify the ambiguous span as `UNKNOWN`.

The observer emits a heartbeat every 10 seconds. Treat it as healthy when the last heartbeat is no older than 30 seconds; a longer interval is an explicit coverage gap. These defaults are centrally configurable, never developer-entered.

Developers must not be asked to add labels, special commit messages, manual forms, or correction steps. Operational alerts go to the system owner.

## Acceptance examples

| Situation | Required result |
|---|---|
| 220 manual dirty lines exist; AI applies an 18-line fix and 35 test lines | Only the net applied AI patch is AI; pre-existing dirty content is excluded |
| Developer finds a bug and prompts AI to fix it | Applied fix is `AI_SKILL` |
| AI imitates module A to create module B, including identical lines | New B instances are `AI_REUSED` |
| AI moves a function and formats it | Origin is retained; AI action is recorded; no new stock |
| Source function remains after a claimed move | New instance is a copy and therefore `AI_REUSED` |
| Prompt contains a complete 60-line patch and AI applies it verbatim | Overlap is `USER_SUPPLIED` |
| AI commits the entire dirty worktree | Commit links mixed evidence; it does not convert dirty content to AI |
| Server is offline for 45 minutes | Coding continues, events queue locally, no receipt is forged |
