# Genesis 蒸馏全景(当前 test)

> 目的:把 onboarding 和二次蒸馏(上传文件让 IO 吸收)当前**真实代码流程**理清,cloud + vps 全覆盖,
> 含分块、记忆引擎、身份两条路,配具体例子。函数名均对应 `backend/genesis/*` 当前 test 代码。
> 供改动(尤其 Seven 的 memory 分流校准)对着看,免得靠记忆画错。
>
> 最后更新:2026-07-08。B2(关系时间覆盖)已上线;A(记忆按来源分流)为待做提案,见文末 §9。

---

## 0 · 总入口 & 路由

```
所有上传 → POST /v1/genesis/imports/plaintext → plaintext_import
                              │  _is_sealed_body(payload)?
              ┌───────────────┴────────────────┐
           否(明文)                          是(sealed_v1)
              │                                 │
        run_plaintext_job                 _resident_sealed_import
        (按 mode 分派)                     存密文 + 建 awaiting_resident job
        mode ∈ {onboarding,               → 【走 §6 二次·VPS】
                add_memory,
                update_identity}
```

- **明文 = cloud/hosted**(服务端蒸馏);**sealed = 自托管 VPS**(本地蒸馏)。按 body 类型路由,无全局开关。
- 三个 mode:`onboarding`(首次)/ `add_memory`(长期记忆·聊天记录)/ `update_identity`(人设)。

---

## 1 · 分块机制(所有 cloud 路的共用前置)

上传文本先切"窗口"再喂模型(`_plaintext_source_groups` → `_plaintext_chunk_texts_for_messages` → `history_import._build_transcript_windows`):

- **一窗 ≈ 18000 字**(`max_chars=18000`),相邻窗**重叠 8 行**(`overlap_lines=8`,防跨窗事实被切断)。
- **窗口总数上限 = tier 定**(`total_windows`,按上传体量 small/medium/large,默认 8);超了 `_select_evenly` **均匀采样**降到上限(大导入会丢一部分窗)。
- **聊天记录 vs support(档案)分开切**:support 走 `_support_source_windows`,历史走行式窗口。
- **onboarding 前台再砍一刀**:`_cap_foreground_history_chunks` 把历史块 cap 到 **8**(`FEEDLING_GENESIS_FG_HISTORY_CAP`)求快,其余留给后台。

fact_map **逐窗**跑。

---

## 2 · 记忆引擎(fact_map → fact_write)—— cloud 的共用核心

```
fact_map   prompt = FACT_MAP_PROMPT
   逐窗抽"值得长期留存的候选事实"(闲聊/一次性/未确认 不抽) ← 【精选口径在这】
        ↓ all_fact_candidates
fact_write prompt = FACT_WRITE_PROMPT
   候选→落卡;known_memories 去重;归 bucket/threads ← 【去重&归类口径在这】
```

- 载体函数:`build_foreground_output_from_texts`(前台 fact_map)、`build_reducer_output_from_texts`(全量:fact_map+fact_write+voice+persona)、`build_memory_output_from_fact_candidates`(单独 fact_write)。
- **"少而精"全在这两个 prompt 的判断里,没有"3-5 张上限"。**

---

## 3 · Onboarding · Cloud

首次上传 = 历史 + 人设卡 + 个人档案 + support material,`mode=onboarding`。

### 3a · v2 前台快进(`_run_plaintext_genesis_v2`,`FEEDLING_GENESIS_V2_ENABLED` 默认开)

```
前台(先能聊):
  _cap_foreground_history_chunks → 历史【采样 8 窗】(support/persona 不采样)
  逐组 build_foreground_output_from_texts(write_core=False) → fact_map【引擎§2】
  汇总 all_fact_candidates
  core = select_core_for_foreground(...)   ← 只当"有没有可聊内容"的门槛
  build_memory_output_from_fact_candidates(all_fact_candidates) → fact_write【引擎§2】
        → 【全量落卡,不是只 3-5】
  身份 = foreground_identity.derive_foreground_identity(...)   ← 复用现成 deriver,init 路
  apply_memory_outputs(全量) + _store_identity_payload(写卡+关系锚点) + 问候
        → complete(前台就绪,App 进门就有名字+锚点)

后台 _run_plaintext_background_enrichment:
  剩余【未采样】窗 → build_reducer_output_from_texts(其余记忆 + voice + persona)
  skip 前台已写的;【不重写身份】;后台失败不影响已能聊的 onboarding
```

### 3b · v1 兜底(v2 无 core 时)

```
逐组 build_reducer_output_from_texts 全量跑一遍(fact_map + fact_write + voice + persona)
```

### 3c · 身份 & 关系锚点

- 走 **init**:`_store_identity_payload`(写身份卡 + 设 `relationship_started_at`;用户填了日期就用,否则从最早记忆推)。
- 锚点算法:`_plaintext_relationship_anchor(payload)`。

> **例子 C(超大导入)**:50 万字 ≈ 28 窗 → 前台采样 8 窗 → 全量落卡 + 建身份 + 问候 → 立刻能聊;后台把剩 20 窗补齐(其余记忆 + 声音 + 人格)。

---

## 4 · Onboarding · VPS

```
resident agent 按 skill(★不进 genesis 管道):
  feedling_identity_init(身份先行,锚点 = 当场 days_with_user + evidence)
  记忆:garden 自然生长,0 卡合法,不回扫、不批量蒸馏
```

- **完全不碰记忆引擎§2**,也没有前台/后台三段式。

---

## 5 · 二次蒸馏 · Cloud

三入口 → 两条 mode 路:

```
人设     → mode=update_identity
长期记忆 → mode=add_memory,source_family=memory_summary
聊天记录 → mode=add_memory,source_family=history
```

### 5a · add_memory(长期记忆 / 聊天记录)

```
_run_plaintext_add_memory_job
  逐 source_group:
    build_foreground_output_from_texts → fact_map【引擎§2】
    build_memory_output_from_fact_candidates → fact_write【引擎§2】→ 全量落卡
  → apply_memory_outputs
  （★单次:无身份、无后台、无前台采样,该组所有窗一次跑完)
```

> **例子 A(聊天记录)**:9 万字 → 5 窗 → fact_map 逐窗抽 11 候选 → fact_write 去重 → 落卡 7 张。✅ 对。
>
> **例子 B(长期记忆)**:手写 3000 字/20 条事实 → 1 窗 → fact_map 按**精选口径**判掉大半,只留 8 候选 → 落卡 6 张。
> ❌ 用户特意整理的 20 条被丢成 6 条 —— **这就是 A(§9)要修的"一刀切误伤长期记忆"。**

### 5b · update_identity(人设)

```
_run_plaintext_update_identity_job
  _derive_identity_with_provider(从上传人设材料派生身份)
  build_persona_output_from_material(重建常驻人格)
  → replace_identity_preserving_anchor
       └─(B2 已上线)上传带明确关系时间 → 覆盖锚点,否则保留
```

---

## 6 · 二次蒸馏 · VPS(已改为复用 cloud 引擎 —— 分支 `feat/vps-reuse-cloud-distill`,待真机 e2e)

```
iOS Material Sheets → sealForCurrentUser 加密 → POST …/plaintext (format=sealed_v1)
        │
  后端存密文 + awaiting_resident job
        │
  你 VPS consumer:poll /v1/genesis/resident/pending
        │  enclave /v1/envelope/decrypt 解密 → 整份明文
        ▼
  add_memory:_window_document(18000字/8行重叠,同 cloud 窗口)
             逐窗 build_foreground_output_from_texts → fact_map【★引擎§2 原函数】
             build_memory_output_from_fact_candidates → fact_write【★引擎§2 原函数】
             → memory.add(consumer 客户端封信封)
  update_identity:_resident_derive_identity(单次 agent,不切块)→ identity.replace
```

- **★关键:VPS 现在跟 cloud 用同一套 §2 引擎、同一批 prompt。** 机制 = `GenesisLLMClient(completion_fn=call_agent适配器, persist_output=False)`:
  把模型换成本地 agent、关掉 DB 缓存,cloud 的 `build_foreground_output_from_texts` / `build_memory_output_from_fact_candidates`
  在 consumer 上**原样跑**。只有三处不同:**上传路径(sealed)、模型(本地 agent)、写入(memory.add 客户端封)**。
- 身份 replace 跟 §5b **共用** `replace_identity_preserving_anchor`。
- 后端配合改动:`GenesisLLMClient` 加 `persist_output`(默认 True=cloud 不变)。
- **待办**:① known_memories 去重未接(fact_write 暂不跨已有卡去重)② 512KiB 上传上限可去掉(切块后不需要,iOS+后端各一处)③ iOS `combinedGenesisMaterial` 目前糊成一份、只带 mode;要接 A(按类型 keep_all)需带 `material_kind` ④ **本地 agent 的 JSON 可靠性 + 串行速度,必须真机 e2e**。

---

## 7 · 身份两条路(别混)

| | 走哪 | 关系锚点 |
|---|---|---|
| **首次(onboarding)** | `init`(`_store_identity_payload` / `derive_foreground_identity`) | 首次直接设 |
| **二次(update_identity / vps replace)** | `replace_identity_preserving_anchor` | 默认保留;**B2:带明确时间才覆盖** |

---

## 8 · 共用点总表

| 组件(真名) | onbd-cloud | 二次-cloud | 二次-vps | onbd-vps |
|---|:-:|:-:|:-:|:-:|
| 分块 `_build_transcript_windows` | ✅ | ✅ | ✗(整份) | ✗ |
| `fact_map` + `FACT_MAP_PROMPT` | ✅ | ✅ | ✗ | ✗ |
| `fact_write` + `FACT_WRITE_PROMPT` | ✅ | ✅ | ✗ | ✗ |
| `build_memory_output_from_fact_candidates` | ✅ | ✅(add_memory) | ✗ | ✗ |
| voice/persona 构建 | ✅ | ✗(add_memory 无) | ✗ | ✗ |
| 身份 `init` | ✅ | ✗ | ✗ | ✅(skill) |
| `replace_identity_preserving_anchor` | ✗ | ✅(update_id) | ✅ | ✗ |
| VPS `_build_distill_prompt` | ✗ | ✗ | ✅ | ✗ |
| 前台采样 + 后台补齐 | ✅ | ✗ | ✗ | ✗ |

---

## 9 · 待做提案:A · 记忆按来源分流(Seven 校准第 2 点)

> 现状问题见 §5a 例子 B:长期记忆走跟聊天记录**同一套精选口径**,把用户整理好的事实丢了。

- **目标**:`memory_summary`(长期记忆)→ 尽量收;`history`(聊天记录)→ 保持精选。
- **cloud 隔离要点**:onbd(§3)和二次(§5a)**共用引擎§2**。所以不能直接改 `FACT_MAP_PROMPT`(会漏进 onboarding),
  **必须用只在 `add_memory` + `memory_summary` 组打开的 `keep_all` 开关**,选一套"尽量收"变体 prompt。
  触点:`prompts.py`(变体 prompt,措辞归 Seven)、`worker.py`(串 `keep_all`)、`plaintext.py:_run_plaintext_add_memory_job`(按 family 打开)。
- **vps 触点**:§6 现已复用引擎§2 → **A 天然继承,不用再写第二份 prompt**(cloud 给 memory_summary 加 `keep_all`,VPS 跟着有)。只需 iOS sealed body 带 `material_kind` 让 consumer 知道该组是 memory_summary 还是 history。
- **门槛**:改抽取 prompt 的行为**单测抓不到,必须真机 e2e**;prompt 措辞归 Seven;onboarding 是否也纳入"尽量收"(其 support material 也是档案)为单独产品决定。
- **B2 已完成**:上传带明确关系时间才覆盖锚点(否则保留),cloud+vps 共用 `replace_identity_preserving_anchor` 一处改双端一致。

### 待做提案:onboarding 同步(这版不做,之后统一优化)

> Seven 这次的校准(记忆分流 / identity 覆盖)**当前只落二次蒸馏**;onboarding 侧**没同步**。已知缺口 + 记下,之后统一优化:
- onboarding 的 support material(个人档案)也是"用户整理好的事实",按同样逻辑也该"尽量收"——但 onbd 和二次共用引擎§2,要动得靠 `keep_all` 的 mode 隔离扩展到 onboarding,或单独决定。
- **这版顺序**:先把二次蒸馏(VPS 复用 cloud 引擎 + cloud 的 A `keep_all`)做好、e2e;**之后再统一优化 onboarding**(hx 定)。
