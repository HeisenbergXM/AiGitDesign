# AiGitDesign

> 面向“主要由生成代码 skill 驱动、少量逻辑由程序员手工编写”的项目，设计一套不增加程序员操作、能够抵抗常见刷量行为的 AI 代码贡献率统计方案。

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 文档名称 | AI 代码贡献率与人工代码占比统计详细设计 |
| 版本 | v2.0 |
| 状态 | 已批准设计，待 PoC 与红队验证 |
| 更新日期 | 2026-07-12 |
| 统计对象 | Git 仓库目标分支中的代码变更与当前代码快照 |
| 部署范围 | 第 1 档本地来源账本；第 2 档独立轻量审计服务 |
| 明确不要求 | Git 托管平台插件、Webhook、分支规则或 CI/CD 改造 |

## 2. 背景与核心结论

项目的大部分代码由程序员调用生成代码 skill，让大模型创建、修改、调试或重构；只有少量功能逻辑由程序员直接编辑。需要回答两个不同问题：

1. 一段统计周期内，AI 和程序员分别完成了多少有效代码变更？
2. 当前目标分支中，仍然存在的 AI 来源代码与程序员手工代码分别占多少？

只拉取 Git 日志、检查提交信息、作者邮箱或时间戳无法可靠回答这两个问题。Git 保存的是提交对象和文件快照，不保存代码在提交前如何产生；author、committer、message 和日期也都可以由提交者设置或重写。[Git commit 官方文档](https://git-scm.com/docs/git-commit)

本设计采用以下原则：

- **Git 是结算载体，不是来源证明。** Git 用来确定哪些变更最终进入目标分支、哪些代码仍然存在；AI 来源必须由生成时自动产生的事件证明。
- **只做正向归因。** 系统可以证明某段代码经过受控 AI 路径生成和应用；没有 AI 证据不等于一定由人手写。
- **未知必须显式展示。** 采集空窗、外部编辑、旧历史或无法唯一匹配的代码统一进入未知来源，不允许偷偷并入人工分母。
- **生成主体与任务发起人分开。** 程序员发现问题并通过自然语言 Prompt 让 AI 修复，最终 patch 由 AI 产生，仍计为 AI 代码。
- **同时报告开发期和当前存量。** 生成过但被删除的代码不能抬高当前贡献；移动和格式化也不能把整个存量来源洗成 AI。
- **不给程序员增加操作。** 不要求自我申报、添加注释、修改提交信息、切换 Git 身份或补填审计数据。

## 3. 目标、非目标与约束

### 3.1 设计目标

- 自动区分可验证 AI、可观测人工、用户直接提供代码和未知来源。
- 统计“窗口内存活新增/改写贡献率”“有效删除份额”和“当前存量 AI 占比”。
- 正确处理 AI 修 Bug、人工局部修改、仿写、复制、移动、格式化、删除、rebase、squash、cherry-pick 和 revert。
- 防止通过伪造提交信息、AI 身份、时间戳、提交拆分或让 AI 提交人工 dirty diff 来刷高 AI 占比。
- 第 1 档不依赖任何服务；第 2 档只依赖独立审计/模型网关，不要求 Git 或 CI/CD 平台改造。
- 归因失败时降低证据等级，不阻塞程序员编码。
- 对不同 skill、模型、语言和采集版本给出覆盖率与可信度。

### 3.2 非目标

- 不证明法律意义上的著作权、原创性或许可证合规。
- 不保证识别通过个人网页 AI、其他设备或未接入工具生成后粘贴的代码。
- 不把“AI 行数高”等同于代码质量高、开发效率高或 skill 优秀。
- 不使用代码风格、困惑度或另一个大模型的猜测作为正式归因依据。
- 不建设依赖 Git 平台保护规则或 CI 强制检查的第 3 档方案。

### 3.3 零额外工作量约束

程序员仍按原习惯调用 skill、查看结果、运行测试和提交代码。方案不得要求程序员：

- 添加 AI 注释、commit 前缀、trailer、PR 标签或日报；
- 切换 Git 用户、维护 AI 专用邮箱或管理签名密钥；
- 为每个文件、函数或提交选择“AI/人工”；
- 在离线、采集器崩溃或日志缺失后补填来源；
- 为抽样审计回答“这段代码是谁写的”。

## 4. 归因模型

### 4.1 两个正交维度

每个代码 token 同时维护两个维度，避免把“谁执行了动作”和“当前内容从哪里来”混为一谈。

| 维度 | 含义 | 示例 |
|---|---|---|
| `content_origin` | 当前内容的可验证来源路径 | AI 生成、可观测人工输入、用户直接提供、未知 |
| `edit_actor` | 本次新增、修改、删除、复制、移动或格式化由谁执行 | AI、人工、其他工具、未知 |

程序员用自然语言 Prompt 让 AI 修复 Bug 的典型记录为：

```text
content_origin = AI
edit_actor = AI
human_directed = true
```

`human_directed` 只说明任务由人发起，不降低 AI 代码贡献率，也不增加程序员手工代码占比。

### 4.2 来源状态

| 状态 | 定义 | 是否进入 AI 主分子 |
|---|---|---:|
| `AI_EXACT` | 与已记录并实际应用的 AI patch 精确匹配 | 是 |
| `AI_REUSED` | AI 在新目标位置仿写或复制仓库已有代码，包含完全相同的新实例 | 是 |
| `AI_DERIVED` | 源于 AI、后来被部分修改的复合区域标签；不是 token 原子状态 | 按底层仍保留的 AI token |
| `OBSERVED_MANUAL_INPUT` | 受支持的编辑器/终端集成明确记录为已认证程序员直接键入或编辑的 token | 否，进入人工分子 |
| `OBSERVED_NON_AI_PATH` | 发生在受控 AI 事务之外，但无法证明是人工、格式器还是其他工具的编辑 | 否，进入未知分子 |
| `USER_SUPPLIED` | Prompt 中直接提供的大段完整代码或 patch，被模型照抄或轻微改写 | 否，单独展示 |
| `UNKNOWN` | 采集缺失、外部编辑、冲突解决或无法建立唯一因果关系 | 否，进入未知分子 |
| `LEGACY_UNKNOWN` | 采集系统上线前已经存在的代码 | 否，进入未知分子 |

`AI_DERIVED` 只用于函数、代码块或文件级展示。单个 token 的原子状态仍是 AI、人工输入、用户提供或未知之一，公式始终汇总原子 token，禁止把复合区域再次计数。

`USER_SUPPLIED` 默认不直接计入人工下限，因为系统不能证明它是程序员原创、从外部复制还是来自未接入的 AI。只有受支持的输入集成明确记录了其直接编辑过程，才能升级为 `OBSERVED_MANUAL_INPUT`。

“发生在 AI transaction 之外”本身不是人工证据。文件系统监听只能产生 `OBSERVED_NON_AI_PATH`；只有能够区分键入、粘贴、格式器、IDE 重构和工具写入的受支持集成，才能生成 `OBSERVED_MANUAL_INPUT`。粘贴、外部进程写入和来源不明的编辑保持未知。

即便是 `OBSERVED_MANUAL_INPUT`，也只证明代码通过程序员直接输入路径进入编辑器，不证明其思想或更早文本一定未借助外部 AI；这一残余风险必须保留在报告说明中。

### 4.3 Prompt 与仓库上下文的不同处理

- Prompt 只是自然语言需求、Bug 描述、堆栈、测试失败、接口约束或设计思路，具体代码由模型产生：计为 AI。
- Prompt 直接包含完整代码或 patch，模型原样输出：重合部分标记为 `USER_SUPPLIED`。
- 模型参考调用前已经提交到仓库的模块，生成新的相似模块：新实例计为 `AI_REUSED`。
- 程序员在调用 AI 前已经写入工作区的 dirty diff，AI 只执行 add/commit：该 diff 不计 AI。
- AI 在新位置复制既有函数：新物理实例计 `AI_REUSED`，原函数来源不变。
- AI 仅移动或格式化原代码：计入 AI 动作，存量内容来源保持不变。

## 5. 威胁模型与信任边界

### 5.1 假设

- 程序员可以控制本机 Git 配置、提交信息、系统时间和工作区内容。
- 程序员可能尝试修改或绕过本地 skill、观察器和日志。
- 生成代码 skill 可以修改，并能在调用模型、应用 patch 和提交前后自动执行采集逻辑。
- 第 2 档允许使用独立的 HTTP 审计服务；该服务可以同时充当受控模型网关。
- 统计程序原本就需要拉取 Git 日志，因此可以使用现有只读权限 clone/fetch 仓库，但不要求仓库配置发生变化。

### 5.2 能证明和不能证明的内容

系统能够形成并按 `attestation_level` 与 `match_confidence` 报告以下证据：

- 某次受控模型会话返回了特定输出或结构化 patch；
- 本地传感器声明 skill 在特定 before blob 上应用了哪些代码区间；该声明本身仍是 `LOCAL_ASSERTED`；
- 网关签署的模型输出是否由独立报告任务匹配到目标分支中的提交和当前快照；
- 证据链是否完整、是否出现删除、重放、乱序或采集空窗。

系统不能绝对证明：

- 没有 AI 事件的代码一定由人原创；
- 开发者未在个人设备或网页 AI 中生成代码再手工转录；
- 模型输出的代码质量、版权或业务价值；
- 第 1 档本地日志没有被拥有管理员权限的开发者整体替换；
- 第 2 档在没有可信设备证明时，本地“应用事件”绝对没有被恶意客户端伪造。

因此正式报告必须同时显示来源比例、未知比例、证据档位和采集覆盖率。

## 6. 总体架构

```mermaid
flowchart LR
    P["程序员 Prompt / 测试结果"] --> S["生成代码 Skill"]
    S --> B["调用前快照<br/>HEAD / index / dirty diff / blob"]
    S --> G["受控模型调用<br/>输出或结构化 Patch"]
    G --> A["Patch 应用事务"]
    O["工作区观察器"] --> L["本地来源账本<br/>事件哈希链"]
    B --> L
    A --> L
    R["本地 Git 对象<br/>commit / tree / blob"] --> M["Git 归因分析器"]
    L --> M
    M --> D["统计报告"]

    G -.->|"第 2 档：经审计网关"| Q["独立审计服务<br/>服务端时间 / nonce / 签名回执"]
    L -.->|"自动批量上传"| Q
    Q --> W["追加写审计存储"]
    W --> D
```

### 6.1 组件职责

| 组件 | 职责 | 依赖 |
|---|---|---|
| Skill 采集适配器 | 包装模型调用，生成 session/event ID，捕获模型输出和结构化 patch | 可修改的生成代码 skill |
| 调用前快照器 | 记录 HEAD、tree、index、dirty diff 和涉及文件的 before blob | 本地 Git |
| Patch 应用器 | 只标记本次 AI 实际成功应用的区间，不把整个工作区归为 AI | Skill 的文件编辑能力 |
| 工作区观察器 | 标记 AI 事务，记录事务外编辑、输入来源、心跳和采集空窗；不能仅凭事务外推断人工 | 本地后台进程或受支持的编辑器/终端集成 |
| 本地来源账本 | 保存事件、来源 span、哈希链、上传状态和服务端回执 | JSONL 或 SQLite |
| 独立审计服务 | 第 2 档代理模型请求、签署生成事件、保存追加日志 | 普通 HTTP 服务 |
| Git 归因分析器 | 将事件映射到 commit DAG 和目标快照，传播 token 来源 | Git 只读对象 |
| 报告器 | 输出 AI、人工、用户提供、未知、覆盖率和辅助指标 | 归因结果 |

## 7. 端到端数据流

### 7.1 初始化仓库会话

1. skill 自动计算稳定的 `repo_id` 和当前 `workspace_id`。
2. 读取当前 HEAD、tree、index 和 dirty diff。
3. 启动或复用工作区观察器。
4. 写入 `HealthEvent` 和 `SessionStartedEvent`。
5. 第 2 档向审计服务申请一次性 event ID、nonce 和服务端时间。

### 7.2 调用模型

1. skill 标记本次请求为 `human_directed=true`。
2. 本地分析 Prompt 中是否包含完整代码或 patch；默认不保存完整 Prompt。
3. 第 1 档直接调用模型并记录响应摘要。
4. 第 2 档通过审计网关调用模型，由网关先看到并哈希响应，再将响应返回 skill。
5. 模型输出必须尽可能转换为结构化 patch；无法结构化时保留响应 token 指纹和目标文件信息。

第 2 档若只接受客户端事后上报，而不代理或旁路观察模型响应，开发者仍可伪造生成事件。因此推荐让审计服务兼任模型网关，客户端不能自行创建可信 `GenerationEvent`。

### 7.3 应用 patch

1. skill 在应用前记录目标文件 before blob。
2. 打开一个 AI apply transaction。
3. 应用模型产生的 patch。
4. 只记录实际成功写入的 span 和 after blob。
5. 部分应用、失败、Undo 或自动回滚按实际结果记录。
6. 关闭 transaction；事务外编辑先记为 `OBSERVED_NON_AI_PATH`。只有受支持集成提供明确的直接人工输入事件时，相关 token 才升级为 `OBSERVED_MANUAL_INPUT`。

### 7.4 关联 Git 提交

1. 观察器发现 HEAD 或 refs 变化后自动生成 `CommitLinkEvent`。
2. 归因分析器读取 commit、parent、tree 和 changed blob。
3. 使用精确 blob、patch 指纹和 token 上下文把事件映射到提交。
4. 只有进入指定目标分支或指定统计快照的代码才进入正式贡献统计。
5. 第 2 档的报告任务可用普通只读 clone 独立重算，不依赖客户端声称“已经提交”。
6. 报告任务周期性记录 `RefSnapshotEvent`；统计窗口以相邻快照中“新变为可达”的 commit 集合定义，不使用可伪造的 author/committer date 充当真实合并时间。

## 8. 事件与账本设计

### 8.1 通用事件信封

```json
{
  "schema_version": "1.0",
  "event_id": "01K0V6QZB5AF3R8N2M7J9C4DTE",
  "event_type": "PatchAppliedEvent",
  "repo_id": "sha256:53a1f5ce8e4b7c3dcaa1ff221dd9b60c8f59a7f551f4f7023c2c4d6f8e9a102b",
  "workspace_id": "device-7f2c91a4",
  "session_id": "01K0V6QW7D8J4P2HY6N5C3M9RA",
  "sequence": 12,
  "previous_event_hash": "sha256:75f3d51f0c541a9db4cb2ac2587d1672a69cda4b691817ee7e560c429f22b793",
  "client_observed_at": "2026-07-12T10:15:30.123Z",
  "skill": {
    "name": "phaseA-codegen",
    "version": "3.4.1",
    "build_digest": "sha256:29b3dc391f04c89da8230a91f54a8e7517565f539a87c221d8a6da9572fb0c1e"
  },
  "payload_digest": "sha256:14dd9e2637de86f68162b292cd5603f0f74bb25e05e2ea8731774a6db97bb99a",
  "event_hash": "sha256:5f480e6557f9b5989dac081efafa319fe13653f6532c88c01ee5ec4114499d78",
  "attestation_level": "LOCAL_ASSERTED",
  "server_receipt": null
}
```

### 8.2 事件类型

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `SessionStartedEvent` | repo、workspace、base revision、skill/model 模式 | 建立会话边界 |
| `GenerationEvent` | provider、model、request ID、output digest、prompt-code overlap、nonce | 证明受控模型产生了什么 |
| `PatchProposedEvent` | generation ID、base blob、path、patch digest、候选 token spans | 将模型输出绑定到编辑建议 |
| `PatchAppliedEvent` | proposal ID、before/after blob、实际 spans、partial/success | 证明实际写入范围 |
| `WorkspaceEditEvent` | before/after blob、transaction 外 spans、keyboard/paste/formatter/tool/external/unknown 输入来源 | 区分明确人工输入与未知外部编辑 |
| `ValidationEvent` | test/build/package 命令摘要、退出码、关联 generation IDs | 评价 skill 产出质量 |
| `CommitLinkEvent` | commit/tree/parent、matched event IDs、coverage manifest | 绑定 Git 提交 |
| `RefSnapshotEvent` | target ref、observed commit、报告端 observed_at、采集档位 | 定义统计窗口和首次可达时间 |
| `UndoDeleteEvent` | 原事件、被删除 spans、原因类型 | 更新存量与存活率 |
| `HealthEvent` | observer version、start/stop/heartbeat/gap | 计算证据覆盖率 |

### 8.3 哈希链和回执

- v1 事件使用 [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html) 生成 UTF-8、无 BOM 的规范字节；时间统一为 RFC 3339 UTC，`sequence` 为非负 64 位整数。
- 计算 event hash 时，从事件对象中排除 `event_hash`、`server_receipt` 和所有签名字段，但必须包含 `previous_event_hash` 与 `payload_digest`。
- 精确输入为 `SHA-256(UTF8("AIGIT-EVENT-V1") || 0x00 || JCS(event_without_hash_receipt_signatures))`，其中 `0x00` 是域分隔字节。
- 每个本地事件引用前一个事件摘要，形成按会话排序的哈希链；链首的 `previous_event_hash` 使用 32 字节全零值。
- 第 1 档可使用操作系统密钥库或设备密钥为事件批次签名，但本地管理员仍可能绕过整个采集路径。
- 第 2 档 v1 回执固定包含 `event_id`、`event_hash`、`repo_id`、`nonce`、`received_at`、`signature_algorithm` 和 `server_key_id`。服务端对 `UTF8("AIGIT-RECEIPT-V1") || 0x00 || JCS(receipt_without_signature)` 做 Ed25519 签名；更换签名算法必须提升 schema 大版本。
- 只有由模型网关直接产生或观察的 `GenerationEvent` 才能获得 `GATEWAY_GENERATION_ATTESTED`。客户端上传的 apply、manual-input、health 和 commit-link 事件即使取得服务端收件回执，其事实语义仍是 `LOCAL_ASSERTED`。
- 服务端只允许追加，不提供客户端覆盖和删除接口；对象存储可启用保留期或 WORM 策略。
- 服务端签名密钥放在 KMS/HSM 或等价的受控密钥服务中，并支持 key ID 和轮换。
- 重放校验必须同时检查 event ID 唯一性、repo、base blob、path、nonce 和消费状态。

## 9. 两档部署方案

| 能力 | 第 1 档：本地来源账本 | 第 2 档：独立审计服务 |
|---|---|---|
| 程序员额外操作 | 无 | 无 |
| Git/CI 平台改造 | 不需要 | 不需要 |
| 外部依赖 | 无 | HTTP 审计/模型网关 |
| 模型输出证明 | 本地记录 | 服务端观察并签署 |
| 时间可信度 | 本机时间与单调序号 | 服务端时间与 nonce |
| 日志删除/修改检测 | 可检测链断裂，但可整体替换 | 可与远端追加副本比对 |
| 离线开发 | 完全支持 | 可 fail-open；网关未见证的生成永久降为本地证据 |
| 中央聚合 | 需远程调度本地报告或收集报告文件 | 原生支持 |
| 对本机管理员的抵抗力 | 低 | 中等；正向 AI 证据较强 |
| 模型生成证明 | `LOCAL_ASSERTED` | `GATEWAY_GENERATION_ATTESTED` |
| Git 内容链接状态 | `LOCAL_EVIDENCE` | `GATEWAY_SIGNED_PENDING_LINK` 或 `GATEWAY_OUTPUT_GIT_MATCHED` |

### 9.1 第 1 档运行方式

- skill 第一次运行时自动启动观察器；观察器在仓库活跃期间保持运行。
- 账本保存在用户配置目录或仓库外的应用数据目录，避免污染源代码。
- 仅有文件系统监听、没有受支持的编辑器/终端输入集成时，事务外代码全部进入 `U`，人工下限可能为 0；系统不得用“总量减 AI”补出人工比例。
- 统计任务由管理端现有终端管理工具自动调度，或在需要时由统计人员运行；不让程序员提交报告。
- 重启、观察器退出或账本缺失形成明确的 `UNKNOWN` 时间窗。
- 适合试点、验证算法和没有服务条件的团队，不应宣称能够抵抗恶意本机管理员。

### 9.2 第 2 档运行方式

- 审计服务优先兼任模型网关，先签署模型输出，再允许 skill 应用。
- apply、health 和 commit-link 事件在后台批量上传。
- 若模型网关已经签署 `GenerationEvent`，但随后 apply、health 或 commit-link 上传中断，则写入本地队列并标为 `GATEWAY_SIGNED_PENDING_LINK`；补传并与 Git 内容独立匹配后可升级为 `GATEWAY_OUTPUT_GIT_MATCHED`。
- 若生成发生时网关不可达，为了不阻塞编码，skill 可以按策略 fail-open 直连模型，但该次生成永久保持 `LOCAL_EVIDENCE`。事后上传客户端摘要不能补造模型网关见证，也不能升级为 `GATEWAY_GENERATION_ATTESTED` 或 `GATEWAY_OUTPUT_GIT_MATCHED`。
- 上传恢复后校验哈希链、nonce 和事件顺序；无法补传的后续应用事件保持 pending 或降为本地证据。
- 报告服务通过现有只读 Git 凭据 clone/fetch 仓库并独立匹配最终内容；不需要 Webhook、Git App、CI job 或分支策略。
- 若无法提供中央只读 Git 权限，则仍可使用本机生成的 commit manifest，但最终提交映射的可信度必须相应降低。

## 10. Git 与代码谱系归因算法

### 10.1 统计单位和范围

- 内部主单位：语言词法分析器产生的非空白 token。
- 展示单位：token 占比和等价代码行。
- 不把一条混合来源行强制全部归给 AI 或人工。
- 产品代码、测试、配置、注释/文档分别分桶。
- vendor、第三方依赖、生成产物、lockfile、minified 文件、二进制和纯空白变化排除或单列。
- 不支持的语言降级为规范化非空行匹配，并标记较低置信度。

### 10.2 处理步骤

1. 确定目标 ref、统计窗口 `W` 和快照 `T`。
2. 从采集基线到 `T` 按拓扑顺序遍历 commit DAG。
3. 对每个 blob 维护 token span source map。
4. 未变化 blob 直接继承 source map。
5. 变化 blob 按匹配等级查找 AI、明确人工输入或未知编辑事件。
6. 先识别纯格式化事务：若受信格式器事件或规范化 AST 能证明语义未变，则对应语义 token 保持原来源；无法证明为纯格式化时，不得把整段来源转给格式化执行方。
7. 对非纯格式化编辑，通过 token LCS/Myers 映射保留 token；新 token 归有正向证据的动作方，否则归未知。
8. merge 继承两个 parent 的已知来源；无法与父分支或自动合并结果唯一对应的冲突解决内容标为未知。
9. 在目标快照聚合，禁止简单累加所有 commit 的增删行。

### 10.3 原子来源判定优先级

对每个新增或替换 token，按以下顺序判定，后面的规则不能覆盖前面已经确定的来源：

1. **用户直接提供优先。** 与程序员在 Prompt 中直接提交的完整代码/patch 重合时标 `USER_SUPPLIED`；skill 自动装载的仓库上下文不属于“用户直接提供”。
2. **纯移动继承。** 原实例被删除、相同实例出现在新位置且能建立唯一 move lineage 时，保持原内容来源和出生时间。
3. **纯格式化继承。** 受信格式器事件或规范化 AST 证明语义未变时，映射后的语义 token 保持原来源。
4. **AI 新实例。** token 位于模型网关观察到的结构化 patch 中，且由本次 apply 在新目标位置创建：与仓库既有代码相同或高度相似时标 `AI_REUSED`，否则标 `AI_EXACT`。
5. **明确人工输入。** 受支持输入集成提供直接键入/编辑事件时标 `OBSERVED_MANUAL_INPUT`；paste 不使用本规则。
6. **其他情况未知。** 仅知道动作发生在 AI transaction 外、仅有文件系统变化或存在多个候选时，标 `OBSERVED_NON_AI_PATH` 并计入 `U`。

`edit_actor` 不能单独决定 `content_origin`。“新 token 归动作方”只适用于已经通过上述正向证据确定生成来源的情况。

### 10.4 匹配等级

P0–P3 只表示“事件内容与 Git 内容匹配得多精确”，不表示事件本身多可信。报告必须把 `match_confidence` 与 `attestation_level` 分成两个字段。

| 等级 | 条件 | 正式归因 |
|---|---|---|
| P0 | 事件、before/after blob 和 span 完全一致 | 精确内容匹配 |
| P1 | 稳定 patch 指纹、唯一上下文和 token 序列一致 | 高置信归因 |
| P2 | 唯一的文件移动、复制或 token lineage | 保留/复用归因 |
| P3 | 模糊 token、AST 或语义相似 | 只作为审计候选 |
| 无匹配 | 多个候选、证据断裂或外部编辑 | `UNKNOWN` |

`attestation_level` 使用：

- `LOCAL_ASSERTED`：Generation、apply 或 manual-input 事实仅由本地传感器声明。
- `GATEWAY_GENERATION_ATTESTED`：模型网关亲自观察并签署了输出；只证明模型响应，不证明客户端实际应用。
- `GATEWAY_OUTPUT_GIT_MATCHED`：独立报告任务把网关签署的输出内容匹配到目标 Git 快照；证明内容对应关系，仍不把客户端 apply 回执解释为可信执行证明。

审计服务给客户端 `PatchAppliedEvent` 的签名回执只证明“服务在该时间收到这份声明”。它不会把 `LOCAL_ASSERTED` 的 apply 事实升级为远端见证。

rebase、squash 和 cherry-pick 可先使用 `git patch-id --stable` 找等价 patch；Git 官方说明稳定 patch ID 会忽略行号和空白等差异，适合作为候选匹配，但仍需结合 blob 和上下文防止误配。[git patch-id](https://git-scm.com/docs/git-patch-id)

`git blame` 只说明某行最后由哪个提交修改，不能显示已经删除或替换的行，也不能可靠表达混合行的原始来源，因此仅作为回溯和抽样工具，不能作为主算法。[git blame](https://git-scm.com/docs/git-blame)

## 11. 统计指标

设某统计集合中：

- `A`：有正向证据的 AI token，包括 `AI_EXACT`、`AI_REUSED` 以及复合 `AI_DERIVED` 区域中仍保留的 AI 原子 token；
- `H`：有明确输入事件的 `OBSERVED_MANUAL_INPUT` token；
- `S`：已确认由 Prompt 直接提供、但不能证明其更早来源的 `USER_SUPPLIED` token；
- `U`：`OBSERVED_NON_AI_PATH`、`UNKNOWN` 和 `LEGACY_UNKNOWN` token；
- `N = A + H + S + U`。

### 11.1 证据覆盖率

```text
Attribution Coverage = (A + H) / N
Observed Path Coverage = (A + H + S) / N
```

`Attribution Coverage` 表示能够直接归给 AI 或人工的范围；`Observed Path Coverage` 还包含已观察到、但不能证明更早作者的用户提供代码。覆盖率必须与所有贡献率同时展示。覆盖率低时禁止只显示 `A / (A + H)` 并称其为整个项目 AI 占比。

### 11.2 窗口内存活新增/改写贡献率

报告任务在窗口开始记录基线 `RefSnapshotEvent(B)`，在窗口结束记录 `RefSnapshotEvent(T)`。定义 `C(B,T)` 为在 `T` 可达但在 `B` 不可达、并且在 `T` 仍然存在的新增或实质改写 token。窗口由两个实际观察到的 ref 快照定义，不使用 Git author/committer date 推断合并时间。

若没有基线快照或明确指定的 base commit，则不发布这一窗口指标，只发布当前存量；不得从当前 Git 日期补造“首次进入目标分支”的时间。

```text
AI 窗口存活贡献率下限 = A_C / N_C
受控 Skill AI 情景上限 = (A_C + U_C) / N_C
潜在 AI 来源上限 = (A_C + S_C + U_C) / N_C
人工窗口存活贡献率下限 = H_C / N_C
人工窗口存活贡献率上限 = (H_C + S_C + U_C) / N_C
```

生成后删除、未进入目标分支或被完整重写的代码不进入“存活新增/改写”分子。程序员用 Prompt 让 AI 修 Bug、补测试或重构所产生且仍保留的新 token 进入 AI 分子。`受控 Skill AI 情景上限` 按已确认政策排除 `USER_SUPPLIED`；`潜在 AI 来源上限` 则承认用户提供代码在更早阶段也可能来自未接入 AI。前者用于评价当前 skill，后者才是来源不确定性的数学上限。

### 11.3 当前存量 AI 占比

```text
AI 存量占比下限 = A_T / N_T
受控 Skill AI 存量情景上限 = (A_T + U_T) / N_T
潜在 AI 存量来源上限 = (A_T + S_T + U_T) / N_T
人工存量占比下限 = H_T / N_T
人工存量占比上限 = (H_T + S_T + U_T) / N_T
```

旧代码不会因为一次 AI 格式化或移动就整体变成 AI；AI 在新位置仿写或复制产生的新实例计为 `AI_REUSED`。

### 11.4 AI 开发动作占比

```text
AI 动作占比 =
AI 执行的新增、替换、删除、复制、移动、格式化动作量
/ 全部已观测动作量
```

动作占比用于说明 AI 承担了多少操作，不作为存量原创占比。纯移动、纯格式化和删除必须单独列出，防止用低价值 churn 抬高数字。

### 11.5 有效删除贡献

删除不会形成当前存量 token，但可能完成简化、漏洞清理或功能移除，因此单独报告：

```text
D_A = 由 AI 删除且到快照 T 仍未恢复的 token
D_H = 由明确人工输入路径删除且到 T 仍未恢复的 token
D_U = 删除执行方未知且到 T 仍未恢复的 token
D = D_A + D_H + D_U

AI 有效删除份额下限 = D_A / D
AI 有效删除份额上限 = (D_A + D_U) / D
删除归因覆盖率 = (D_A + D_H) / D
```

分母必须包含未知删除，不能只在“已归因删除”中计算 AI 百分比。该指标还要按被删除 token 的原来源、删除执行方和删除原因分桶；未知执行方不并入人工。它与存活新增/改写贡献并列，不相加为一个百分比。

### 11.6 入库转化率

```text
AI 入库转化率 =
最终匹配到目标分支的 AI token
/ 实际应用到工作区的 AI token
```

它能区分“模型生成很多”与“真正进入项目”。编辑器接受率、Prompt 数、token 消耗和 session 数只能作为使用量指标，不能代替贡献率。

### 11.7 存活率与返工

- 分别报告 AI 代码 14、30、90 天的严格存活率和谱系存活率。
- 严格存活要求规范化 token 未变。
- 谱系存活允许移动、拆分和局部保留。
- 同时报告早期删除、revert、人工重写和后续 Bug 修复次数。
- 存活率是稳定性信号，不自动等同于质量。

### 11.8 复用和去重指标

```text
AI 复用率 = AI_REUSED token / 全部 AI token
```

复制生成的新实例仍进入“物理实例 AI 占比”，满足本项目对 AI 执行仿写/复制的归因规则。同时必须把以下两项作为并列主指标，任何报告不得只展示前者：

- **物理实例 AI 占比：** 每个实际存在的实例都计数，`AI_REUSED` 进入 AI 分子。
- **clone-family 去重 AI 独特占比：** 对规范化 token/AST clone family 只计算一个代表；family 的独特来源由最早可验证内容来源决定。AI 从人工 family 复制的新实例增加 AI 物理贡献，但不虚构 AI 独特创造。

clone 算法、相似度阈值和最小片段长度必须版本化。物理实例占比不得单独用于评价 skill 或人员；去重指标也不能替代对合法复用工作量的展示。

## 12. 边界案例

| 场景 | `edit_actor` | 存量来源处理 |
|---|---|---|
| 程序员发现 Bug，Prompt 让 AI 修改 | AI | AI 新增或替换 token 计 AI |
| 程序员让 AI 反复调试直到通过测试 | AI | 每次实际保留的 AI token 计 AI |
| AI 修改人工旧函数的一小部分 | AI | 新 token 为 AI，未修改 token 保持人工 |
| 受支持输入集成记录到人工局部修改 AI 代码 | 人工 | 保留 AI token 仍为 AI，新 token 为人工 |
| AI 仿照已有模块生成新模块 | AI | 新模块计 `AI_REUSED`，即使文本相同 |
| AI 把函数复制到新文件 | AI | 新实例计 `AI_REUSED`，原实例不变 |
| AI 把函数从 A 移到 B | AI | 动作计 AI，原内容来源和出生时间保持 |
| AI 只格式化人工代码 | AI | 纯空白不进主分母，非空白 token 来源不变 |
| 人工先写 dirty diff，AI 只 add/commit | AI 提交动作 | dirty diff 不计 AI 生成 |
| Prompt 直接给完整代码让 AI 插入 | AI | 重合部分为 `USER_SUPPLIED` |
| AI 生成后又删除 | AI | 动作和有效删除指标按实际记录，当前存量及窗口存活新增贡献为 0 |
| 人工复制 AI 代码 | 人工 | 新实例保留 AI 内容来源，动作记人工复制 |
| AI 复制人工代码 | AI | 新实例按已确认规则计 `AI_REUSED` |
| AI 删除人工代码 | AI | 计 AI 删除动作，不产生负 AI 存量 |
| rebase/squash/cherry-pick | 原动作方 | patch 指纹和 token 上下文重关联，只计一次 |
| revert | revert 执行方 | 被撤销代码退出当前存量 |
| merge conflict 手工解决且无事件 | 未知 | 冲突中新写内容标未知 |
| 网页 AI 生成后粘贴 | 未知 | 粘贴事件不构成人工原创证明，也没有受控 AI 证据 |
| 采集上线前历史 | 未知 | 标 `LEGACY_UNKNOWN` |

## 13. 防作弊设计

| 作弊或异常方式 | 控制措施 | 残余风险 |
|---|---|---|
| 模仿 `feat(AI):`、`fix(AI):` | 提交信息不参与正式归因 | 无 |
| 伪造 AI 用户名、邮箱和提交时间 | Git 自述字段不参与正式归因 | 无 |
| 先手写代码，再让 AI 提交 | 调用前快照排除既有 dirty diff；只认本次 AI patch | 人工可先删除再设计复杂提示让模型重建 |
| 把人工完整代码放入 Prompt | 计算 Prompt-code overlap，重合部分标用户提供 | 编码、拆分或语义改写只能提高审计风险，无法绝对识别 |
| 让 AI 提交整个工作区 | Patch 应用器只产生实际 AI spans，提交范围不改变归因 | 被篡改的第 1 档客户端仍可绕过 |
| 大量生成再删除 | 主指标只统计进入目标分支且仍存活的代码 | 动作指标仍会升高，因此必须分开展示 |
| 大量复制已有代码 | 计 `AI_REUSED`；物理实例与 clone-family 去重占比强制并列展示 | 业务上合理复制与刷量仍需结合质量指标 |
| 来回移动或格式化项目 | 只增加动作指标，不改变存量来源 | 无 |
| 拆分、合并或改写提交历史 | 按内容和 DAG 归因，不依赖提交粒度 | 实质重写可能降为未知 |
| 删除、修改或重排本地日志 | 哈希链检测；第 2 档与服务端副本核对 | 第 1 档可被本机管理员整体替换 |
| 重放其他会话事件 | 绑定 repo/base/path/nonce，事件只能消费一次 | 审计服务状态丢失时需从追加日志恢复 |
| 修改系统时间 | 使用 sequence；第 2 档使用服务端时间 | 第 1 档时间只能视为辅助信息 |
| 关闭观察器后手工修改 | 心跳空窗中的变化标未知，覆盖率下降 | 无法确认具体动作方 |
| 使用未接入 AI 后粘贴 | 标未知，不使用被动检测器补写 ownership | 无法得到 AI 的完整召回率 |
| 修改 skill 伪造客户端事件 | 第 2 档可信 GenerationEvent 只能由模型网关签发 | 本地 apply 仍是弱信任传感器 |

必须承认：在不使用受管远程开发环境、设备证明、Git 服务端校验和 CI 强制策略的前提下，不可能对拥有本机控制权的恶意开发者实现绝对防作弊。本设计通过正向模型证据、独立 Git 内容核验、未知分桶和辅助指标，旨在提高作弊成本与可见性，但实际效果必须通过 PoC 和红队验证，也不宣称达到司法取证级保证。

## 14. 异常与降级处理

| 异常 | 行为 |
|---|---|
| 网关已签署生成，但后续上传中断 | 不阻塞编码；后续事件进入本地队列并标 `GATEWAY_SIGNED_PENDING_LINK` |
| 已签署生成的后续事件补传成功 | 校验链路并独立匹配 Git 内容后升级为 `GATEWAY_OUTPUT_GIT_MATCHED` |
| 生成时网关不可达 | 可 fail-open 直连模型，但永久保持 `LOCAL_EVIDENCE`，不得事后升级 |
| 后续事件永久无法补传 | 保持 pending 或本地证据，不伪造服务端见证 |
| Patch 部分应用 | 只归因成功写入且能匹配的 spans |
| Undo、delete 或 revert | 自动更新存量和存活记录 |
| 观察器崩溃 | 用 HealthEvent 划定空窗，空窗内新增归未知 |
| 不支持的语言 | 降级到规范化行匹配并显示低置信度 |
| 二进制或生成产物 | 排除或单独分桶 |
| Merge conflict 无唯一来源 | 冲突中新内容归未知 |
| commit 被 force rewrite | 重新遍历可达 DAG；不可达代码退出目标快照统计 |
| 事件 schema 升级 | 保存 schema、skill、parser 和算法版本，禁止静默混算 |
| 签名密钥轮换 | 回执携带 key ID；验证器保留受信历史公钥 |

## 15. 报告设计

每份报告必须包含：

- 仓库、目标 ref、快照 commit、统计窗口和生成时间；
- `attestation_level`：`LOCAL_ASSERTED`、`GATEWAY_GENERATION_ATTESTED` 或 `GATEWAY_OUTPUT_GIT_MATCHED`；
- 链接状态：`LOCAL_EVIDENCE`、`GATEWAY_SIGNED_PENDING_LINK` 或已完成 Git 匹配；
- AI、人工、用户提供、未知和遗留未知 token/等价代码行；
- 已验证 AI 下限、受控 Skill AI 情景上限、潜在 AI 来源上限，以及人工上下限；
- 证据覆盖率、观察器在线率和待上传事件数；
- `AI_EXACT`、`AI_REUSED`、`AI_DERIVED` 明细；
- 物理实例 AI 占比与 clone-family 去重 AI 独特占比，二者均为主指标；
- 产品代码、测试、配置、注释/文档分桶；
- AI 动作占比以及新增、替换、删除、移动、格式化拆分；
- 入库转化率、14/30/90 天存活率、revert 和后续重写；
- skill、模型、观察器、parser 和归因算法版本；
- P0/P1/P2/P3 匹配数量和未知原因分布。

示例：

| 指标 | 示例值 | 解释 |
|---|---:|---|
| 当前已验证 AI 存量下限 | 62% | 有正向证据的 AI 代码 |
| 受控 Skill AI 存量情景上限 | 68% | 假设 6% 未知均来自当前受控 AI 路径，但排除 5% 用户提供代码 |
| 潜在 AI 存量来源上限 | 73% | 用户提供和未知都可能在更早阶段来自未接入 AI |
| 当前人工存量下限 | 27% | 受支持输入集成确认的直接人工输入 |
| 当前人工存量上限 | 38% | 假设 5% 用户提供和 6% 未知最终都属于人工 |
| 用户提供占比 | 5% | 已知不是本次受控模型新生成，但更早来源不明 |
| 未知占比 | 6% | 不强行归给任何一方 |
| 直接归属覆盖率 | 89% | 可直接归给 AI 或人工的部分 |
| 路径观察覆盖率 | 94% | 另包含已观察到的用户提供路径 |
| AI 复用率 | 21% | AI 代码中来自仓库仿写/复制的新实例 |
| clone-family 去重 AI 独特占比 | 49% | 去掉重复实例后，由 AI 首次产生的独特内容份额 |
| AI 30 天谱系存活率 | 87% | 允许移动与局部修改 |

AI 与人工的上下限会因用户提供和未知来源而重叠，不能相加成一个饼图。禁止为了得到看似整齐的 100% 而隐藏不确定性。

## 16. 如何评价生成代码 skill

AI 代码占比只回答“代码通过什么路径产生”，不能单独证明 skill 优秀。建议按 skill 版本和团队聚合以下指标，避免把刷行数变成个人激励：

- AI patch 入库转化率；
- 首次测试/编译/打包通过率；
- 达到测试通过所需的 AI 迭代次数；
- 14/30/90 天存活率；
- 人工后续重写比例；
- revert、回滚和缺陷修复比例；
- `AI_REUSED` 物理实例占比与 clone-family 去重 AI 独特占比，必须并列评价；
- 单位有效存活代码的模型成本和耗时；
- 同类任务在不同 skill 版本间的团队级纵向变化。

已有测试、编译或打包 skill 可以自动生成 `ValidationEvent`，不要求程序员额外填写结果。

## 17. 对原 README 措施的评估

| 原措施 | 保留方式 | 正式证据等级 |
|---|---|---|
| AI 在类、方法或代码块中添加时间和用途注释 | 不建议；注释可编辑、会过期并污染源码 | 不作为证据 |
| 记录 AI 提交树、文件路径和 Git 哈希 | 升级为 patch、before/after blob、event ID 和来源 span manifest | 重要辅助证据 |
| `feat(AI):` / `fix(AI):` 提交模板 | 可保留用于阅读和搜索，不进入公式 | 弱展示信号 |
| AI 独立 Git 用户和邮箱 | 对全自动 Agent 便于展示，不能证明逐行来源 | 弱身份信号 |
| 生成、测试后立即提交 | 保留缩短匹配链路；只能提交本次 AI patch，不能吞并既有 dirty diff | 有用流程优化 |
| 本地 Git hook | 可用于自动唤醒采集器，但 hook 可被 `--no-verify` 绕过 | 不作为信任根 |

Git 官方说明客户端 hook 可以被跳过，因此 hook 只能改善自动化体验，不能作为防作弊根基。[Git hooks](https://git-scm.com/docs/githooks)

## 18. 其他公司、产品和可行方案

截至 2026-07-12，主流产品已经较普遍地提供 AI 使用量和接受行遥测，但把 AI 内容持续关联到 commit 或已合并代码的能力仍不统一。未找到与本文两档架构完全同构、且公开披露准确率和对抗测试结果的公司案例；下表是各组成机制的官方能力先例，不是本设计已经生产验证的证明。“已有能力”来自链接中的官方说明，“可以借鉴”是本文据此作出的工程推论。

| 方案 | 已有能力 | 可以借鉴的部分 | 不能直接证明的内容 |
|---|---|---|---|
| GitHub Copilot Usage Metrics | 组织/企业维度的建议、接受、AI 增删行和 Agent 活动 | 自动采集、按工具/语言/功能分桶 | 编辑器加入的代码是否提交、合并或仍然存在。[官方文档](https://docs.github.com/en/copilot/reference/copilot-usage-metrics/lines-of-code-metrics) |
| GitHub Copilot cloud agent | Agent author、任务发起人 co-author、签名提交、session 日志和 audit session ID | 服务端生成事件与提交的正向关联 | 人工后续编辑后每个 token 的来源。[会话追踪](https://docs.github.com/en/enterprise-cloud%40latest/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents)、[审计事件](https://docs.github.com/en/enterprise-cloud%40latest/copilot/reference/agentic-audit-log-events) |
| Cursor AI Code Tracking API | 按 commit SHA 输出 Tab、Composer 和推导的 non-AI 增删行，并提供 accepted change ID | 客户端事件关联 commit 的数据模型 | 未支持工具、网页粘贴和被绕过客户端；non-AI 是差值而非独立人工证明。[官方文档](https://docs.cursor.com/en/account/teams/ai-code-tracking-api) |
| JetBrains AI Activity and Impact | 自动统计生成、接受、修改、删除和支持范围内的 AI 工具活动 | IDE/插件版本覆盖率、AI 与人工 origin 视图 | IDE 外提交和未覆盖 Agent；接受后删除或手改会影响解释。[官方文档](https://www.jetbrains.com/help/ide-services/ai-activity-and-impact.html) |
| Claude Code Analytics / OpenTelemetry | session、工具决策、LoC、commit、PR、token、成本和活跃时间 | 受管配置、后台 OTel 事件和 skill 遥测 | LoC 或工具接受事件本身不能证明代码存活。[官方文档](https://code.claude.com/docs/en/monitoring-usage) |
| Claude Code contribution metrics | 将 Claude session 输出与已合并 PR 内容做高置信匹配 | 从“曾生成”推进到“仍存在于合并代码” | 仍是内容匹配而非绝对来源证明，且依赖 GitHub App。[官方文档](https://code.claude.com/docs/en/analytics) |
| DX AI Code Insights | 端侧 daemon/hooks 记录 original、modified、remaining、deleted AI LoC 和 commit SHA | 跨工具端侧谱系、代码保留和修改状态 | 浏览器粘贴与未支持 Agent；daemon 仍位于开发者机器。[数据模型](https://docs.getdx.com/schema/ai_code_commits/)、[限制说明](https://docs.getdx.com/ai-code-insights/troubleshooting/) |
| LinearB AI Analytics | 结合厂商 API、Bot author、co-author 和时间相关性标记 AI-assisted commit/PR | 完全后台化、与交付指标联动 | 官方说明部分归因是启发式，不是逐行事实。[官方说明](https://linearb.helpdocs.io/article/7y766re1v8-how-linear-b-calculates-ai-attribution) |
| Swarmia AI impact | 结合 Agent author、co-author 和 24 小时使用窗口识别 AI-assisted PR | 自动展示归因理由、无需程序员打标签 | 官方明确说明时间相关性会产生假阳性，而且属于 PR 级标签而非逐行来源证明。[官方说明](https://help.swarmia.com/use-cases/measure-the-productivity-impact-of-ai-tools/ai-impact-on-pr-metrics) |
| Devin Desktop Analytics | completion stats、AI-written percentage、总行数、tool calls 和团队使用趋势 | 自动化团队趋势与 AI 动作占比 | 官方页面未提供通用的 commit 或当前存量来源证明。[官方文档](https://docs.devin.ai/desktop/accounts/analytics) |

DX 官方还公开了 Booking.com 使用 DX 衡量生成式 AI 采用和交付影响的案例；该材料来自供应商案例而非独立审计，应作为实践参考而不是准确性证明。[Booking.com 案例](https://getdx.com/customers/booking-uses-dx-to-measure-impact-of-genai/)

### 18.1 可直接采用、且不增加程序员工作量的措施

- 在自研 skill 中内置生成事件、patch 和 before/after blob 采集。
- skill 自动启动工作区观察器或端侧 daemon。
- 将模型调用统一经过独立轻量网关，由服务端签署响应摘要。
- 统计服务使用现有只读权限拉取 Git 日志和对象，不改仓库配置。
- 通过 commit 内容匹配而不是 author/message 自述归因。
- 用追加日志、事件哈希链、服务端 nonce 和一次性事件消费检测篡改与重放。
- 自动记录测试、编译和打包结果，用保留率与返工评价 skill。
- 由统计团队执行分层抽样审计，不找程序员补标。

### 18.2 只能作为弱信号的措施

| 方法 | 正确定位 |
|---|---|
| Commit message、AI 邮箱、co-author、分支前缀 | 搜索、展示或启发式筛选 |
| Signed commit | 证明某个密钥签过 commit 对象，不证明内容由 AI 或人工产生 |
| Git notes | 保存报告 URI 或摘要，note 自身可增删改，不能当信任根 |
| AI 注释和时间戳 | 可编辑、易过期、污染代码 |
| Prompt、session、token 数 | 使用量，最容易通过空跑和批量生成刷高 |
| 被动机器学习检测器 | 抽样优先级或研究，不写回 ownership |
| 代码风格、命名和注释密度 | 格式器、团队规范和人工改写都会改变 |
| 代码水印 | 阳性可作模型特定佐证；阴性不能证明人工 |
| 让另一个大模型判断 | 不可校准、不可复现，不用于人员判断 |

一项跨语言、跨数据集和跨生成模型研究发现，现有自然语言 AI 检测器迁移到代码后多数准确率不足以承担实际来源判定，说明生产系统应优先做生成路径追踪而非事后猜测。[ICSE 2025 研究](https://arxiv.org/abs/2411.04299) Google 对 SynthID 的官方说明也强调水印检测是概率性的，彻底改写会降低置信度。[SynthID 局限](https://ai.google.dev/responsible/docs/safeguards/synthid)

## 19. 隐私与安全

- 默认不持久化完整 Prompt；只保存摘要、代码重合判定和必要 span。
- 原始 patch 默认保存在本机；远端可选择加密 patch、token 指纹或仅摘要模式。
- 公开或共享报告不得包含 Prompt、源代码片段、密钥、凭据或业务敏感上下文。
- 即使只保存哈希，低熵代码片段仍可能被猜测；敏感摘要应使用带租户密钥的 HMAC 或加盐策略。
- 审计服务按组织、仓库和角色做访问控制，并记录管理员读取和导出行为。
- 配置明确的数据保留期；原始事件与聚合报告采用不同保留策略。
- 服务端密钥与业务数据库分离，支持轮换、吊销和历史回执验证。
- 个人维度仅用于排查覆盖问题，不建议作为绩效、纪律或奖金的唯一依据。

## 20. 验证与验收

### 20.1 黄金测试仓库

建立带真实来源标签的自动测试场景：

- AI 新增、明确人工输入、事务外未知编辑、自然语言 Prompt 后 AI 修 Bug；
- AI 仿写、完全复制、移动、格式化和局部重写；
- Prompt 直接粘贴完整代码；
- 人工 dirty diff 交给 AI add/commit；
- 部分应用、Undo、delete、revert；
- rebase、squash、cherry-pick 和 merge conflict；
- 网关已签署后的离线补传、网关未见证的 fail-open、事件重放、日志删除和观察器中断；
- 多语言、不同换行符、编码和不支持 parser；
- 生成文件、vendor、lockfile 和二进制排除规则。

### 20.2 必须通过的规则

- AI 精确 patch 在 P0 场景中不得被归为人工。
- 人工调用前 dirty diff 不得因 AI commit 动作变成 AI。
- 自然语言 Prompt 让 AI 修复产生的新代码必须计 AI。
- 仿写和复制到新位置的实例必须计 `AI_REUSED`。
- 纯移动和格式化不得转移存量来源；无法证明为纯格式化的编辑不得整段改归执行方。
- 观察器空窗不得默认归人工。
- 服务端回执篡改、nonce 重放和跨仓嫁接必须被拒绝。
- rebase、squash、cherry-pick 和 merge 不得重复计算同一物理实例。
- 审计服务故障不得阻塞正常编码，但网关未见证的生成不得事后升级为远端可信。
- 整个流程不得出现要求程序员补标签或补说明的交互。

### 20.3 生产抽样审计

由平台或分析人员按工具、语言、匹配等级、复制/移动、大 patch 和未知原因分层抽样。分别从“已判 AI”和“全部新增/未知”两个样本框抽取，报告 precision、recall、false-positive rate、coverage 及 95% 置信区间。

被过度抽样的高风险分层必须按总体权重还原，不能直接平均。算法、skill、插件、模型或 parser 版本变化后重新校准；P3 模糊匹配在审计达标前始终保持未知。

## 21. 推行步骤

### 阶段 0：规则冻结与静默基线

- 冻结文件排除、tokenizer、目标分支和来源状态定义。
- 将上线前代码标为 `LEGACY_UNKNOWN`。
- 运行黄金测试，不发布个人数据。

### 阶段 1：第 1 档试点

- 在少量仓库启用本地账本和观察器。
- 验证工作区性能、Git 映射、语言覆盖和未知原因。
- 只做团队/skill 版本报告，不用于奖惩。

### 阶段 2：第 2 档上线

- 将模型调用切换到独立审计网关。
- 启用服务端 nonce、签名回执和追加存储。
- 报告服务使用现有只读 Git 权限独立核验。
- 对比第 1 档与第 2 档覆盖率和归因差异。

### 阶段 3：稳定运营

- 固定月度或版本周期报告。
- 监控观察器健康、待上传事件和 schema 覆盖。
- 用转化率、存活率、返工和成本改进 skill。
- 定期更新业界工具连接器，但不改变“正向证据优先、未知不猜测”的原则。

## 22. 最终结论

在当前“不增加程序员工作量、不改 Git/CI 平台”的约束下，推荐的做法不是要求程序员在提交中声明“这是 AI 写的”，而是让生成代码 skill 在代码产生和应用的瞬间自动形成来源事件，再用 Git 判断这些代码是否真正进入目标分支并保留至今。

第 1 档适合低成本试点和本地统计，但不能抵抗拥有本机控制权的恶意开发者。第 2 档通过受控模型网关、服务端回执、追加日志和只读 Git 内容核验，预期提高正向 AI 归因可信度，同时不需要 Git 托管平台或 CI/CD 配合；实际提升幅度必须由 PoC、黄金集和红队测试验证。

正式报告始终保留 `AI`、`人工`、`用户提供` 和 `未知`，并同时展示窗口存活新增/改写、有效删除、当前存量、动作占比、证据覆盖率、复用率和存活率。这样既能减少刷量指标的影响，也不会用一个看似精确、实际可伪造的百分比误导项目决策。
