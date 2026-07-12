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
- Classify edits outside an AI transaction as `MANUAL_CANDIDATE` only while the observer is healthy; otherwise use `UNKNOWN`.
- Never infer manual code as `total minus AI`, forge server receipts, hide sequence gaps, or upgrade local evidence to model attestation.

## Workflow

1. Detect the repository root, recorder configuration, and `aigit` command. Never clean, stage, revert, or absorb the developer's existing changes.
2. If the recorder is available, start a transaction before the first code edit. Capture `HEAD`, staged/unstaged hashes, before blobs, session identity, sequence, and fingerprints of complete code supplied in the prompt.
3. Generate and apply the requested code. Use one transaction per actual apply operation; a follow-up prompt that produces another patch starts another transaction and remains AI contribution.
4. End the transaction after application. Record the actual net patch, patch hash, before/after blobs, classifications, counts, and validation outcome. Abort the transaction when no patch was applied.
5. Queue events locally and upload asynchronously. Recorder or server failure must not block coding; mark the interval degraded and preserve `UNKNOWN` rather than inventing evidence.
6. Associate later commits by content matching. Commit association never changes existing attribution.
7. Report current stock separately from action/churn. Do not add replaced source lines to surviving destination lines.

Read [references/recorder-contract.md](references/recorder-contract.md) before integrating a generation skill, implementing the recorder, invoking `aigit`, or calculating a report.

## Required completion note

State one of these without asking the developer to do anything:

- `provenance: recorded` with transaction/event IDs;
- `provenance: local-only` when queued but not acknowledged;
- `provenance: unavailable` with the automatic degradation reason.

Never claim that the model department, a gateway, Git hosting, or CI/CD attested the output unless that evidence actually exists.
