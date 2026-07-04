# TEE 内明文 Postgres 迁移设计（feedling-mcp）

日期：2026-07-04
状态：草案，待用户确认四个决策点（见 §1）
参考：`~/Downloads/on-disk-postgres-reference.md`（hivemind-core 生产经验蒸馏，含四条事故修正）

## 0. 目标与非目标

**目标（终态）：**

- 用户数据存在一个独立 TEE CVM 内的 Postgres 里，**明文存储**，落在 CVM 的
  FDE（LUKS2）加密磁盘上——静态加密由平台透明提供。
- 停掉外部 AWS RDS（prod + test 两套）。
- 删除 app（iOS）与后端的整个 v1 信封加解密层：`ContentEncryption.swift`、
  `backend/content_encryption.py`、`backend/core/envelope.py`、enclave 的
  content decrypt 路由、rewrap/swap 密文路径。
- 过渡期与现有 RDS 双写，任何时刻可回滚。

**替代性信任边界（照搬参考文档 §1）：明文永不离开 enclave。** 出边界的只有两样，各有保护：

| 出边界流量 | 保护 |
|---|---|
| SQL 跨 CVM（app/runner CVM ↔ pg CVM） | gateway TLS 透传，TLS 在 pg CVM 内终止 + PG 密码认证 |
| WAL/base backup → R2 | WAL-G libsodium 客户端加密（**强制，fail-closed**） |
| frame body → R2（若保留 offload） | TEE 内存储层对称加密（KMS 派生钥） |

**非目标：**

- 不改动托管 agent 运行时、litellm gateway、proactive 等业务逻辑（它们只透过
  db.py 读写，不感知底层换库）。
- 不做 RDS 内密文的原地解密（RDS 从头到尾只见过密文，无新增暴露）。

## 1. 决策点（按推荐值行文，均待用户确认）

| # | 决策 | 推荐值 | 备选 |
|---|---|---|---|
| D1 | `local_only` 隐私等级命运 | **取消，全部明文**。存量 local_only 服务端解不了，需 iOS 配合重传明文；重传不了的接受丢弃（prod 用户少且均为 tester） | 保留为密文特例列（iOS 永久保留加解密代码，与目标冲突）；或存量直接丢弃 |
| D2 | 部署拓扑 | **独立 pg CVM + 原生 PG wire over gateway TLS 透传**（`<pg-app-id>-5432s.<gw>`）。db.py 几乎不改，LISTEN/NOTIFY 保住，app 部署不碰 DB | 塞进主 app CVM（数据生命周期绑死主 CVM）；HTTP sql-proxy（db.py 重写 + wake bus 死，排除） |
| D3 | 切读窗口的旧客户端 | **协调 iOS 强更，短窗口**：backend 切读与 iOS 明文版同步发布，旧版信封写直接 400/426 提示升级。不碰 enclave 热路径 | 兼容期内旧信封写走 enclave 同步解密落库（单线程 enclave 是已知 502 瓶颈，低流量勉强可行） |
| D4 | R2 上的 frame body | **保留 R2 offload + 存储层加密**：TEE 内用 KMS 派生的服务端对称钥加解密。这是存储层加密（同 WAL-G 备份一个性质），不算 app 信封层 | 全部收回 PG 明文（磁盘与备份体积大涨） |

**关键教训（来自本仓库历史事故，必须遵守）：pg CVM 使用独立的 AppAuth 合约**，
绝不与主 app 或 runner 共享——上次 runner 复用主 AppAuth 直接导致主 enclave
KMS 内容钥被换（0a5578→2d642e 事故）。

## 2. 终态架构

```
┌─ 主 app CVM ─────────────┐      ┌─ pg CVM (feedling-pg-{test,prod}) ──┐
│ ingress/backend/mcp/     │ TLS  │ postgres:17 (direct TLS, :5432)     │
│ enclave                  ├─────►│   pgdata volume on FDE disk         │
│ DATABASE_URL=postgres…   │ 透传 │ WAL-G ──► R2 (libsodium 加密)       │
└──────────────────────────┘      │ cron: 每日 base backup 03:00 UTC    │
┌─ runner CVM (test/prod) ─┤      │ 独立 AppAuth 合约                    │
│ agent-runner / genesis   ├─────►│ 手动部署，排除在 app 自动部署外      │
└──────────────────────────┘      └─────────────────────────────────────┘
```

- 每个环境一个 pg CVM：`feedling-pg-test`、`feedling-pg-prod`（先 test 全流程走完再 prod）。
- 消费方共 3 个 CVM（主 app、test runner、prod runner），全部通过同一种连接串访问。
- backend 侧只换 `DATABASE_URL`；psycopg3 连接池、LISTEN/NOTIFY wake bus、
  Alembic 全部照旧工作（原生 wire 协议）。

### 网络路径（D2 的技术核心，Phase 0 spike 验证）

gateway 按 SNI 路由，`s` 后缀 = TLS 透传（gateway 只见密文，参考
`dstack-tutorial/04-gateways-and-tls`）。要求客户端 **TLS-first + SNI**：

- **首选**：Postgres 17 支持 direct TLS（ALPN，跳过明文 SSLRequest 前导），
  libpq ≥17 客户端加 `sslnegotiation=direct sslmode=verify-full`。
  需确认 psycopg3 所带 libpq 版本 ≥17（psycopg[binary] wheel 或容器内装 libpq17）。
- **兜底**：每个消费 CVM 加一个 stunnel client sidecar
  （localhost:5432 → TLS+SNI → gateway → pg CVM 内 stunnel server → postgres），
  对 psycopg 完全透明，任何 libpq 版本可用。
- **必测项**：LISTEN/NOTIFY 专用长连接过 gateway 的空闲超时/keepalive 行为
  （wake bus 靠一条池外常驻连接，`backend/db.py:140-145`）；断线重连逻辑已有则确认，
  没有则补。另测跨 CVM RTT 对比现 RDS（现状本来就出 CVM 到 us-east-1，预期不变差）。
- 认证：PG 强密码（Phala 加密 env 注入）+ TLS 证书校验（客户端 pin pg CVM 证书
  或 verify-full）。数据库层面继续单库多 schema/表，不做多租户。

## 3. pg CVM 构成（照搬 hivemind + 四条修正）

从 `hivemind-core` 复制并改造（源文件见参考文档末表）：

- **镜像** `deploy/postgres/Dockerfile`：pinned `postgres:17@sha256:…` digest
  （可复现 compose hash，attestation 需要）、WAL-G 静态二进制、cron。
- **compose** `deploy/docker-compose.phala.postgres.yaml`：
  - 命名卷 `pgdata`（原地 compose 更新不丢数据）；
  - `pg_isready` healthcheck；
  - **修正 1**：archive 配置全部走 compose `command` 服务器旗标
    （`archive_mode=on`、`archive_command=wal-g wal-push %p`、
    `archive_timeout=60`、`wal_level=replica`），**不用** initdb 脚本
    （volume 早于备份配置时会静默不归档）；
  - direct TLS / stunnel server 配置。
- **entrypoint**（改造 hivemind 的 `entrypoint-wrapper.sh`）：
  - **修正 4（fail-closed）**：`WALG_S3_PREFIX` 已设而 `WALG_LIBSODIUM_KEY`
    未设 → exit 1。密钥 `openssl rand -hex 32` 生成一次，存密钥管理 **和**
    enclave 平台之外的 break-glass 位置；
  - **修正 2**：cron 环境写入 `PGHOST=/var/run/postgresql`、`PGUSER`、
    `PGPASSWORD`（hivemind 曾因缺这个，夜间 base backup 静默失败数月，
    桶里 1000+ WAL 零 base backup = 不可恢复）；
  - **修正 3**：每次启动后台检查 `wal-g backup-list`，无 base backup 立即补推。
- **备份**：每日 03:00 UTC `backup-push.sh`（`wal-g backup-push` +
  `delete retain FULL 7`）；R2 新 bucket（如 `feedling-pg-backups`，
  与现有 io-user-logs 凭证分开）。
- **监控（没有监控 = 没有备份）**：外部 cron/CI 两项检查——
  `pg_stat_archiver.last_archived_time` 超 1h 告警（直连 SQL 查询即可，
  我们有原生 wire，不需要 hivemind 的 proxy）；R2 bucket 检查
  `wal_005/` 最新对象 <1h、`basebackups_005/` 非空且最新 sentinel <26h。
  每月 restore 演练（fetch LATEST 进 scratch 容器 + `SELECT count(*)` 已知表）。
- **部署**：独立 AppAuth 合约；CI 排除在 merge 自动部署外
  （只手动 `target=postgres`）；`deploy/DEPLOYMENTS.md` 增补 runbook
  （含 restore.sh 灾难恢复流程）。

## 4. 明文 schema 设计

原则（参考文档 Phase 0）：**这是唯一一次把类型、索引、约束做对的机会，
不要照抄密文 schema。**

- 13 张本就明文的表（`users`、`user_blobs`、`user_logs`、`global_blobs`、
  `server_config`、`perception_daily`、`genesis_import_jobs/_outputs`、
  `copytext_*`、`agent_runtime_*`、心跳表）：DDL 原样复制。
- 6 类密文表改明文列：
  - `chat_messages` / `memory_moments` / `world_book_entries`：doc JSONB 里的
    信封字段（`body_ct`/`nonce`/`K_user`/`K_enclave`/`enclave_pk_fpr`）换成
    明文 `body`（text 或 jsonb）；保留 `visibility` 列（产品语义上
    「agent 是否可见」仍存在，只是不再由密码学强制）。顺手加过去做不了的
    索引（如 body 的全文/trigram 索引，按需）。
  - `frame_envelopes` → `frames`：行内存明文 meta；body 按 D4 存 R2
    （存储层加密）+ 行内存 R2 key 与存储钥版本。
  - `perception_items`：photo 信封部分同 frames 处理。
  - `genesis_import_chunks.encrypted_body BYTEA` → `body TEXT/JSONB`。
- **迁移框架**：TEE 库用独立的 Alembic 迁移链（新目录
  `backend/alembic_tee/`，独立 version table），因为两库 schema 在过渡期
  分叉；终态 RDS 链归档删除，TEE 链成为唯一权威。
- 序列/约束：沿用 `0012` 的 per-user `ON DELETE CASCADE`；回填后
  `setval` 校正 SERIAL（参考文档已知坑）。

## 5. 双写与复制（过渡期数据流）

**核心约束：backend 解不了密（只有 enclave 能解 shared，只有设备能解
local_only），且 enclave 单线程是已知 502 瓶颈——热路径绝不同步 decrypt。**

三条数据流，分别处理：

1. **明文表（13 张）：db.py 同步双写。**
   db.py 增加第二个连接池（`TEE_DATABASE_URL`），写 helper 内 fan-out：
   RDS 为主（失败语义照旧），TEE 侧失败只记日志 + 计数器（双写期 TEE 是影子库，
   不能影响主路径）。开关 `FEEDLING_TEE_DUAL_WRITE=1`。
   业务层 35 个模块零改动。

2. **密文表（6 类）：异步「解密复制」worker。**
   - 新模块 `backend/tee_replicator/`：按表扫 RDS（`updated_at`/id 水位游标，
     游标存 TEE 库），批量调 enclave decrypt（复用
     `/v1/content/rewrap-to-current-key` 已走通的「enclave 解密→重落库」编排，
     `backend/content/routes.py:341`），限速（可配 QPS，避免压垮 enclave），
     幂等 upsert 进 TEE 明文表，断点续传。
   - 同一机制覆盖**存量回填**和**过渡期旧客户端增量**（增量 = 水位之后的新行）。
   - `local_only` 行解不了：标记 `pending_device_migration`，等 D1 的
     iOS 重传流程；到停 RDS 的 gate 时还没重传的按 D1 决议处理。
   - frame body：从 R2 拉密文 → enclave 解 → 存储钥重加密 → 写 R2 新前缀，
     行内更新指针。
   - **执行防护**（参考文档 Phase 3）：手动触发的 CI workflow，默认
     `dry_run=true`（只出 plan + schema 校验），真跑需字面 `confirm: MIGRATE`；
     目标表非空需显式 `--truncate-dst`；与部署用不同 concurrency group。

3. **切读后的新写入：客户端发明文，backend 反向双写。**
   iOS 明文版发布后：明文同步写 TEE pg（成为主库），同时用现成的
   `build_envelope`（`backend/content_encryption.py:91`，加密只需公钥，
   backend 都有——rewrap 的加密侧就是这么干的）加密写回 RDS，维持回滚能力，
   直到停 RDS。开关 `FEEDLING_TEE_PRIMARY=1`。

**一致性验证 job**（切读 gate 的硬条件）：per-table per-user 行数对比 +
按比例抽样（enclave 解 RDS 行与 TEE 行逐字段比对）；差异输出报告。

## 6. iOS 侧改动（独立仓 feedling-mcp-ios，与 Phase 5 同步）

- 上传/下载走明文（TLS 终止在 TEE 内的现有通道不变）。
- 删除 `ContentEncryption.swift` 信封构造/解析、Keychain `content_sk`
  的内容层用途（注意：如果该钥还用于别的身份用途需先确认）。
- `local_only` 存量重传：登录后检测服务端 `pending_device_migration` 清单，
  本地能解的解密重传明文，一次性流程。
- 「decrypt failed」类僵尸数据（设备钥漂移救不回的）在此终局：服务端
  rewrap 能救的走复制 worker 顺带救，救不回的随 RDS 退役丢弃。

## 7. 分阶段实施计划

每阶段有明确验收标准；test 环境全流程（Phase 0–7）走完并 soak 后，Phase 8 在 prod 复刻。

**Phase 0 — 网络 spike（半天～1 天）**
临时起一个最小 pg CVM，验证：gateway `-5432s` 透传 + PG17 direct TLS 下
psycopg3 连接、简单读写、**LISTEN/NOTIFY 长连接存活/重连**、RTT。
失败则落 stunnel sidecar 方案再测。
✅ 产出：可用的 DATABASE_URL 形态 + 连接参数写进设计文档。

**Phase 1 — pg CVM 基建（test）（2–3 天）**
按 §3 建 `feedling-pg-test`：镜像/compose/entrypoint（四修正全数落实）、
独立 AppAuth、R2 backup bucket、监控两项、部署脚本 + DEPLOYMENTS.md runbook。
✅ 验收 = 参考文档 §7 检查单：`SHOW archive_mode`=on（活服务器上）、
bucket 有 base backup、桶内对象确认加密、归档告警可触发、
**restore 演练端到端成功一次**、app 部署不重启 pg CVM。

**Phase 2 — 明文 schema + 双写基建（2–3 天）**
`backend/alembic_tee/` 迁移链 + §4 明文 schema；db.py 第二池 +
明文表同步双写（`FEEDLING_TEE_DUAL_WRITE`）；双写失败计数器/日志。
⚠️ 与 ASGI 迁移 worktree（backend-asgi-migration，动 db.py 周边）协调：
本阶段改动集中在 db.py 与新模块，二者需商定合并顺序（建议 ASGI 先合或
本工作基于其分支）。
✅ 验收：test 环境双写开启，明文表两库行数持平；关掉开关系统行为不变。

**Phase 3 — 解密复制 worker（3–5 天）**
§5.2 的 `tee_replicator` + 防护 workflow + 一致性验证 job。
在 test 环境完成存量回填 + 增量追平。
✅ 验收：验证 job 全绿（行数 + 抽样比对）；worker 可随时中断重启不丢不重；
enclave 负载可控（无 502 回归）。

**Phase 4 — 明文写入路径 + iOS 改造（并行，3–5 天后端 + iOS 独立排期）**
backend 接受明文写（内容 API 的 v2 形态：明文 body + visibility），
实现 §5.3 反向双写；iOS 按 §6 改造 + local_only 重传流程。
旧信封写路径此阶段仍正常工作（走 §5.2 增量复制）。
✅ 验收：test 环境新旧客户端并存均正常；明文写的行两库一致。

**Phase 5 — 切读 + 强更（1 天 + 观察窗口）**
同步发布：backend `FEEDLING_TEE_PRIMARY=1`（读写主库切 TEE pg，
反向双写 RDS 保持同步），iOS 明文版强更，旧版信封写返回 400/426 提示升级。
回滚 = 关开关（RDS 一直被双写，数据不落后）。
✅ 验收：全功能回归（chat/memory/perception/proactive/genesis/托管 agent）、
wake bus 跨 worker 唤醒正常、无 P95 恶化。

**Phase 6 — 停 RDS（gate 制，非时间制）**
Gate 全绿才动：≥1 次 base backup 成功 **且** ≥1 次 restore 演练成功、
归档监控在线、一致性验证通过、切读后 soak ≥7 天无回滚、
local_only 重传收尾（按 D1 处理残余）。
然后：关反向双写 → RDS final snapshot → 只读保留 2–4 周 → 终止实例
（test 和 prod 分别走）。
✅ 在 restore 演练成功前，pg CVM 磁盘是数据唯一副本——这段窗口越短越好
（Phase 1 已把备份前置，实际窗口≈0）。

**Phase 7 — 拆加解密层（2–3 天）**
- backend：删 `content_encryption.py`、`core/envelope.py`、enclave decrypt
  代理与路由、rewrap 端点；`/v1/content/swap` 退化为纯 visibility 字段更新；
  `tee_replicator` 归档。
- enclave：content_sk 派生与 decrypt 路由下线（enclave/KMS 身份保留，
  R2 存储钥派生迁入或保留在 enclave，按 D4）。
- iOS：删加解密代码。
- 文档：`docs/DESIGN_E2E.md`、`CONTENT_ENCRYPTION_INTERACTION_CURRENT.md`
  改写为 TEE 信任模型说明；`deploy/DEPLOYMENTS.md`、`CONTRIBUTING.md`、
  io-onboarding 三件套涉及加密表述的同步更新。
✅ 验收：全量测试基线绿；grep 无信封字段残留（`body_ct`/`K_user`/
`K_enclave`）除历史迁移归档。

**Phase 8 — prod 复刻**
`feedling-pg-prod` 走 Phase 1→6（代码已就绪，主要是基建 + 回填 + 切换 +
gate）。prod 回填量：主要是 frames 与 chat（参考 hivemind 实测吞吐
~750–820 rows/s 过 gateway 定超时）。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| gateway 透传跑不通 PG wire | Phase 0 前置 spike；stunnel sidecar 兜底；再不行才考虑 HTTP proxy 重构（代价另评） |
| LISTEN/NOTIFY 长连接被 gateway 掐 | spike 必测项；补 keepalive + 自动重连 |
| enclave 被复制 worker 压垮（历史 502） | 限速可配 + 只在低峰跑 + 随时可停（断点续传） |
| pg CVM 磁盘 = 唯一副本窗口 | 备份基建先于数据进入（Phase 1 验收含 restore 演练） |
| 备份静默失败（hivemind 全部四条事故） | 四修正 + fail-closed + 双监控 + 月度演练，一条不少 |
| 共享 AppAuth 换钥事故重演 | pg CVM 独立 AppAuth，写进 runbook |
| 与 ASGI 迁移冲突（都动 db.py 周边） | Phase 2 前商定合并顺序 |
| local_only 用户不重传 | D1 决议 + 停 RDS gate 里显式清点残余 |
| 明文库磁盘容量 | 建 CVM 时按 RDS 当前体积 ×（明文膨胀系数~1，密文≈明文长度）× 增长余量估；frames 走 R2 不占盘 |

## 9. 明确不做

- RDS→TEE 的逻辑复制/pg_dump 直搬（数据是密文，搬了也没用，必须过 enclave 解密）。
- 多租户 sql-proxy、admin HTTP 面（hivemind 需要，我们有原生 wire + Alembic，不需要）。
- 双主/双读（TEE 库切主前永远只是影子库，读永远单源，避免一致性泥潭）。
