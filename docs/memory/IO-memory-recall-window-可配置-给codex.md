# recall 召回窗口可配置 + 全开 · 执行说明(CC → Codex)

> 2026-06-22 · 作者:Claude(CC) · hx 已拍板
> 一句话:**把 agent 召回(index/fetch)的 top-N 窗口做成可配置——默认仍 50,可配具体数字,可配"全开(不截断)";全开时只去掉"截断",资格过滤 / 排序 / selector 都照留;另加 HARD_MAX 安全阀。**

---

## 0. 背景(为什么要做)

当前 agent 召回靠"按元数据取前 50 张"当候选窗口,**query-agnostic + 固定 50** → 卡多时会漏掉相关的旧卡(尾巴丢失)。做成可配置:近期默认 50 止血,以后按 `user_card_count` 监控决定调大 / 全开,**纯配置切换、不改代码**。

---

## 1. ⚠️ 先看清:50 卡在三处,有一处写死(最容易漏)

| 位置 | 现状 | 要改成 |
|---|---|---|
| `backend/memory/routes.py:28` `_MEMORY_READSIDE_LIMIT = 50` | 字面常量 | 读 env,默认 50 |
| `backend/memory/routes.py:116` `candidates[: min(limit, _MEMORY_READSIDE_LIMIT)]` | 夹到 50 | 支持"全开"→ 夹到 HARD_MAX |
| `backend/memory/routes.py:140` `requested_limit = payload.limit or _MEMORY_READSIDE_LIMIT` | 默认 50 | 默认走 env |
| **`backend/enclave_app.py:1079 / 1104` `moments[:50]`** | **硬写死 50** | **用同一个 limit(从请求传入或读同一 env)** |

**最关键**:就算后端放大,**enclave 的两处 `moments[:50]` 还会砍回 50**——**必须一起改**,否则配置无效、被静默盖住。

(注:`enclave_app.py:821 _memory_readside_model_api_limit()` 是**另一条路**——route B 自动召回的 limit,已可配。**本次不动它**,别搞混。)

---

## 2. 配置设计

```text
FEEDLING_MEMORY_READSIDE_LIMIT     默认 50      # 召回窗口大小
                                   = 0          # 哨兵值 = "全开"(不截断)
FEEDLING_MEMORY_READSIDE_HARD_MAX  默认 1000    # 安全阀:全开也不超过这个
```

- 具体数字(如 80 / 120):取该数字(仍 ≤ HARD_MAX)。
- `0` = 全开:不做 top-N 截断,但实际仍受 HARD_MAX 兜底。

---

## 3. 行为规则(全开 ≠ 把整步删掉)

召回候选那一步其实做三件事,**全开只关"截断"这一件**:

| 子步骤 | 代码 | 全开时 |
|---|---|---|
| ① **资格过滤** | `_memory_readside_available`(active / 属本人 / 可解密 / **非 superseded·archived·local_only**) | ✅ **永远保留** |
| ② **排序 + 截断** | sort + `[:limit]` | **截断关掉**(放开到 HARD_MAX);排序**保留**当呈现顺序 |
| ③ **按 query 挑** | `select_memory_index_items` | ✅ 照跑,且看的是全部、更准 |

> **硬约束**:全开**绝不能连 ① 也关掉**。否则已退场 / superseded 的旧卡会被重新召回(比如被取代的"狸花猫"又冒出来),逻辑就乱了。

---

## 4. 实现要点

1. `_MEMORY_READSIDE_LIMIT` 改成读 `FEEDLING_MEMORY_READSIDE_LIMIT`(默认 50)。
2. 新增读取 `FEEDLING_MEMORY_READSIDE_HARD_MAX`(默认 1000)。
3. 解析逻辑:`limit==0 → 用 HARD_MAX`;否则 `min(limit, HARD_MAX)`。
4. `routes.py:116` 的 clamp 用上面解析出的有效上限,不再硬夹 50。
5. **`enclave_app.py:1079 / 1104` 的 `moments[:50]` 改成 `moments[:effective_limit]`** —— effective_limit 从请求 payload 传入(后端已知)或 enclave 读同一 env。**这处务必改,别漏。**
6. ① 资格过滤、② 排序、③ selector 逻辑**原样不动**。

---

## 5. 测试(`tests/test_memory_readside*.py` 扩展)

| 用例 | 断言 |
|---|---|
| 默认(env 未设) | 行为 = 现状 top-50(回归不变) |
| `LIMIT=120` | 后端**和 enclave**都返回到 120(不被 50 偷偷夹回)← 重点测 enclave |
| `LIMIT=0` 全开 | 返回**全部**合格卡,不截断 |
| 全开仍排除退场卡 | superseded / archived / local_only **仍不出现**(① 没被关) |
| 全开排序保留 | 返回顺序仍按 salience/importance/时间 |
| 全开 + query | selector 仍能从全部里挑 suggested_ids |
| 超 HARD_MAX | 合格卡 > HARD_MAX 时,全开也只到 HARD_MAX(并 log/标记 truncated) |

---

## 6. 范围 / 别碰

- ❌ 不动 `_memory_readside_model_api_limit()`(route B 自动召回的另一条 limit)。
- ❌ 不动 index/fetch 两层结构(全开时两层仍省 token + 隐私)。
- ❌ 不动 M2 写入。
- ✅ 建议同时确认 `user_card_count` 已在 trace 里(用来日后决定何时调大/全开)。

---

## 7. 一句话收口

> **默认 50、可配数字、`0`=全开;全开只去掉"截断",资格过滤 / 排序 / selector 全留,另有 HARD_MAX 兜底。最易漏的是 enclave 里两处写死的 `moments[:50]`,必须跟着改,否则配置被静默盖住。**
