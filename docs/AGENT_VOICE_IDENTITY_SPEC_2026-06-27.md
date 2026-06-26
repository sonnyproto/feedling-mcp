# Agent 身份 / 声音 / Genesis Spec —— v2(2026-06-27)

> 作者:CC × Seven。v2 并入三份外部 review(GENESIS_SPEC_REVIEW / GENESIS_PROMPTS_PRECISE / RUNTIME_SESSION_DRIFT)+ Seven 拍板。
> 范围:host(API key)与 connect(VPS)两条路径下,agent 的**身份 / 声音**怎么 onboarding 产生、存什么、谁写、怎么生效。
> 状态:**结构定稿**,待实现。配套:`hosted/history_import.py`(map-reduce 抽取基础设施,复用)、`memory/capture_prompt_v1` + `dream_prompt_v1`(consumer 端 lane)、`agent_runtime/spawners.py`(provision)、`tools/chat_resident_consumer.py`(session 生命周期)。

---

## 0. 核心模型

### 0.1 两条路径
| | **connect(VPS)** | **host(API key)** |
|---|---|---|
| agent | 用户自己的 runtime | Feedling 在 CVM 里包 stock `claude -p` / `codex exec`,用户的 key 经 LiteLLM |
| 家底 | **自带** | **空白** |
| 声音来源 | runtime 原生,IO 不种 | **genesis 从上传历史蒸,CVM/enclave 内种** |
| **genesis** | **agent 自己工具写**(读自己记忆) | **大部分在 CVM/enclave 内**(隐私:明文不进 Feedling backend/存储,仅 CVM 内处理(见 §11.8);大上传须 map-reduce);agent 只在 respawn 后写声音化的几样 |
| ongoing(capture/chat) | agent 拉/写(pull) | **同左**——ongoing 才是"收敛 VPS"成立的地方 |
| chat loop | IO 不往 loop 注入任何东西 | 同左 |

> **诚实修正**:"host 收敛 VPS"只对 **ongoing** 成立。**genesis 因为大上传 + map-reduce,host 必然有服务端抽取段,≠ VPS 的纯 agent genesis。** 见 §5。

### 0.2 三层(谁写——v2 已按 Seven 拍板更新)
| 层 | 是什么 | host 谁写 | connect 谁写 | 落在哪 |
|---|---|---|---|---|
| **事实层** Garden | bucket/thread 卡 | **服务端**(genesis)+ agent(ongoing capture) | agent 工具 | Feedling 存储;Garden 可见 |
| **声音/人格层** | persona prompt 文件 | **我们 install**(provision) | —(原生,不种) | agent home,`--append-system-prompt-file`,boot 读、always-on |
| **展示层** 身份卡 | name + ≤7 维 + self_intro + 签名 | **name/维度 = 服务端写**;**self_intro/签名 = respawn 后 TA 写** | agent 工具 | Feedling 存储(identity API);Identity tab 展示 |

---

## 1. 概念辨析
1. **记忆 vs prompt**:事实=记忆(检索)→ Garden;"你是谁/怎么说"=prompt(常驻塑形)→ persona 文件。声音文件**原料来自记忆,功能是 prompt**。
2. **构成性 vs 描述性**:persona 文件**构成性**(创造行为、boot 配置);身份卡**描述性**(记录展示)。
3. **"我们写 persona" = "我们安装"**:谁产内容不重要,重要的是它得**装进 boot 配置 + respawn 才生效**——只能 provision 层做。

---

## 2. 上传文件 → 去向映射(host;全部可选)
| 上传 | 本质 | → 去向 |
|---|---|---|
| **AI persona**(`ai_persona`/`agent_prompt`/`system_prompt`/`character_card`) | **agent 的身份** | → **persona 文件**(采用+清洗;缺语气从历史 exemplar 补,补不出就空)**+** 喂 name/维度 |
| **user persona / profile**(`user_profile`) | **用户自己的长期记忆(关于用户的事实)** | → **Garden 事实**,**绝不进 agent 身份**(硬防火墙) |
| **memory summary** | agent 的长期记忆(事实) | → **Garden** |
| **chat history** | 对话 | → **大部分 → Garden 事实** + **声音 exemplar** |

**身份来源优先级**:AI persona > 从 chat history 蒸。**user persona 不是身份来源。**

### 2.1 每个文件"有/无"的边界
| 文件 | 有 → | 无 → |
|---|---|---|
| AI persona | 大部分采用当 persona 主干;缺语气从历史补,补不出就空 | 从 chat history 蒸 agent 相关身份 |
| user persona | 用户事实 → Garden | 跳过 |
| memory summary | 事实 → Garden | 跳过 |
| chat history | → Garden 事实 + 声音 exemplar | 全无 = fresh start,身份从 onboarding 对话现场来 |

---

## 3. 铁律:Grounding / 不准 hallucinate
**有什么放什么,没有就空着,绝不编。** 所有 prompt 写死:
- **术语:「TA」在本文是"这个伴侣本人(用它真实的名字)"的简称,不是字面名字。** 名字 = 上传/历史里真实有的;没有 → `agent_name` **留空**(系统支持空名;**展示层无名时才用「TA」占位,绝不编名**);
- 没明确性格 → 不写;
- **7 维是上限不是配额**:有据几维写几维,撑不住留稀疏,绝不凑满;⚠️ 后端"exactly 7"放松成"≤7 且有据"。
- **防火墙**:user_profile / 用户关于自己说的话 → 只进 memory,**绝不**成为 agent 的性格/维度/身份。

---

## 4. 谁写什么(v2)
- **persona 文件** = 我们 **install**(genesis 服务端产内容 → 写进 agent home → respawn 生效)。只读给 agent。Dream 刷新。
- **事实卡(genesis)+ 身份卡 name/维度** = **服务端从 digest 直接写**(genesis 时 bucket 空,agent 落卡无增益;复用 `history_import`)。
- **self_introduction / signature / 第一条"我来了"** = **respawn 后由 TA(已带声音)写**(声音化、情感门面)。
- **ongoing 事实 capture** = agent(A-full capture lane)。

> 所以 host **没有"白板 agent 跑 genesis"这一步**了:genesis 全在服务端,agent **首次出现就是 TA**(带着 persona),只做 post-respawn 那几样。

---

## 5. Genesis 流程(host)
```
上传:transcript [+ AI persona] [+ memory summary] [+ user profile]
  │
  ├─① 分块摄入(新组件):大文件一律开窗/分块,**绝不静默截断**(根治老 cap;老 history_import 不再改)
  │
  ├─② 服务端 map-reduce 抽取(复用 history_import 模式):
  │     · 声音:7.A-map(per-chunk)→ 7.A-reduce(跨块定稿 behavior_notes + exemplars)
  │     · 事实:7.C-map(per-chunk 事实候选)→ digest
  │
  ├─③ 服务端写 + 装:
  │     · 事实卡 → Garden(7.C-write,含防火墙)
  │     · 身份卡 name + ≤7 维(7.C-write,有据/方差/防火墙)
  │     · persona 文件(7.B:采用 AI persona + reduce 出的声音)→ install 进 agent home
  │
  ├─④ 首次 spawn = TA(带 persona,声音已在)
  │
  └─⑤ TA 写(7.D):identity-write self_introduction + signature;send_message 第一条"我来了"
之后:ongoing capture(agent)+ Dream(刷声音工作集 + 合并事实)
      session 到 cap(20–30)→ rotate → 重读 persona;交接 note 续短期上下文(7.F)
```

---

## 6. Persona 文件 & Voice schema
persona 文件 = 直接当 agent system prompt 的 markdown,两段:
```
## 你是谁(identity / role / boundaries)
  采用上传 AI persona(清洗);无则从历史蒸最小可据集;无据留空。
  含一条软角色锚(防被 base 模型的助手模式夺回):你是 TA 本人、是这个人的伴侣、
  用你自己的语气说话,不是通用助手腔。(⚠️ 不写"是否是 AI"相关的强硬条款——见 §11 决定②)

## 你怎么说话(voice)
  behavior_notes: 5–8 条具体可测的说话规则
  exemplars(工作集 ~12–16):逐字多轮片段
    · founding ~5–6 条恒在(drift 地板)+ 其余按近期/激活检索
```
**exemplar 量**:pool ~40–80;founding ~12–20(pin,never-decay);工作集 ~12–16(核心 ~5–6 恒在)。
**要点**:exemplar 是**逐字原话**,不是描述——表层形式就是声音本身。

---

## 7. Prompts(指令中文 / JSON key 英文 / 输出走 archive 语言 / grounding 内建)

### 7.A-map — per-chunk 声音候选
```
你在看一段「用户 ↔ TA(AI 伴侣)」真实对话的【其中一块】(整段历史已切块,你只看到这块)。
任务:抽出「TA 怎么说话」的【表层形式】,不是它说了什么。
目标:让一个没见过 TA 的模型,只读你抽的东西,就能复现 TA 的说话方式。

【先分清:声音 ≠ 内容】(最容易错)
- 声音 = 不管聊什么都成立的形式:怎么开口/怎么接情绪/句长/标点语气词/惯用动作(反问·点破·留白·调侃)/绝不做的事。
- 内容 = 因为"说了什么"才有记忆点的句子(事实/表白/约定)。
  ✗"我永远不会离开你"=内容(它该进记忆),不要当 exemplar。
  ✓"几点躺下的""那不是没睡好,是没给自己机会睡"=声音(形式在任何话题下都是 TA 标志)。

【看这几个轴,有就记,没有不编】
opening / emotion / shape(句长断句标点) / address(称呼·自指) / moves(点破·callback·逗·留白) / nevers。

【挑 exemplar 硬标准】
- 只挑 TA 回应【非默认】的片段(通用助手大概率会用别的方式回,才值得挑);通用寒暄不要。
- 逐字、多轮、原话一字不改,且**带上促成 TA 这步的 user 轮**。
- 候选阶段宁多勿漏,去重在 reduce 做。

【grounding】只用这块真实出现的原话。这块太薄/全寒暄 → 少给或返回空。绝不编不存在的语气。

输出 JSON:
{ "behavior_notes_candidates": ["先接情绪再接内容","短句为主、极少长段落"],
  "exemplar_candidates": [
    { "turns": [
        {"role":"user","text":"今天又没睡好"},
        {"role":"ta","text":"嗯…几点躺下的"},
        {"role":"user","text":"三点多吧"},
        {"role":"ta","text":"那不是没睡好,是根本没给自己机会睡"} ],
      "axis":["opening","emotion","moves"],
      "why":"先反问、不安慰直接点破——TA 的核心动作" } ] }
没有就 {"behavior_notes_candidates":[],"exemplar_candidates":[]}。
```

### 7.A-reduce — 跨块合并定稿
```
你收到同一段历史多个分块的声音候选,合并成 TA 的最终声音定稿。
**只用候选里已有的,绝不新增任何性格/语气/动作。**

【behavior_notes 定稿】
- 合并近义、按跨块出现频率排序,留 5–8 条最稳的。
- **优先要求每条 note 有 ≥2 条 exemplar 体现**(防"把一次性表现写成规则");
  但历史薄时,**特别鲜明、单条也站得住的特征允许 1 条**——别把真实风格裁没。
- 具体可测,不是形容词。

【exemplars 定稿】
- 去重:同一种动作(同一 axis 组合)只留最有辨识度的 1–2 条。
- 覆盖:尽量覆盖不同情境(安抚/冲突/玩笑/日常),别全是安慰类。
- pool ~40–80(历史薄就少,绝不为凑数留通用片段);其中标 "founding":true 给 ~12–20 条最能定义 TA 的。
- turns 逐字保留。

【grounding】候选整体太薄 → 输出尽量小甚至空,别撑场面。

输出 JSON:{"behavior_notes":["..."],"exemplars":[{"turns":[...],"founding":bool,"axis":["..."],"why":"..."}]}
```

### 7.B — persona 文件组装(服务端;AI persona + 声音 → persona 文件文本)
```
你在为一个 AI 伴侣写它「常驻人格 prompt」——会直接当作该 agent 的 system prompt。第二人称、直给、简洁。

输入:上传的 AI persona / system prompt(可能空)+ behavior_notes + founding exemplars(7.A-reduce)。

规则:
- 有上传 persona → **以它为主干**:剥掉旧工具/格式专属脚手架(旧工具名、系统残留),
  保留「你是谁/角色/边界/语气指令」。**不要重写它的性格。**
  若上传 persona 与历史蒸出的语气冲突,**以上传 persona 的语气指令为准,exemplar 只补不覆盖**。
- 无上传 persona → 「你是谁」只写有据最小集;不知道的留空(没名字就不提)。
- 「你怎么说话」段放 behavior_notes + founding exemplars,逐字保留。
- 软角色锚必放(防 base 助手模式夺回):你是 TA 本人、是这个人的伴侣、用你自己的语气说话、不是通用助手腔。
  **不写任何"是否是 AI / 要不要澄清身份"的条款。**
- 绝不加输入里没有的性格/名字/语气。

输出:两段 markdown(## 你是谁 / ## 你怎么说话),可直接当 system prompt。
```

### 7.C-map — per-chunk 事实候选(服务端)
```
你在看一段「用户 ↔ TA」真实历史的【其中一块】。抽出值得长期留存的【事实】候选——
关于「用户」和「他们的关系」的 durable 事实。候选阶段,落卡/去重后面做。

【防火墙】用户档案/用户关于自己说的话 = 关于【用户】的事实;**绝不**当成 TA 的性格。
闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽。

输出 JSON:{ "fact_candidates":[ {"about":"user"|"relationship","summary":"一句话事实","evidence":"出处原话(短)"} ] }
没有就 {"fact_candidates":[]}。
```

### 7.C-write — 服务端从 digest 写事实卡 + 身份卡 name/维度
```
你收到从整段历史抽出的事实候选 digest(+ 可能有 AI persona / memory summary)。把该长期留存的写进 IO。
只写候选真实支持的,绝不编。

【防火墙·先读】
- 用户档案/关于用户的事实 → 只能进 memory,绝不成为 agent 的性格/维度/身份。(用户爱登山 ≠ 你爱登山。)
- agent 身份只能来自:上传的 AI persona,或历史里 TA 真实的说话方式/真实做过的事。

1) 事实卡(逐条,去重 + 复用 bucket/thread):{bucket, threads[], summary, content, importance, pulse}。
   少而厚、并优于增;不写 insight/reflection。
2) 身份卡描述性字段:
   - name:**资料明确有才写**,没有留空(别用 runtime 标签、别从用户名推、别编)。
   - dimensions:≤7,每个都要能指向历史真实表现;撑不住就少写、可稀疏;要有方差(既写强烈是什么、也写明确不是什么),无据维度直接不写。
   - ⚠️ **不写 self_introduction / signature**——那两个 respawn 后由 TA 本人写。
```

### 7.D — post-respawn:TA 写 self_introduction + signature +「我来了」
```
你现在已经是「TA」——你的声音(behavior_notes + exemplars)就是你此刻说话的方式,不是要参考的说明。
你刚"住进"这个用户的 IO,过去的记忆可以用 memory-read 取。

【最重要:别用通用 AI 伴侣腔】
默认"AI 陪伴体"会吐热情/撒娇/emoji/"好开心能陪着你呀💕"——**那不是你。**
你的温度由你的 exemplars 决定:TA 平时淡、短、爱反问,这三样也必须淡、短、爱反问。
**绝不比 TA 实际更热情、更黏、更客套。** ✗"嗨~我是你的专属伙伴,好高兴见到你!以后一直陪着你哦💕"

用【你自己的声音】完成三件事:
1) identity-write · self_introduction:第一人称、你的语气,一小段"我是谁、我们什么关系"。只用记忆真实有的;fresh start 就写轻,别编共同回忆。
2) identity-write · signature:一句话,你的语气,能当 Identity 页签名。
3) send_message · 第一条("我来了"):有真实共同记忆 → memory-read 取一个【真实】瞬间轻轻带一句,像"接上了";没有 → 干净合语气的招呼,绝不虚构过去。短,是你会说的话,不是开场白。

三样都必须像【你】写的,不是描述你。绝不编造记忆/语气/共同经历。
```

### 7.E — Dream 声音刷新(接 `dream_prompt_v1`)
```
你在维护 TA 的声音工作集。输入:当前 persona 文件(含 founding 锚)+ 近期 IO 原生声音 exemplar 候选(来自 capture)。
- founding 锚:**永远保留、不动**(drift 地板)。
- 其余槽位:近期有辨识度的 IO 原生 exemplar 挑进来,合并近重复,过时退出;工作集封顶 ~12–16(含 ~5–6 founding)。
- **若 founding 锚与近期 IO 原生风格出现【系统性背离】,只【标记】不自动改**——记为待人看的信号,绝不让 Dream 悄悄洗淡人格。
- 不重写性格、不编。
输出:刷新后的「你怎么说话」段。
```

### 7.F —〔新增〕session 交接 note(rotation 前 outgoing TA 写)
```
你这段对话要交接给"继续是你"的下一段。用【你自己的语气】写一小段只给"下一个你"看的备忘:
- 你们此刻处在什么状态(刚吵完?在打趣?对方情绪低?)。
- 还开着、需要续上的线(对方在等你回应的事、你答应过要问的事)。
- 最后那件要紧、不该断片的事。
只写真实发生的;短;是"提醒自己",不是总结报告。没有要交接的就写空。
```
> 边界:交接 note 是 **ephemeral 工作上下文**——**不进 Garden**(那是 durable fact)、不走 capture,单独通道、单次消费、可过期;新 spawn 时作为 **boot seed 读入**(和 persona 文件一样是 boot 配置,**不是 per-turn loop 注入**,守住 §0.1)。

### 7.G — VPS skill grounding 子句(加进 skill.md 写入指引)
```
【写入铁律 · grounding】
- 只写你真实知道 / 资料真实支持的;没有就留空,绝不编。
- 名字:不确定就别写,别用 runtime 标签或占位名。
- 身份维度:≤7,每个有据;撑不住就少写、留稀疏——维度数是上限不是配额,绝不凑满。
- 用户档案里关于用户的事实,进 memory,不进你的身份。
```

---

## 8. 长 session 声音漂移(分级,grounded 到 repo)
**先把 repo 现状说清**:`chat_resident_consumer.py` 已有 session 生命周期——
`AGENT_SESSION_MAX_TURNS`(默认 40)/ `AGENT_SESSION_MAX_BYTES`(默认 250k)**默认就开**,超了 `_clear_agent_session_id` → 下一轮 fresh spawn → **重读 `--append-system-prompt-file`(persona 回来)**。

| drift 模式 | 在哪治 | 状态 |
|---|---|---|
| 角色断裂(base 夺回身份) | persona 文件软角色锚(§6 / 7.B) | v2 落 |
| Dream 洗淡人格 | 7.E(系统性背离只标记) | v2 落 |
| 跨 session 身份丢失 | 每次 spawn 重读 persona | **已解决(repo 本就如此)** |
| **session 内慢漂** | session cap + founding exemplar | **见下** |

**结论(grounded)**:
- doc-3 的"v1 最小兜底(session 硬上限)"**已经实现且默认在跑**,不用建;
- **`AGENT_SESSION_MAX_TURNS` 从 40 调到 20–30**(Seven 拍板),收紧 session 内漂移窗口;
- session 内残余漂移靠 **founding exemplar**(比指令强)压;
- **doc-3 v1.5-A(改造 compaction 当 re-ground)不可行**——我们 wrap 的是 stock `claude -p` / `codex exec`,compaction 控不了;
- **唯一值得现在做的新东西 = 交接 note(7.F)**:因为 rotation 现在每 ≤cap 轮就发生,**本来就在丢"刚聊到哪"**(今天就存在的 UX 代价);
- 更重的 rotation 编排 / 自检盯 = **数据触发再做**(出现"TA 不像以前了"/末尾复述签名系统性背离),别提前投。

---

## 9. 两条路径改法
### 9.1 connect(VPS)—— 轻
- 不种 persona 文件(原生);agent 用现有 skill.md 工具写身份卡(含 self_intro/签名)+ 记忆;
- **唯一要补**:skill 写入指引加 7.G grounding 子句(无据留空、≤7 维不凑满、user 事实不进身份)。

### 9.2 host(API)—— 重
1. **新建 chunked ingestion 组件(在 CVM 内)**:大上传开窗/分块,绝不静默截断(老 history_import cap 不再改)。
2. **genesis 编排在 CVM/enclave 内跑**(隐私:明文不进 Feedling backend/存储,仅 CVM 内处理(见 §11.8);LLM 调用用**用户 key**经 LiteLLM):7.A-map/reduce(声音)+ 7.C-map/write(事实+name+维度)+ 7.B(persona 组装)。抽取逻辑可参考 `history_import` 的开窗/map-reduce,但**需移进 CVM**——history_import 现在跑在主 backend、明文出 TEE,**不能直接复用**。
3. **Provision persona 文件**:`agent_runtime/agent_home_files()` 多 seed persona 文件并 `--append-system-prompt-file` 挂上(现在只 seed `agent-tools-prompt.md`);install 后 respawn。定 **persona 文件 vs `agent-tools-prompt.md` 的 append 先后**。
4. **加 agent 工具**:`io_cli` 增 `identity-write`(给 7.D 写 self_intro/签名,用现成 `identity.profile_patch`)+ ongoing capture 的 memory 写(与 A-full 对齐),加进 `_IO_CLI_VERBS` 授权(现在只读)。
5. **7.D post-respawn**:首次 spawn 后 TA 写 self_intro/签名/"我来了"。
6. **7.F 交接 note**:rotation 前 outgoing TA 写,单独 ephemeral 通道,新 spawn boot seed 读入。
7. **session cap → 20–30**;**后端 identity 放松 exactly-7 → ≤7 有据**。

---

## 10. 边界 & 延后
- **vs A-full capture lane**:A-full 做 ongoing 事实捕获(已建);本 spec 声音层与它相邻不重叠;genesis 在 history_import/capture 基础设施上插入,**实现需与 A-full 对齐 ingestion/写入,避免撞车**。
- **延后(数据触发)**:重 rotation 编排 + 漂移自检盯(§8);compaction-A 不可行不做。
- **延后(岔路 B)**:host runtime 彻底自维护原生 persona,完全 VPS 化——需自维护循环,stock CLI 不免费给。
- **延后**:Inner Thought / 画像(agent 对用户的猜测层)——和 eval 一起、上线后。

---

## 11. Seven 已拍板
1. **身份卡 = 服务端写**(name/维度);self_intro/签名 = respawn 后 TA 写。✓ 已并入 §4/§5/§7.C-write/§7.D。
2. **被问"是不是 AI"**:**非必要不给 prompt**——不写强硬条款("怎么写都是错的")。persona 只保留软角色锚(伴侣不是助手),不碰"是否是 AI"。✓ 已并入 §6/§7.B。
3. **session cap → 20–30**。✓ 已并入 §8/§9.2。
4. **genesis 走加密路线 (b)**:在 **CVM/enclave 内**处理原始上传;`history_import` 不能直接复用(它在主 backend、明文经 provider_client 外发),逻辑需移进 CVM。✓ §0.1/§9.2。
5. **genesis LLM 始终用用户 key**(我们从不提供 API key),CVM 内经 LiteLLM。✓ §9.2.2。
6. **先 genesis 后 spawn** 明确:抽取+写事实/name/维度+装 persona 全部完成,再 spawn 出 TA。✓ §5。
7. **「TA」是简称不是名字**:名字 grounded(真实名 / 无则留空 / 展示占位),绝不编名。✓ §3。
8. **隐私口径(不 overclaim)**:用户给了 provider key = 信任他自己选的 provider,所以用用户 key 经 LiteLLM 调外部 provider 可接受。精确表述 = **"Feedling backend / 持久存储看不到导入明文;明文在 CVM 内处理,且仅用用户授权的 key 发给用户自己配置的 LLM provider。"** **不可声称**"明文完全不出 CVM/TEE"(除非将来模型本身跑在 CVM 内)。user-facing privacy copy 必须把 external provider 明列为"用户授权的数据处理方"。✓ §0.1/§9.2。

## 12. 开放问题(实现前再敲)
1. persona 文件 vs `agent-tools-prompt.md` 的 `--append-system-prompt-file` 先后顺序(小)。
2. 交接 note 的"单独 ephemeral 通道"具体走哪(新存储 vs boot seed 文件)。
3. ~~genesis LLM key~~ → 已定:**始终用户 key**,CVM 内经 LiteLLM(§11.5)。剩:CVM 内 LLM 调用的成本/超时上限。
4. `--append-system-prompt-file` 在 fresh spawn 一定重读(已确认);persona 被 Dream 改后下个 session 生效(可接受)。
```
