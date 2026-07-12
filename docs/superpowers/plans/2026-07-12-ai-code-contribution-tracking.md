# AI Code Contribution Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现第 1 档本地证据账本和第 2 档单机日志应用，自动统计 AI、人工候选、用户直接提供和未知代码的存量及动作量，并覆盖已定义的常见作弊路径。

**Architecture:** 生成代码 skill 在每次实际 apply 前后调用本地 `aigit` 客户端；客户端用内容快照隔离调用前脏改动，把事件写入哈希链账本并异步上传。常驻轻量观察器记录事务外编辑与心跳；单机 FastAPI 应用用 SQLite 接收事件、关联 Git 内容并生成报告。整个链路 fail-open，不依赖模型网关、Git 平台或 CI/CD。

**Tech Stack:** Python 3.12、标准库 `sqlite3/subprocess/hashlib/difflib`、FastAPI 0.115、Uvicorn 0.34、HTTPX 0.28、pytest 8.3、Docker Compose v2、SQLite 3；PostgreSQL 只保留存储接口，不在本计划实现。

## Global Constraints

- 只实施 README 第 1 档和第 2 档；不建设模型网关，不截取 token，不要求模型部门、Git 平台或 CI/CD 配合。
- 不要求开发者加标签、写特殊提交信息、填表、确认归因或手工补传。
- 只把一次 AI transaction 内实际应用的净 patch 计入 AI；调用前 staged、unstaged 和 untracked 内容必须排除。
- 开发者通过自然语言 Prompt 让 AI 修复逻辑或 bug，实际应用部分计 `AI_SKILL`。
- AI 在新位置复制或仿写仓库代码计 `AI_REUSED`；真正移动和纯格式化只记 AI 动作，保持原内容来源。
- Prompt 直接包含完整代码或 patch 时，重合部分计 `USER_SUPPLIED`。
- 观察器健康期间、AI transaction 外的修改才可计 `MANUAL_CANDIDATE`；空窗和不可分离并发修改计 `UNKNOWN`。
- 本地或服务器故障不得阻塞编码；禁止伪造回执、按提交信息补来源或用“总量减 AI”补人工。
- 服务端默认只接收哈希、计数、分类、时间和关联 ID，不上传原始 Prompt、源文件、完整 patch 或模型输出。
- 所谓“90% 效果”验收为已列举作弊场景覆盖率不低于 90%，不是逐行识别准确率。
- 一次实际 apply 对应一个 transaction；观察器每 10 秒心跳，最近心跳不超过 30 秒才视为健康。
- 精确复用块至少 3 个规范化非空行；近似复用块至少 5 行且 token 相似度不低于 0.85。

---

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Python 版本、依赖、`aigit`/`aigit-server` 入口和 pytest 配置 |
| `src/aigit/domain.py` | 事件、分类、动作和状态的稳定类型契约 |
| `src/aigit/canonical.py` | JSON 规范化、内容哈希和事件哈希 |
| `src/aigit/local_store.py` | 本地 SQLite 状态、内容寻址 blob 和 JSONL 哈希链 |
| `src/aigit/git_state.py` | Git 根目录发现、tracked/untracked 快照和 commit 内容读取 |
| `src/aigit/classifier.py` | Prompt 重合、移动、复用和未知 span 分类 |
| `src/aigit/recorder.py` | begin/end/abort transaction 与净 patch 生成 |
| `src/aigit/observer.py` | 事务外编辑检测、心跳和自动恢复事件 |
| `src/aigit/uploader.py` | fail-open 本地队列、幂等批量上传与退避 |
| `src/aigit/cli.py` | `aigit` 稳定命令接口和 JSON 输出 |
| `src/aigit_server/store.py` | 服务端 SQLite 仓储及未来 PostgreSQL 接口边界 |
| `src/aigit_server/app.py` | 事件、心跳、ref snapshot、报告和健康 API |
| `src/aigit/matcher.py` | 事件 patch 与 Git revision 内容关联，本地和服务端共用 |
| `src/aigit/reporting.py` | 存量、窗口存活、动作、覆盖率和异常指标，本地和服务端共用 |
| `src/aigit_server/scheduler.py` | 定时生成报表快照并清理过期接收状态 |
| `tests/unit/` | 纯函数和边界单元测试 |
| `tests/integration/` | 临时 Git 仓库、客户端、服务端和离线恢复测试 |
| `tests/golden/` | 作弊场景矩阵与 90% 覆盖验收 |
| `deploy/Dockerfile` | 单容器服务镜像 |
| `compose.yaml` | SQLite 数据卷、令牌和健康检查 |

### Task 1: Freeze Domain and Package Contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/aigit/__init__.py`
- Create: `src/aigit/domain.py`
- Test: `tests/unit/test_domain.py`

**Interfaces:**
- Consumes: README 分类和事件口径。
- Produces: `Classification`, `ActionKind`, `ProvenanceStatus`, `Event`, `GitSnapshot`, `PatchSpan`；`PatchSpan.added(path, lines)` 和 `PatchSpan.relocated(old_path, new_path, lines)` 是测试与分类器共用构造器；后续任务只能扩展 payload，不能改枚举值。

- [ ] **Step 1: Write the failing contract test**

```python
from aigit.domain import Classification, Event, ProvenanceStatus


def test_public_classification_values_are_stable() -> None:
    assert {item.value for item in Classification} == {
        "AI_SKILL", "AI_REUSED", "AI_DERIVED", "MANUAL_CANDIDATE",
        "USER_SUPPLIED", "UNKNOWN", "LEGACY_UNKNOWN",
    }
    assert ProvenanceStatus.LOCAL_ONLY.value == "local-only"


def test_event_rejects_empty_identity() -> None:
    try:
        Event.new("", "session-1", "heartbeat", {})
    except ValueError as exc:
        assert str(exc) == "repo_id must not be empty"
    else:
        raise AssertionError("empty repo_id was accepted")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests/unit/test_domain.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'aigit'`.

- [ ] **Step 3: Create package metadata and domain types**

Use these exact dependency floors and entry points in `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "aigit-design"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi==0.115.*", "httpx==0.28.*", "uvicorn==0.34.*"]

[project.optional-dependencies]
test = ["pytest==8.3.*", "pytest-cov==6.0.*"]

[project.scripts]
aigit = "aigit.cli:main"
aigit-server = "aigit_server.app:main"

[tool.pytest.ini_options]
addopts = "-ra --strict-markers"
testpaths = ["tests"]
```

Implement `domain.py` with immutable dataclasses. `Event.new()` must validate non-empty IDs, create a UUID event ID, set RFC3339 UTC time, initialize `previous_event_hash` and `event_hash` to empty strings, and leave hashing to Task 2. Define `GitSnapshot` as `head`, `index_hash`, `worktree_hash`, and `files: dict[str, str]`; define `PatchSpan` as path, old/new line ranges, normalized lines, classification, action and confidence.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/unit/test_domain.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/aigit tests/unit/test_domain.py
git commit -m "feat: freeze contribution event contracts"
```

### Task 2: Build the Append-only Local Evidence Ledger

**Files:**
- Create: `src/aigit/canonical.py`
- Create: `src/aigit/local_store.py`
- Test: `tests/unit/test_local_store.py`

**Interfaces:**
- Consumes: `Event` from Task 1.
- Produces: `canonical_json(value) -> bytes`, `hash_bytes(value) -> str`, `LocalStore.append(event) -> Event`, `LocalStore.verify_chain() -> list[str]`, `put_blob(data) -> str`, `get_blob(digest) -> bytes`.

- [ ] **Step 1: Write failing hash-chain tests**

```python
import json
from aigit.domain import Event
from aigit.local_store import LocalStore


def test_append_builds_a_verifiable_chain(tmp_path) -> None:
    store = LocalStore(tmp_path)
    first = store.append(Event.new("repo-1", "s-1", "session_started", {}))
    second = store.append(Event.new("repo-1", "s-1", "heartbeat", {"healthy": True}))
    assert second.previous_event_hash == first.event_hash
    assert store.verify_chain() == []


def test_tampering_is_reported(tmp_path) -> None:
    store = LocalStore(tmp_path)
    store.append(Event.new("repo-1", "s-1", "heartbeat", {"healthy": True}))
    line = json.loads(store.ledger_path.read_text(encoding="utf-8").splitlines()[0])
    line["payload"]["healthy"] = False
    store.ledger_path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert store.verify_chain() == [line["event_id"]]
```

- [ ] **Step 2: Run the focused test**

Run: `python -m pytest tests/unit/test_local_store.py -q`

Expected: FAIL because `aigit.local_store` does not exist.

- [ ] **Step 3: Implement deterministic storage**

Canonical JSON uses UTF-8, sorted keys, compact separators, rejects NaN, and excludes `event_hash` while computing the hash. Store blobs under `<state>/blobs/<first-two-hex>/<remaining-hex>` using SHA-256. Append one compact JSON object per line with `flush()` and `os.fsync()` before returning. Maintain `state.sqlite3` tables `sequences`, `active_transactions`, and `upload_queue`; JSONL remains the audit source and SQLite remains a rebuildable index.

Hash input must be:

```python
def event_hash(event_dict: dict[str, object]) -> str:
    unsigned = dict(event_dict)
    unsigned["event_hash"] = ""
    return "sha256:" + hashlib.sha256(canonical_json(unsigned)).hexdigest()
```

Before append, allocate the next per-repo sequence under `BEGIN IMMEDIATE`, read the previous event hash, compute the new hash, then append. `verify_chain()` recomputes every event and returns corrupted event IDs without rewriting the ledger.

- [ ] **Step 4: Verify durability behavior**

Run: `python -m pytest tests/unit/test_local_store.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/canonical.py src/aigit/local_store.py tests/unit/test_local_store.py
git commit -m "feat: add hash chained local evidence ledger"
```

### Task 3: Capture Git-aware Worktree Snapshots Without Mutating Developer State

**Files:**
- Create: `src/aigit/git_state.py`
- Test: `tests/integration/test_git_state.py`

**Interfaces:**
- Consumes: `LocalStore.put_blob`, `GitSnapshot`.
- Produces: `SnapshotPolicy`, `DEFAULT_POLICY`, `find_repo(path) -> Path`, `repo_id(root) -> str`, `capture_snapshot(root, store, policy) -> GitSnapshot`, `diff_snapshots(before, after, store) -> list[PatchSpan]`.

- [ ] **Step 1: Write a dirty-worktree isolation test**

Create a temporary Git repository, commit `app.py`, then modify it and add untracked `test_app.py`. Capture snapshot A, change only one existing line through the simulated AI operation, capture snapshot B, and assert the computed patch contains only that second change while snapshot A's dirty content is absent.

```python
def test_snapshot_delta_excludes_preexisting_dirty_diff(repo, store) -> None:
    (repo / "app.py").write_text("manual = 1\n", encoding="utf-8")
    before = capture_snapshot(repo, store, DEFAULT_POLICY)
    (repo / "app.py").write_text("manual = 1\nai_fix = 2\n", encoding="utf-8")
    after = capture_snapshot(repo, store, DEFAULT_POLICY)
    spans = diff_snapshots(before, after, store)
    assert [span.new_lines for span in spans] == [("ai_fix = 2",)]
```

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest tests/integration/test_git_state.py::test_snapshot_delta_excludes_preexisting_dirty_diff -q`

Expected: FAIL because snapshot functions are missing.

- [ ] **Step 3: Implement read-only capture**

Use `git rev-parse --show-toplevel`, `git rev-parse HEAD`, `git diff --cached --binary`, and `git ls-files -co --exclude-standard -z`. Never invoke checkout, clean, reset, stash, add, or update-index. For each included file up to 2 MiB, store bytes in the blob store and map repository-relative POSIX path to digest. Hash the canonical file manifest as `worktree_hash`; hash the cached diff separately as `index_hash`.

Default exclusions are `.git/**`, `.aigit/**`, vendored dependency directories, lockfiles, minified files, generated outputs and binary content. Repository policy can add glob exclusions but cannot silently include secrets denied by central policy.

Compute text deltas with `difflib.SequenceMatcher(autojunk=False)` and preserve old/new ranges. Binary, oversized, unreadable or concurrently changing files produce an `UNKNOWN` span instead of being skipped.

- [ ] **Step 4: Run integration tests**

Run: `python -m pytest tests/integration/test_git_state.py -q`

Expected: all tests pass and `git status --porcelain=v1` before/after the capture command is byte-identical.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/git_state.py tests/integration/test_git_state.py
git commit -m "feat: capture non-mutating git worktree snapshots"
```

### Task 4: Classify Prompt Code, Moves, Reuse and AI-generated Spans

**Files:**
- Create: `src/aigit/classifier.py`
- Create: `src/aigit/prompt_evidence.py`
- Test: `tests/unit/test_classifier.py`

**Interfaces:**
- Consumes: before/after `PatchSpan`, pre-transaction repository blobs, prompt fingerprints.
- Produces: `RepositoryBlock`, `ClassificationContext`, `classify_spans(spans, context) -> list[PatchSpan]`, `build_prompt_evidence(code_blocks, key) -> PromptEvidence`.

- [ ] **Step 1: Write the precedence tests**

```python
from aigit.classifier import ClassificationContext, RepositoryBlock, classify_spans
from aigit.domain import Classification, PatchSpan
from aigit.prompt_evidence import build_prompt_evidence


MODULE_LINES = ("def total(items):", "    values = list(items)", "    return sum(values)")


def test_new_exact_copy_is_ai_reused() -> None:
    context = ClassificationContext(
        in_transaction=True,
        observer_healthy=True,
        prompt_evidence=build_prompt_evidence((), key=b"k" * 32),
        repository_blocks=(RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE),),
        removed_blocks=(),
    )
    result = classify_spans((PatchSpan.added("b.py", MODULE_LINES),), context)
    assert result[0].classification is Classification.AI_REUSED


def test_true_move_retains_origin() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)
    context = ClassificationContext(True, True, build_prompt_evidence((), b"k" * 32), (source,), (source,))
    result = classify_spans((PatchSpan.relocated("a.py", "moved.py", MODULE_LINES),), context)
    assert result[0].action.value == "MOVED"
    assert result[0].classification is Classification.MANUAL_CANDIDATE


def test_prompt_patch_overlap_wins() -> None:
    evidence = build_prompt_evidence((MODULE_LINES,), key=b"k" * 32)
    context = ClassificationContext(True, True, evidence, (), ())
    result = classify_spans((PatchSpan.added("b.py", MODULE_LINES),), context)
    assert result[0].classification is Classification.USER_SUPPLIED
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/unit/test_classifier.py -q`

Expected: three failures because classifier functions are missing.

- [ ] **Step 3: Implement ordered classification**

Normalize line endings, trim trailing whitespace, drop empty lines for metrics, and tokenize identifiers/operators without renaming identifiers. Apply this exact order:

1. Prompt direct-code fingerprint overlap -> `USER_SUPPLIED`.
2. Source removed plus normalized destination match -> retain origin, action `MOVED` or `FORMATTED`.
3. New destination produced inside transaction -> `AI_SKILL`.
4. Exact pre-repository match of at least 3 lines, or token similarity >= 0.85 for at least 5 lines -> refine to `AI_REUSED`.
5. Outside transaction while observer healthy -> `MANUAL_CANDIDATE`.
6. Ambiguous/concurrent/gap -> `UNKNOWN`.

An unchanged source means copy, not move. Structural similarity below 0.85 stays `AI_SKILL`, so uncertainty never reduces total AI contribution. Mixed lines split at token boundaries only when both segments are contiguous and reproducible; otherwise the line is `UNKNOWN`.

Use the same language-neutral tokenizer on both sides: normalize CRLF to LF, trim trailing whitespace, then match string literals, identifiers, decimal numbers, two-character operators and finally any non-whitespace character with `r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|==|!=|<=|>=|=>|::|&&|\|\||\S'''`. Keep token text unchanged and compute similarity with `difflib.SequenceMatcher(None, left_tokens, right_tokens, autojunk=False).ratio()`. Align candidate blocks by the largest matching block, then expand symmetrically to the shorter block length before applying the 0.85 threshold.

`RepositoryBlock` contains path, normalized lines and prior origin. `ClassificationContext` contains `in_transaction`, `observer_healthy`, `prompt_evidence`, `repository_blocks` and `removed_blocks` in that order. Prompt evidence contains normalized HMAC-SHA256 fingerprints and counts only. Use a per-installation key from the local state; never persist raw code blocks after `begin`.

- [ ] **Step 4: Verify GREEN and boundary values**

Run: `python -m pytest tests/unit/test_classifier.py -q`

Expected: exact 3-line copy and 0.85 similarity pass; 2-line copy and 0.849 similarity remain `AI_SKILL`; all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/classifier.py src/aigit/prompt_evidence.py tests/unit/test_classifier.py
git commit -m "feat: classify ai reuse moves and supplied code"
```

### Task 5: Implement Recorder Transactions and the Stable CLI

**Files:**
- Create: `src/aigit/recorder.py`
- Create: `src/aigit/cli.py`
- Test: `tests/integration/test_recorder_cli.py`

**Interfaces:**
- Consumes: snapshot, store and classifier APIs from Tasks 2-4.
- Produces: `Recorder.begin`, `Recorder.end`, `Recorder.abort`, and the exact CLI in `skills/tracking-ai-code-contributions/references/recorder-contract.md`.

- [ ] **Step 1: Write a transaction boundary test**

```python
import json
import subprocess
import sys


def invoke(*args: object) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "aigit.cli", *(str(arg) for arg in args)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_cli_records_only_net_applied_patch(repo) -> None:
    (repo / "dirty.py").write_text("manual = 1\n", encoding="utf-8")
    begun = invoke("begin", "--repo", repo, "--session", "s-1")
    (repo / "dirty.py").write_text("manual = 1\nai = 2\n", encoding="utf-8")
    ended = invoke("end", "--repo", repo, "--transaction", begun["transaction_id"], "--validation", "passed")
    assert ended["status"] in {"recorded", "local-only"}
    assert ended["counts"] == {"AI_SKILL": 1}
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/integration/test_recorder_cli.py -q`

Expected: FAIL because `aigit.cli` is absent.

- [ ] **Step 3: Implement begin/end/abort**

`begin` acquires a per-repo lock for at most 250 ms, captures the before snapshot, creates `transaction_started`, persists the active transaction, deletes the prompt evidence file in `finally`, and returns immediately. A second active transaction for the same repo returns JSON error `ACTIVE_TRANSACTION` without touching Git.

`end` captures after snapshot, computes only snapshot delta, classifies spans, appends one `patch_applied` event per file plus `transaction_finished`, enqueues them, deletes the active transaction and returns counts. If capture or classification cannot isolate a span, append `UNKNOWN` rather than absorbing all current diff.

`abort` appends `transaction_aborted` and clears the transaction without contribution. All commands print one JSON object and use exit code 0 for recorded/local-only/unavailable fail-open states; use non-zero only for invalid arguments or state corruption.

Implement `status`, `begin`, `end`, `abort`, `link-commit`, `upload`, and `report` subcommands exactly as documented in the recorder contract.

- [ ] **Step 4: Verify CLI and no-recorder degradation**

Run: `python -m pytest tests/integration/test_recorder_cli.py -q`

Expected: tests pass; pre-existing dirty content never appears in the AI counts; missing server returns `local-only` without delaying longer than 500 ms.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/recorder.py src/aigit/cli.py tests/integration/test_recorder_cli.py
git commit -m "feat: add fail-open ai apply transactions"
```

### Task 6: Add the Zero-burden Observer and Heartbeat Coverage

**Files:**
- Create: `src/aigit/observer.py`
- Create: `src/aigit/process.py`
- Test: `tests/integration/test_observer.py`

**Interfaces:**
- Consumes: `capture_snapshot`, active transaction state, local event store.
- Produces: `Observer.tick(now)`, `ensure_observer(root)`, `heartbeat` and `recovery_detected` events.

- [ ] **Step 1: Write observer health tests**

Test that a transaction-external edit after a healthy heartbeat becomes `MANUAL_CANDIDATE`, an edit after 31 seconds without heartbeat becomes `UNKNOWN`, and an edit during an active transaction is left for `Recorder.end`.

```python
def test_gap_never_becomes_manual_candidate(observer, clock, repo) -> None:
    observer.tick(clock.now())
    clock.advance(seconds=31)
    (repo / "app.py").write_text("changed = True\n", encoding="utf-8")
    event = observer.tick(clock.now())[-1]
    assert event.event_type == "recovery_detected"
    assert event.payload["classification"] == "UNKNOWN"
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/integration/test_observer.py -q`

Expected: FAIL because observer module is missing.

- [ ] **Step 3: Implement observer lifecycle**

Poll every 10 seconds. Persist last heartbeat and snapshot so process restart exposes the gap. If no transaction is active, classify an exactly isolated delta as `MANUAL_CANDIDATE`; if timing, file read or concurrent state is ambiguous, emit `UNKNOWN`. Never infer source from OS username or editor process name.

`ensure_observer()` starts one detached process per repo only when the PID file is absent or stale. On Windows use `CREATE_NO_WINDOW | DETACHED_PROCESS`; on POSIX use `start_new_session=True`. The coding agent never waits for it. Repeated calls are idempotent.

Observer startup, crash and recovery append events; operational alerts go to configured owners, not developers. A gap affects exactly the content delta from the last healthy persisted snapshot through the first recovery snapshot. Any span in that delta not already covered by a complete AI transaction is `UNKNOWN`; the first post-recovery heartbeat establishes the next healthy boundary.

- [ ] **Step 4: Verify timing and process idempotency**

Run: `python -m pytest tests/integration/test_observer.py -q`

Expected: all tests pass under a fake clock without real sleeps.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/observer.py src/aigit/process.py tests/integration/test_observer.py
git commit -m "feat: observe transaction external edits and gaps"
```

### Task 7: Implement Idempotent Upload and the Single-server Ingest API

**Files:**
- Create: `src/aigit/uploader.py`
- Create: `src/aigit_server/__init__.py`
- Create: `src/aigit_server/store.py`
- Create: `src/aigit_server/app.py`
- Test: `tests/integration/test_ingest_api.py`

**Interfaces:**
- Consumes: queued event JSON from LocalStore.
- Produces: `POST /api/v1/events/batch`, `POST /api/v1/heartbeats`, `POST /api/v1/ref-snapshots`, `GET /health`; `EventStore` protocol and `SQLiteEventStore`.

- [ ] **Step 1: Write ingest tests**

```python
import hashlib
from fastapi.testclient import TestClient
from aigit_server.app import create_app
from aigit_server.store import SQLiteEventStore


def test_duplicate_event_is_idempotent(tmp_path, valid_event_batch) -> None:
    token = "test-token"
    app = create_app(SQLiteEventStore(tmp_path / "server.sqlite3"), hashlib.sha256(token.encode()).hexdigest())
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    first = client.post("/api/v1/events/batch", json=valid_event_batch, headers=headers)
    second = client.post("/api/v1/events/batch", json=valid_event_batch, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["accepted"] == 1
    assert second.json()["duplicates"] == 1


def test_sequence_gap_is_accepted_and_flagged(tmp_path, sequence_gap_batch) -> None:
    token = "test-token"
    app = create_app(SQLiteEventStore(tmp_path / "server.sqlite3"), hashlib.sha256(token.encode()).hexdigest())
    response = TestClient(app).post(
        "/api/v1/events/batch",
        json=sequence_gap_batch,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["anomalies"] == ["sequence_gap:2-4"]
```

Define `valid_event_batch` and `sequence_gap_batch` as complete pytest fixtures in the same test module using `Event.new()` plus Task 2 hashing; the only sequence values are `[1]` and `[1, 5]` respectively.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/integration/test_ingest_api.py -q`

Expected: FAIL because server app is missing.

- [ ] **Step 3: Implement storage and HTTP validation**

Define `EventStore` methods `append_events`, `append_heartbeat`, `append_ref_snapshot`, `events_for_repo`, and `anomalies_for_repo`. SQLite tables are `repositories`, `events`, `heartbeats`, `ref_snapshots`, and `receipts`; `events.event_id` is unique, and `(repo_id, server_sequence)` is indexed.

Validate a bearer token whose SHA-256 digest is configured server-side, cap request bodies at 1 MiB and batches at 500 events, validate schema version and event hash, stamp `received_at` and monotonic `server_sequence`, and return per-event acknowledgement. Accept out-of-order events but flag duplicate, gap, late upload and broken local chain. Never treat server receipt as model attestation.

Uploader sends at most 500 events, uses stable IDs, deletes queue rows only after acknowledgement, and retries after 5/15/60/300 seconds then every 300 seconds with +/-20% jitter. Network and 5xx errors return `local-only`; 4xx schema errors remain queued and create an operational alert.

- [ ] **Step 4: Verify API and offline retry**

Run: `python -m pytest tests/integration/test_ingest_api.py -q`

Expected: all tests pass; duplicate storage count remains one; server downtime never changes the local event hash.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/uploader.py src/aigit_server tests/integration/test_ingest_api.py
git commit -m "feat: ingest contribution evidence idempotently"
```

### Task 8: Match Commits and Produce Stock, Survival and Action Reports

**Files:**
- Create: `src/aigit/matcher.py`
- Create: `src/aigit/reporting.py`
- Create: `src/aigit_server/scheduler.py`
- Modify: `src/aigit_server/store.py`
- Modify: `src/aigit_server/app.py`
- Test: `tests/integration/test_reporting.py`

**Interfaces:**
- Consumes: server events, ref snapshots and read-only local mirror/repository path configured for the server.
- Produces: shared `ContributionCounts`, `ActionCounts`, `build_report(counts, actions)`, local `aigit report`, scheduled report snapshots, `GET /api/v1/reports/{repoId}` and report DTO with `stock`, `window_survival`, `actions`, `reuse`, `coverage`, `health`, and `anomalies`.

- [ ] **Step 1: Write report invariant tests**

```python
from aigit.reporting import ActionCounts, ContributionCounts, build_report


def test_stock_denominator_keeps_unknown() -> None:
    report = build_report(
        ContributionCounts(ai=40, manual_candidate=30, user_supplied=10, unknown=20),
        ActionCounts(),
    )
    assert report.stock.ai_ratio == 0.40
    assert report.stock.manual_candidate_ratio == 0.30
    assert report.coverage == 0.80


def test_move_is_action_not_new_stock() -> None:
    before = ContributionCounts(ai=10, manual_candidate=12, user_supplied=0, unknown=0)
    report = build_report(before, ActionCounts(moved_lines=12), before_stock=before)
    assert report.actions.moved_lines == 12
    assert report.stock.total_lines == report.before_stock.total_lines
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/integration/test_reporting.py -q`

Expected: FAIL because reporting module is missing.

- [ ] **Step 3: Implement matching and metrics**

Match in order: exact after-blob, exact normalized span, patch context, then token similarity. Record match method and confidence; do not force ambiguous candidates. A commit link narrows search but does not alter origin. Ref snapshots determine “first entered target branch” and period-end survival.

Compute these exact stock variables:

```text
A = AI_SKILL + AI_REUSED + surviving AI_DERIVED
M = MANUAL_CANDIDATE
S = USER_SUPPLIED
U = UNKNOWN + LEGACY_UNKNOWN
N = A + M + S + U
ai_ratio = A / N
manual_candidate_ratio = M / N
coverage = (A + M + S) / N
```

Return null ratios with `empty_scope=true` when `N=0`. Separately report added/replaced/moved/formatted/deleted actions, AI effective deletion, reuse rate, clone-adjusted logical contribution, 7/30/90-day retention, observer uptime, sequence gaps, late uploads and acknowledgement rate.

The same reporting module reads a local ledger for `aigit report` and reads an `EventStore` for the server. The scheduler materializes one report snapshot per active repository every 15 minutes and once at UTC day end. `GET /api/v1/reports/{repoId}` returns the latest snapshot by default and recomputes only when `refresh=true` and the caller is authorized. Add `report_snapshots(repo_id, calculated_at, revision, report_json)` to SQLite.

- [ ] **Step 4: Verify report invariants**

Run: `python -m pytest tests/integration/test_reporting.py -q`

Expected: all tests pass; `A+M+S+U=N`; unknown remains visible; moves do not inflate stock.

- [ ] **Step 5: Commit**

```bash
git add src/aigit/matcher.py src/aigit/reporting.py src/aigit_server/scheduler.py src/aigit_server/store.py src/aigit_server/app.py tests/integration/test_reporting.py
git commit -m "feat: report surviving ai and manual candidate stock"
```

### Task 9: Package the Lightweight Server for One-machine Deployment

**Files:**
- Create: `deploy/Dockerfile`
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `docs/operations.md`
- Create: `src/aigit_server/backup.py`
- Test: `tests/integration/test_server_security.py`

**Interfaces:**
- Consumes: `aigit-server` entry point and SQLite store.
- Produces: one container, one persistent volume, `/health` health check, bearer-token rotation procedure and backup/restore commands.

- [ ] **Step 1: Write security tests**

Test missing/wrong token -> 401, oversized body -> 413, unsupported schema -> 422, raw `prompt`/`source`/`model_output` keys -> 422, and valid hashes/counts -> 200.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/integration/test_server_security.py -q`

Expected: at least one policy assertion fails before hardening.

- [ ] **Step 3: Add deployment artifacts**

Build from `python:3.12-slim`, install the wheel as a non-root user, expose 8080, store `/data/aigit.sqlite3`, and health-check `GET /health`. Compose mounts only `aigit-data`, sets `AIGIT_TOKEN_SHA256`, `AIGIT_DB=/data/aigit.sqlite3`, `AIGIT_MAX_BODY_BYTES=1048576`, `AIGIT_REPORT_INTERVAL_SECONDS=900`, and binds the organization-approved interface.

Document exact operations:

```bash
docker compose config
docker compose up -d --build
docker compose exec aigit-server python -m aigit_server.backup /data/backup.sqlite3
docker compose logs --since 10m aigit-server
```

Token rotation accepts current and next digests for 24 hours. SQLite uses WAL, `busy_timeout=5000`, daily online backup, 30-day receipt/event retention minimum and centrally configured longer retention where required.

Implement `aigit_server.backup` with `sqlite3.Connection.backup()`: it accepts exactly one destination path, opens the configured `AIGIT_DB` read-only as the source, writes to a temporary sibling, calls `os.replace()` after success, and exits non-zero without replacing the last backup on failure.

- [ ] **Step 4: Verify image and security**

Run: `python -m pytest tests/integration/test_server_security.py -q`

Expected: all policy tests pass.

Run: `docker compose config`

Expected: exit 0 with exactly one service and one named volume.

- [ ] **Step 5: Commit**

```bash
git add deploy compose.yaml .env.example docs/operations.md tests/integration/test_server_security.py
git commit -m "ops: package lightweight evidence server"
```

### Task 10: Integrate the Generation Skill and Prove 90% Scenario Coverage

**Files:**
- Modify: `skills/tracking-ai-code-contributions/SKILL.md`
- Modify: `skills/tracking-ai-code-contributions/references/recorder-contract.md`
- Create: `src/aigit/skill_adapter.py`
- Create: `tests/golden/scenarios.yaml`
- Create: `tests/golden/test_scenarios.py`
- Create: `docs/pilot-runbook.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: stable `aigit` CLI and all classification/report interfaces.
- Produces: zero-interaction `SkillAdapter`, golden scenario score and 2-week pilot procedure.

- [ ] **Step 1: Write adapter and scenario tests**

`SkillAdapter` exposes `before_apply(session_id, prompt_code_blocks)`, `after_apply(transaction_id, validation)`, and `abort(transaction_id, reason)`. Every method catches recorder availability errors and returns one of `recorded`, `local-only`, or `unavailable`; it never asks the developer to intervene.

The scenario file must contain these 20 named cases with expected classification/invariant:

1. natural-language new module -> `AI_SKILL`
2. natural-language bug fix -> `AI_SKILL`
3. pre-existing dirty diff -> excluded from AI transaction
4. AI commits mixed worktree -> commit does not change origins
5. exact repository copy to new path -> `AI_REUSED`
6. 0.85 near copy to new path -> `AI_REUSED`
7. below-threshold imitation -> `AI_SKILL`
8. true move -> retain origin and action `MOVED`
9. source remains after claimed move -> `AI_REUSED`
10. format-only change -> retain origin and action `FORMATTED`
11. complete prompt patch verbatim -> `USER_SUPPLIED`
12. prompt patch plus AI additions -> split supplied and AI spans
13. observer-healthy external edit -> `MANUAL_CANDIDATE`
14. observer gap external edit -> `UNKNOWN`
15. concurrent inseparable write -> `UNKNOWN`
16. recorder absent -> `unavailable`, coding continues
17. server offline -> `local-only`, coding continues
18. duplicate upload -> one stored event
19. sequence gap -> visible anomaly and degraded coverage
20. pre-adoption source -> `LEGACY_UNKNOWN`

- [ ] **Step 2: Verify RED against the whole system**

Run: `python -m pytest tests/golden/test_scenarios.py -q`

Expected: the suite prints every unsupported scenario ID and fails only when fewer than 18 of 20 are supported; no xfail or skip is allowed.

- [ ] **Step 3: Complete adapter and documentation integration**

Make the generation skill invoke the adapter automatically around each actual patch apply. Preserve the skill's hard attribution rules; replace design-only CLI wording only where implemented behavior differs, and update its `agents/openai.yaml` only if the trigger text changes.

Add README quick start for local-only mode and server mode, but retain all existing trust-boundary warnings. The pilot runbook defines 3 repositories, 5-10 developers, 2 weeks, no manual developer reporting, daily observer/server health review by the system owner, and a frozen scenario matrix.

Pilot acceptance gates:

- at least 18/20 golden scenarios pass, reported as >=90% scenario coverage;
- 100% of pre-existing dirty-diff tests exclude that diff from AI;
- 100% of natural-language AI fix tests count applied fixes as AI;
- 100% of exact-copy-to-new-location tests count `AI_REUSED`;
- no server outage blocks an AI apply;
- no test derives manual contribution as total minus AI;
- observer uptime target >=95%, with all gaps visible;
- p95 `begin` and `end` local overhead each <=500 ms on the pilot repository set, excluding Git content snapshot time for files over the configured limit;
- server batch ingest p95 <=1 second for 500 metadata-only events on the pilot host.

- [ ] **Step 4: Run final verification**

Run: `python -m pytest -q`

Expected: all unit/integration tests pass, at least 18 of 20 golden scenarios are supported, every unsupported ID is printed, and there are zero skips.

Run: `python -X utf8 C:/Users/arthu/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tracking-ai-code-contributions`

Expected: `Skill is valid!` in an environment with PyYAML and UTF-8 mode enabled.

Run: `rg -n "TB[D]|TO[D]O|implement la[t]er|fill in detai[l]s|总量减 AI.*MANUAL_CANDIDATE" README.md docs skills src tests`

Expected: no design placeholders and no prohibited manual backfill rule.

- [ ] **Step 5: Commit**

```bash
git add skills/tracking-ai-code-contributions src/aigit/skill_adapter.py tests/golden docs/pilot-runbook.md README.md
git commit -m "feat: integrate zero-burden attribution skill"
```

## Execution Order and Release Gates

- Tasks 1-5 produce a usable AI transaction recorder；在 Task 5 全部测试通过后可发布内部 `0.1.0-recorder`，但不得声称已有完整本地存量报告。
- Task 6 adds manual-candidate/unknown coverage and must run for 48 hours without an invisible heartbeat gap before wider pilot use.
- Task 8 completes shared local stock reporting；Tasks 1-6 and 8 together are the complete第 1 档。
- Tasks 7-9 produce第 2 档单机服务；server outage tests are a release blocker, but actual server availability is never a coding blocker.
- Task 10 is the only gate for claiming “约 90% 常见作弊路径覆盖”。If fewer than 18 scenarios pass, publish the exact uncovered IDs and do not round the score upward.
- PostgreSQL, model gateway, token interception, Git provider app and CI enforcement are explicitly outside this plan. Revisit them only after the two-tier pilot demonstrates a concrete need.
