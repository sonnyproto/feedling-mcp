# VPS 侧二次蒸馏改由 resident agent 消费 — 设计方案 v3 (定稿, 2026-07-07)

> v3 收敛:客户端加密 + 不做应用层切块 + 本地 agent 蒸馏;endpoint 复用 URL 但 body 用**显式 sealed
> schema + 双向硬校验**;memory=轻捕获+Dream、identity=本地派生后**整卡 replace(对齐 cloud)**、经**新
> `identity.replace` server-build action**(agent 只发明文、不碰加密);大小上限=**仅 resident、可配置字节
> 数**;claim=genesis 自己的 SQL 原子锁 + heartbeat + 更长 lease;app 只见 processing/done/failed;
> privacy copy 分模式;cloud worker 路径**一字不改**。

## 问题

iOS 三处二次蒸馏(Garden 补记忆 / Identity 补卡 / onboarding,均调 `uploadGenesisPlaintext`)把材料当
**明文一次性 POST** 到 `/v1/genesis/imports/plaintext`,由**服务端 genesis worker 用服务端 model_api 的
LLM** 蒸馏,**无 self-hosted 分支**。

对 VPS 自托管用户是错的:他们有**自己的本地 agent(本身是 LLM)**。聊天走本地 agent,但补卡绕过它跑服务端
LLM。**且现状下 VPS 的 identity/memory 上传是坏的**——多数自托管用户没在服务端配 model_api → 直接失败;
即便配了也是用错的 LLM + 对 identity 做盲目 replace。

## 目标

self-hosted 时,二次蒸馏的**蒸馏步交给本地 resident agent**,材料**端到端加密**(服务器只存密文、永不见
明文),**行为对齐 cloud、cloud 流程零改动**。

## 现状(review 可对照)

- 三处补卡都走 `uploadGenesisPlaintext`:整份材料**明文**、**单次 POST**、**无客户端加密、无切块**
  (`FeedlingAPI.swift:3089`)。服务端 daemon 线程内存蒸馏、原文不落盘(`genesis/service.py:57`)。
- cloud 两条语义:**memory** = 抽事实→写卡(support 材料**全读不采样**,只有 chat history 会 `_select_evenly`
  降采样);**identity** = LLM 重派生整份身份 → `replace_identity_preserving_anchor`(**整卡替换**,保留相处天数)。
- iOS **已有客户端封信封能力**(未启用的分块路 `uploadGenesisImport`/`genesisPutChunk(envelope)`,
  `sealForCurrentUser` 可封任意 Data)。→ 复用它封"一坨材料"(非分块)。
- resident consumer 轮询 `/v1/chat/poll`;持 `FEEDLING_ENCLAVE_URL` 可**解密** chat/memory/identity。
- 身份写:`/v1/identity/actions` 的 `profile_patch`/`dimension_nudge` 服务端建信封、**无客户端 crypto**
  (`identity/actions.py:75`);`/v1/identity/replace` 才要客户端信封(`identity_core.py:186`)。
- Dream 是真机制:`memory/dream_prompt_v1.py` —— 夜间**纯整理已有记忆卡**(合并/厚化/消矛盾),不碰 identity。

## 设计

### 0. 贯穿全局的加密不变量

> **Resident agent/consumer 只做两件加密相关的事:①解密(为了读材料)②写的时候发明文、由服务器建信封。
> 它自己永远不建信封。** 与 `memory.add`、identity `actions` 现有做法一致。

### 1. 部署门控 + 双向硬校验

`FEEDLING_GENESIS_DISTILL_MODE = worker | resident`,**默认 `worker`**。
- `worker`(cloud)= 现状一字不改(`plaintext_import → start_job` 原样)。
- `resident`(self-hosted)= 密文材料 + 本地 agent 认领消费。
- **双向 per-request 硬断言**(这是本版最大的坑,Codex P1):worker 部署**拒收 sealed body**;resident 部署
  **拒收 plaintext distill**。防误配把密文当明文塞进 cloud worker。

### 2. Endpoint & schema —— 复用 URL,但 body 不歧义

复用 `/v1/genesis/imports/plaintext` URL 缩小 iOS 面,但**请求体加显式 schema tag**(如 `format: "sealed_v1"`
/ `sealed_envelope` 字段),两种 body **schema 明确分开**。worker 只认旧 plaintext body、resident 只认 sealed
body(承接 §1 的双向硬校验)。**不要**让同一 body"有时明文有时密文还看不出来"。

### 3. 上传:客户端加密 + 不做应用层切块

- 手机把材料**封成 enclave 可解的 shared envelope(密文)**,单次上传;**不做应用层切块**——大小由 agent 自己
  Read 分段处理(见 §5 memory / §6 identity)。
- envelope 必须**绑定 job_id / owner_user_id / AAD**(防张冠李戴),且**含 resident 可解路径**(不是只客户端本地可解)。
- HTTP/传输层拆包(TCP / `Transfer-Encoding: chunked`)是透明的、不设计。应用层切块(续传/绕网关体积限)是**延后
  增强**,M1 不做;靠 §7 的大小上限把 body 压在安全单发范围内。

### 4. 端到端流程(两个入口共用后半段)

```
【入口 A:App 补卡】
  手机:封密文(不切块)→ 传 → 服务器只存密文 + 建 job(永不见明文)
【入口 B:聊天里把文件丢给 agent】
  文件已在 agent 本地,直接进下面(resident skill 覆盖)
  ↓ 共用后半段
  consumer 认领 job → 解密材料到本地临时文件(入口 A);或直接读本地文件(入口 B)
  → 本地 agent 用自己的 LLM,照 skill 判断内容:
       • 事实/记忆  → §5
       • 人物卡/人设 → §6
  → consumer 标 job done → 删服务端密文 + 删本地临时文件
  App 轮询 job → done(UX 不变)
```

### 5. memory md —— 轻捕获 + Dream 整理(不加新工具)

- 上传时 agent **只做轻捕获**:把值得记的**快速落成卡**(克制、少比对甚至不比对),用现有 **`memory.add`**
  (明文进、服务端加密,`memory/actions.py:315`)。**不加新工具。**
- **深度整理(合并/去重/消矛盾)交给 Dream**(夜间纯整理,本来就干这个)。
- 差别只是**行为**(全量文件时快落卡、别在写时逐条死磕),写进 skill,不是新契约。
- **⚠️ 绕开本地建封 capture lane(Codex P2)**:resident consumer 现有 capture lane 有 `_capture_build_envelope`
  会**本地建 memory envelope**(`chat_resident_consumer.py:5280`)。这条新 resident distill path 必须**明确绕开它**,
  **只产 `memory.add` 明文 action**(承接 §0 加密不变量:agent/consumer 不建信封)。

### 6. identity md —— 本地派生 + 整卡 replace(对齐 cloud)

- 上传时**本地 agent 用自己的 LLM 从 md 派生整份身份(7 维度)**——这步在本地(用对 LLM),不再用服务端 model_api。
- 结果做**整卡 replace(保留相处天数)**,对齐 cloud 语义(上传 identity 是明确的主动动作:要么设成这个、要么旧的
  坏了要重来 → replace 是对的)。
- 写回经**新增的 `identity.replace` server-build action**:agent **发明文整份身份**,**服务端建信封 + 替换 +
  保留 anchor**(实现成 `_identity_replace_action`,复用/抽取 `replace_identity_preserving_anchor` 的清洗 /
  非空校验 / 保留 anchor / 建信封逻辑;收明文 + runtime token,跟 `profile_patch`/`nudge` 一个模式,补进 action
  supported list + tests)。→ **agent 不碰加密**。
- **⚠️ 高危门控(Codex P1,必须做)**:这是**整卡覆盖**级写操作,**不能变成普通聊天 agent 随手可调的常规 action**。
  action 必须带 `source: "genesis_resident_distill"` / `job_id` / `reason`,**后端校验该 job 属于当前用户且处于
  resident `claimed/running` 状态**才放行。否则未来模型一次误判就能整卡重写身份。
- **⚠️ envelope 边界(Codex P1)**:这是 **server-build 新 action**,**不复用**旧 `/v1/identity/replace` 的客户端
  envelope 合约(`identity_core.py:186`)。测试里钉死:agent payload **不允许带 envelope**、也不要求 agent 有
  user/enclave 公钥;服务端走 `core_envelope._build_shared_envelope_for_store` 建封。
- 之后 **6h 复盘**继续基于对话微调(replace 定基线,复盘往前演化)。**注意 Dream 不碰 identity**,所以身份的整理
  必须在**本次 replace 就完成**,不能指望后台补。

### 7. 大小上限 —— 仅 resident,可配置字节数

**cloud 不加、行为不变**(服务端有 `_select_evenly` 降采样兜底);**resident 加**(本地 agent 无降采样兜底,
且几 MB≈上百万 token 超上下文)。
- 配置项 `FEEDLING_RESIDENT_DISTILL_MAX_BYTES`,**默认 512KiB–1MiB**,真机/VPS 测完再定死。
- 按 **UTF-8 明文字节 + envelope/base64/JSON 膨胀**计算(密文比原文大)。
- **两处拦,都只在 self-hosted/resident 生效**:客户端(iOS 按 storageMode 上传前拦)+ 服务端(resident 分支硬拦)。
- 超限:明确报错"文件太大,拆分后再传",不静默截断。
- 网关请求体上限是现存共享老限制,cloud 也受它管——**本次不动**。

### 8. claim / lease —— genesis 自己的 SQL 原子锁

- **不只靠 status**:参考 proactive(`only_if_status="pending"` 原子 patch + stale reclaim,
  `proactive_core.py:384`/`poll_core.py:45`)与 genesis 侧 `FOR UPDATE SKIP LOCKED`(`db.py:1215`),genesis
  **自己实现 SQL 原子 claim**。
- job 字段:`consumer_id` / `claimed_at` / `heartbeat_at` / `attempt_count` / `max_attempts`。
- **lease 要够长 + heartbeat**:本地 agent 读大材料 + 分段 + 写身份可能 >10min → 独立 lease(如 30min)+ 定期
  heartbeat 续租;`attempt_count ≥ max_attempts` → terminal `failed`。否则"agent 中途死"会永远卡。
- **DB 内部细分**(claimed/running/lease)与**给 app 的 status 分开**(见 §9)。

### 9. app-facing status —— 只暴露 processing/done/failed

- resident 的 claim/running/lease 细节**只在 DB 内部**;给 app 的 `job.status` **只有 processing/done/failed**,
  细分走 `output.stage`(套现有 public stage:`chat_history_importing`/`background_importing`/`completed`,
  `service.py:34`)。**不要把 claimed/running/lease_expired 泄给 app**(Garden 有 active-status 白名单,认不出会卡)。

### 10. privacy copy —— 分模式

`genesis/service.py:57` 现文案(不存 imported plaintext、发给用户配置的 LLM)**按 mode 分**:
- worker:保持原意。
- resident:改成"服务器**短暂持久化的是密文**,明文只在 resident/enclave 解密后进入本地 agent,job 终态
  (done/failed)后删除材料"。

## 不变的东西(review 重点)

- **cloud/hosted genesis:完全不动**(worker 模式,`plaintext_import → start_job` 原样;`_select_evenly` 降采样
  照旧;identity 服务端 replace 照旧)。**cloud 不加任何逻辑大小上限。**
- iOS 改动**仅限 self-hosted 分支**:补卡上传"明文单发 → 本地封密文单发 + 上传前大小检查";`job.status`/进度 UI
  继续只见 processing/done/failed。
- 记忆写 `memory.add`、身份写 `/v1/identity/actions`(含新 `identity.replace`)——统一"明文进、服务端建信封"。

## 新增/改动契约汇总

- 部署 flag `FEEDLING_GENESIS_DISTILL_MODE` + 双向 per-request 硬断言。
- 上传 body:显式 `sealed_v1` schema;resident 模式存**密文**材料(keyed by job_id)、终态后删。
- 新 client action:**`identity.replace`**(server-build,收明文整份身份 + runtime token → 建信封 + replace +
  保留 anchor)。**高危门控**:必带 `source=genesis_resident_distill`/`job_id`/`reason`,后端校验 job 属当前用户
  且 resident `claimed/running`;payload **禁带 envelope**。
- resident-facing:`GET /pending`(或扩展 poll)、`POST /{id}/claim`、`POST /{id}/complete`;job 加
  `consumer_id/claimed_at/heartbeat_at/attempt_count/max_attempts`。
- 大小上限 `FEEDLING_RESIDENT_DISTILL_MAX_BYTES`(仅 resident:客户端 + 服务端)。
- consumer plumbing(Python):认领 → fetch → enclave 解密 → 本地临时文件 → 唤 agent("吸收 <path>")→ complete
  → 清理。**claim/fetch 在 consumer,不需给 agent 新 io_cli verb。**
- skill(entry B)同步:identity=本地派生后 replace(经 `identity.replace`);memory=轻捕获(Dream 整理)。

## 实现顺序(Codex 建议,采纳)

1. **先定** sealed schema + 门控(双向硬校验)——这是安全边界,先钉死。
2. **再做** resident claim/lease(原子锁 + heartbeat + attempt cap)。
3. **再** identity.replace server-build action + memory 轻捕获行为。
4. **最后** 真 VPS 部署 crypto e2e。

## 待确认 / 开放

1. `identity.replace` action 是并进 `/v1/identity/actions` 还是独立端点(倾向并进,复用 runtime-token 鉴权)。
2. resident 认领走独立 `GET /pending` 还是并进 chat poll。
3. 大小上限默认值(512KiB vs 1MiB),真机定。
4. sealed envelope 的具体 AAD 绑定(job_id|owner|v),复用现有 envelope 构造。

## 红线

碰加密信封(客户端封 + consumer 解 + 新 identity.replace 服务端建封),**最后必须真 VPS 部署 e2e**;本地
fake-decrypt 只验流程不验 AEAD/enclave 真解密。

## 归属

跨仓多 owner:iOS(补卡上传改本地加密 + 大小检查)、backend(DISTILL_MODE 分流 / sealed schema / 密文存储 /
claim 端点 / `identity.replace` action)、resident consumer(认领→解密→唤 agent→complete plumbing)、
io-onboarding skill(entry B 对齐)。
