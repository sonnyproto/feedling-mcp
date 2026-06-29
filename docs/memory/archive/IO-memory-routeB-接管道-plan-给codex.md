# route B 接管道 · Plan(Claude → Codex)

> 2026-06-21 · 作者:Claude(Opus 4.8) · 给执行代码的 Codex
> 决策来源:hx 拍板「先把 route B(API/model_api 形式)搞好,用户最多最能体现」+「先只接管道,不提精准」。
> 一句话:**把 route B 的记忆召回来源,从现有 `select_context_memories` 换成统一的 readside(index → select → fetch),作为生产基座。本轮不提精准,目标是「不回归 + 统一代码路 + 为后续 agentic/embedding 升级铺一处可改的口子」。**

---

## 0. 必读:本轮的真实定位(别误解)

- 对 route B 来说这是**内部重构,不是用户可感的新能力**。现状 route B 已经有确定性关键词召回了(每轮 ≤8 张卡注入 prompt)。
- 新旧选择器**底层是同一个关键词打分器**(都 import `context_memory_selection`),所以**精准度不变**——用户不会觉得「记得更准」。
- 做它的价值是:① 真实流量验证新 readside;② route A / route B 共用一套召回,后续 embedding/agentic **只改一处**;③ 拿到 eval 基线。
- **不要**对外宣称这轮提升了召回质量。

---

## 1. 现状链路(route B / model_api)

```
app.py model_api flow
  → _enclave_get_json(/v1/chat/history?context_mode=model_api)        # app.py ~8430
  → enclave v1_chat_history()                                        # enclave_app.py:1012
      → _load_decrypted_moments(api_key, user, sk)  (解密 ≤200 条)    # enclave_app.py:1144
      → select_context_memories(moments, latest_user_text, ≤8)        # enclave_app.py:1154/1160
      → 回 context_memories[]                                         # enclave_app.py:1167
  → app.py 读 hist["context_memories"] 注入 prompt                    # app.py:8438
```

注入点在 `app.py`,但**召回选择发生在 enclave**。

---

## 2. 接管道 = 改什么

**只改 enclave 一处,app.py 不动**(它继续读 `context_memories`):

```
enclave v1_chat_history():
  旧: select_context_memories(moments, latest_user_text)
  新: index → select_memory_index_items(query=latest_user_text) → fetch 对应 ids
      产出同样形状的 ≤8 张全文卡,塞进 context_memories
```

- 复用本次已建的 `select_memory_index_items`(`memory_index_selector.py`)做 pick。
- 产出口径必须和旧 `context_memories` 一致(字段、条数),让 `app.py` 注入逻辑零改动。

---

## 3. ⚠️ 回归风险与防护(本轮重点)

### 风险 1:新路召回可能比现状更差
- 现状:enclave 解密 **≤200** 条再选。
- 新路 readside:backend 先按**元数据 top-50(查询无关)**预筛再选。
- 卡多的用户,相关旧卡排到第 51 名 → 新路看不到 → **召回退步**。
- **防护**:
  - 评估真实用户卡片数分布;若普遍 >50,**把预筛 limit 调大**(80/100/200),或卡少时直接沿用「解密全部再选」。
  - 上线灰度阶段盯「注入卡数 / 命中率」是否较老路下降。

### 风险 2:两个选择器口径不一致 → 行为漂移
- 新 `select_memory_index_items`:`cap=8, min_score=0.28`,带 topic_match/sensitive 门槛。
- 旧 `select_context_memories`:自己的阈值/条数(mode=model_api)。
- **防护**:对齐条数与召回松紧;先用旧路做基线,新路参数调到「注入结果 ≈ 旧路或更全」。

### 风险 3:别误以为给 route B 加了隐私
- route B 的 **backend 为了拼 prompt 本来就拿全文卡**;index 的「安全摘要」隐私分层主要利好 route A。
- 本轮不要把隐私当卖点宣传。

---

## 4. 怎么安全切

1. **加 flag**:如 `MEMORY_READSIDE_FOR_MODEL_API`(默认 `false`)。
   - `false` = 完全走老路 `select_context_memories`,零影响。
   - `true` = 走新 readside 路。可秒级回滚。
2. **最小防回归 eval**(不是证明变好,是保证不更差):
   - 一二十条 probe(涉及过往的问题 + 期望召回的卡)。
   - 对比「老路注入卡」vs「新路注入卡」:新路不能比老路漏更多。
   - 这是切 flag 到 `true` 的**前置条件**。
3. **灰度**:小流量开 `true`,盯命中率/注入卡数无下降,再放量。

---

## 5. 验收标准

- flag `false` 时:route B 行为与现状**逐字节一致**(完全老路)。
- flag `true` 时:注入的 `context_memories` 形状/字段与老路一致,`app.py` 注入零改动。
- 防回归 eval:新路召回 ≥ 老路(不漏更多)。
- 旧 `/v1/chat/history` 其他字段、`context_memory_trace`、Memory Garden 不受影响。
- backend 日志不打 `summary/verbatim/follow_up`(沿用 readside 既有约束)。

---

## 6. 明确不做(锁死范围)

- ❌ agentic 召回(hosted runtime 把记忆当工具自己挑)—— 下一阶段「提精准」再做。
- ❌ embedding / 向量召回 —— 提精准的规模化正解,post 本轮。
- ❌ 写入 / insert / supersede / merge / contradict / decay。
- ❌ route A 收口。
- ❌ iOS UI 改动。
- ⚠️ `AGENTS.md` 是未跟踪文件,不属于本次改动,别误提交。

---

## 7. 下一阶段预告(给 Codex 心里有数)

本轮「接管道」跑稳后,route B「提精准 = 让用户真感觉到」的两条路(二选一,届时再定):
- **Agentic**:把记忆做成 hosted runtime 可请求的工具(像 `web_search`),LLM 自己语义挑。复用 index/fetch,建得快;每轮多一次往返、best-effort。
- **Embedding**:向量相似度替换关键词选择器(enclave 内对解密明文算)。每轮都有、延迟低、不加 LLM;要新建向量组件。**「用户最多」场景的生产正解。**

因为本轮已把召回统一到一条 readside 路,**那时只改这一处,route A / route B 同时受益。**

---

## 8. 一句话收口

> **route B 接管道 = 把召回来源统一到 readside(只改 enclave 一处 + flag 灰度),精准不变、用户无感,价值在「统一代码路 + 真实验证 + 铺路」。唯一要小心的是 top-50 预筛比现状 ≤200 窄,可能漏召回——所以必须带防回归 eval 和可回滚 flag。提精准(agentic/embedding)是下一阶段,届时只改这一处。**
