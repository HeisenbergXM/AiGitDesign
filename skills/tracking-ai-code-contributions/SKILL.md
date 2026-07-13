---
name: tracking-ai-code-contributions
description: Use when code is generated, modified, moved, formatted, or committed in a repository that measures AI versus manual contribution, or when contribution attribution is calculated from local recorder evidence.
---

# Tracking AI Code Contributions

## Purpose

Capture AI contribution automatically at the moment a patch is applied. Preserve evidence quality without asking the developer to label commits, fill forms, or change Git/CI workflows.

## Non-negotiable attribution rules

- Attribute content from evidence, never from commit messages, author identity, or who ran `git commit`.
- Count only the net patch actually applied inside the current AI transaction. Exclude all pre-existing dirty diff.
- Count a natural-language-prompted AI fix as `AI_SKILL`, even when the developer found the bug or directed the fix.
- Count code the AI creates at a new location by copying or imitating repository code as `AI_REUSED`, even when identical.
- For a true move or format-only edit, record AI as the actor but retain the content's prior origin. If the source remains, treat the new instance as `AI_REUSED`.
- Classify complete code or patch supplied directly in the prompt as `USER_SUPPLIED` to the extent it overlaps the applied patch.
- A transaction boundary alone is not evidence that a changed line came from the generation skill. Before `begin`, the skill must automatically add strict HMAC-only proposed hunks binding action, HMACed path/old-path, 0-based half-open old/new coordinates, and HMACed old/new lines. Proposed hunks and observed spans are consumed globally one-to-one. Only a complete bound match may continue through normal `USER_SUPPLIED`/`AI_REUSED`/`AI_SKILL` classification; missing, mismatched, duplicated, or reused evidence is `UNKNOWN`.
- Classify edits outside an AI transaction as `MANUAL_CANDIDATE` only while the observer is healthy; otherwise use `UNKNOWN`.
- Never infer manual code as `total minus AI`, forge server receipts, hide sequence gaps, or upgrade local evidence to model attestation.

## Workflow

1. Detect the repository root, recorder configuration, and `aigit` command. Never clean, stage, revert, or absorb the developer's existing changes.
2. Generate the proposed patch in memory without changing the repository. Automatically build one temporary HMAC-only evidence file containing prompt-code fingerprints/counts and one strict entry per proposed hunk. HMAC paths and old/new lines with their domain prefixes; retain action and coordinates only. Never put raw paths, prompt text, or code in that file, and never ask the developer to create or confirm it.
3. If the recorder is available, call `begin` with the existing `--prompt-evidence` flag before the first code edit. Capture `HEAD`, staged/unstaged hashes, before blobs, session identity, sequence, and both evidence groups; the recorder deletes the temporary file on every success or failure path.
4. Apply exactly the evidenced proposed patch. Use one transaction per actual apply operation; a follow-up prompt that produces another patch creates fresh applied-patch evidence and starts another transaction.
5. End the transaction after application. Record the actual net patch, patch hash, before/after blobs, classifications, counts, actions, and validation outcome. Pure additions may split only at bound coordinates; replacements, formatting, moves, and deletions require one complete unique match. Prompt overlap remains first after authorization; repository reuse and ordinary AI classification follow. Exact moves/deletions record `edit_actor=AI` without adding stock. Everything unmatched is `UNKNOWN`. Abort when no patch was applied.
6. Queue events locally and upload asynchronously. Before risky capture/diff work, the recorder persists a deterministic `UNKNOWN` recovery fallback with the terminal claim. Recorder, degradation-plan, or server failure must not block coding or let a later retry absorb more edits.
7. Associate later commits by content matching. Commit association never changes existing attribution.
8. Report current stock separately from action/churn. Do not add replaced source lines to surviving destination lines.

Read [references/recorder-contract.md](references/recorder-contract.md) before integrating a generation skill, implementing the recorder, invoking `aigit`, or calculating a report.

## Required completion note

State one of these without asking the developer to do anything:

- `provenance: recorded` with transaction/event IDs;
- `provenance: local-only` when queued but not acknowledged;
- `provenance: unavailable` with the automatic degradation reason.

Never claim that the model department, a gateway, Git hosting, or CI/CD attested the output unless that evidence actually exists.
