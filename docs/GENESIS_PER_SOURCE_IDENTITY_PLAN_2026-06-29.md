# Genesis 一次性流程 — per-source 身份/voice/时间 完整修复方案

> 2026-06-29. Seven 拍板。Driven by CC(prompt/agent_runtime/iOS/审计/E2E)+ Codex(backend)。
> 触发:真机 e2e 跑通了 genesis(42 记忆 + persona + voice),但**身份卡整个是空的**
> —— 没名字、性格"—"、0 天、没自我介绍、进 app 没主动问候。

## 背景 / 现状
我们把 onboarding 上传从「客户端分块加密」退回「一次性明文进 TEE」(见
`docs/CHANGELOG.md` 2026-06-27/29)。退回时新建的 plaintext 端点
(`backend/genesis/routes.py`)把 **4 份材料(角色卡 / 个人档案 / 长期记忆 / 聊天记录)
全塞进一个 job**,`source_kind` 只取一个值。这压扁了原本「每份材料一个 job、各走各
source_family 分支」的 per-source 机制 → 身份这一层没被正确塑造。

## 四份材料 → 三个落点(确认无误)
| 材料 | 后端 lane(source_family) | 落点 |
|---|---|---|
| AI 角色卡 | `ai_persona` | 身份(名字+维度)+ persona 脊柱 + voice 锚 |
| 个人档案 | `user_profile` | **只进记忆,硬防火墙(绝不进身份/voice)** |
| 长期记忆 | `memory_summary` | 进记忆;只能补「名字」 |
| 聊天记录 | `history` | voice + 记忆 + 身份(AI 真实行为) |

落点三维度:**① 记忆 ② Identity(名字/维度/自我介绍) ③ 相处时间**。

## 根因(5 条)
1. **压扁成单 source**:plaintext 端点把全部材料当一个 `source_kind=history` 的 job
   → `_build_reducer_output` 只跑 history 分支 → **角色卡的 `ai_persona` 分支
   (`worker.py:539`,`_fact_write(persona_material=角色卡)` → 身份)从未被调用**。
   history 分支里 `persona_material = existing_persona.content`,一次性流程
   `existing_persona={}` → 角色卡完全没喂进身份提取。
2. **history→身份提取偏弱**:即便走 history,名字/维度/天数也没捞出来(grounding 过保守)。
3. **`init_identity_if_absent`(`service.py:535`)只在身份卡不存在时写**;已存在
   → `already_initialized` 跳过。早先失败/僵尸尝试可能已写过一张空身份卡 → 这次成功的
   run 看到「已存在」就没覆盖 → 显示的是旧空身份卡。
4. **`days_with_user` 没来源**:没从历史时间戳 / 「认识时间」那步算 → 默认 0 → 今天 → 0 天。
5. **7.D 没接**:self_introduction / 签名 / 首问候 永远空。

## 设计原则(Seven 已认可)
- 身份/voice **只来自描述 AI 的东西**;**`user_profile` 永不进身份/voice(硬防火墙)**。
- 优先级:**`ai_persona`(权威) > `history`(安全主兜底:voice/维度/名字来自 AI 真实行为)
  > `memory_summary`(只补名字)**。
- 兜底哲学:**宁可身份空(可补),绝不让用户画像变成 AI 的身份**。空 > 错。
- user_profile 误填风险 → 靠**防火墙 + 清晰 UI 标签**,**不上分类器猜**(误判风险高)。

---

## 改造分项

### 三、【Codex】plaintext 端点:恢复 per-source 路由 + merge
`backend/genesis/routes.py`(`_prepare_plaintext_import` / `_run_plaintext_genesis_job`)
**不再单 history pass。按 source_family 分组,每组各跑一次 `_build_reducer_output`,
按顺序 merge**(复用现成的 `existing_persona`/`existing_voice` 跨-pass 合并参数 ——
分块设计当年就靠它跨 job 合并):

1. **ai_persona 组**(若有)→ ai_persona 分支 → 身份 + persona 脊柱 + 标 voice 锚。**先跑。**
2. **history 组**(若有)→ history 分支,`existing_persona`=①的 persona →
   voice + 事实 +(**若 ① 没给身份,则从 history 兜底出名字/维度/days**)+ merge persona。
3. **memory_summary 组** → 事实(剥身份);身份若仍缺名字 → 从这里补名字。
4. **user_profile 组** → 事实(防火墙剥身份)。
5. **合并产出**:身份=①>②>③补名字;voice=①锚+②例句 merge(§7.B);
   **所有源的 memory 都进 Garden**(去重);days 见「四」。

实现可复用 `_persona_support_messages` 已经按 `source_family` 打好标的 message —
按 family 分桶 → 各自 `_build_transcript_windows` → 各自 `build_reducer_output_from_texts`
→ 线性 merge。注意 §7.B order-edge:ai_persona 先于 history seed 身份,history 再 merge。

### 四、【Codex】身份写入:init → "init or update" + days 兜底链
`backend/genesis/service.py`:
- **`init_identity_if_absent` → upsert**:genesis 重跑时**覆盖 genesis 来源的字段
  (agent_name / dimensions / days / relationship anchor)**,但**保留 agent 自己
  post-respawn 写的 `self_introduction` / `signature`**(按字段/来源区分,别互相覆盖)。
- **`days_with_user` 兜底链**:onboarding「认识时间」显式日期 > 历史时间戳跨度
  (`timeline_span_days`) > memory_summary 里提到的关系时长 > 0。写进 relationship anchor。
- ⚠️ 放宽 `_identity_payload_from_output:530`「name 和 dims 都空才 return None」的逻辑要配合
  「四」的 upsert,避免兜底出的稀疏维度被丢。

### 五、【CC】history→身份提取加强(prompt)
`backend/genesis/prompts.py`:
- `FACT_WRITE_PROMPT`(或新增专门的身份 pass):可靠抽出
  **名字**(AI 在对话里被怎么称呼 / 怎么自称,不是用户名) ·
  **维度**(AI 表现出的性格,**每维必须带 description**,否则被 `service.py:505` 丢弃) ·
  **days_with_user**(从时间戳/内容)。
- grounding 仍是铁律(不编名字),但「有信号就要出」——别保守到啥都不给。
- ai_persona 分支的身份提取同样保证每维带 description。

### 六、【CC + Codex】7.D post-respawn
- spawn 后 agent 用 `io_cli identity-write` 写 **self_introduction + signature**,
  并发**第一句问候**("我来了",in-voice)。
- CC:7.D prompt + `agent_runtime` 触发编排(spawner/consumer 首轮钩子)。
  Codex:若需 supervisor/consumer 侧落点(首轮 gate)配合。

### 七、【Codex】验证卡完整性
`backend/hosted/onboarding_validation.py`:`identity_card` 要**真有 name 或 dimensions**
才 `passing`(不再「初始化了就算过」)。relationship_anchor 同理按真实写入判。

### 八、【CC】iOS 配套
`feedling-mcp-ios`:
- 「认识时间」stage 的日期喂给 `relationship_started_at`(已有 stage,确认接好)。
- `historyImportClientJobID` 把长期记忆内容算进去(否则幂等错误复用旧 job)。

---

## 验收标准(E2E,必须全绿)
1. 传角色卡 → 身份卡**有名字 + 维度(每维带描述)**。
2. 只传 history(无角色卡)→ **从历史兜底出名字/维度/天数**,voice 来自 AI 真实话。
3. **days > 0**(有时间依据时);只有"认识时间"无历史时也能出天数。
4. spawn 后 → **有自我介绍 + 主动问候**。
5. 重跑/重试 → **覆盖**旧空身份卡(不再 already_initialized 卡死)。
6. **user_profile 内容绝不出现在身份/voice**(firewall 回归测试)。
7. `validateOnboarding` 在身份真空时**不**报 passing。

## E2E 手段
- `tools/genesis_e2e.py` `upload-plaintext`:构造**多份材料**的合成测试集
  (一份带名字的角色卡 + 一份 history + 一份 memory + 一份 user_profile),跑到 done,
  断言:身份有名字/维度、days>0、firewall(user_profile 的独特串不出现在身份)、
  记忆数合理。再跑一遍**只有 history**的,断言兜底身份出得来。
- 真机:Seven 用新 build 重跑一把,看 Home 的 性格/天数/名字 + 进 app 的首问候。

## 分工
- **Codex(后端)**:三(per-source 路由+merge)、四(upsert+days)、七(验证完整性)、
  六的 supervisor/consumer 落点(若需)。
- **CC(我)**:五(prompt)、六(7.D prompt+编排)、八(iOS)、E2E harness + 验收 + 审计。
- 各自单测 + 我跑 env E2E;两边都绿才合 + 部署 + 让 Seven 真机验收。
