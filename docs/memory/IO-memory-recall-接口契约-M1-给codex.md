# M-1 接口契约:`POST /v1/memory/recall`(给 Codex 落代码)

> 2026-06-24 · 作者:CC · 状态:**待 Codex 复核后实现**
> 隶属里程碑 `IO-memory-里程碑M-读写闭环+行为一致-plan.md` 的 **M-1**。
> 核心原则:**不新造 selector / readside,纯编排现有件**。recall = 服务端版的 `index → 语义挑 → fetch`,用**真 selector** 替掉 consumer 现在手搓的 score 排序。

---

## 0. 一句话

把 consumer 现在 `_memory_recall_for_message` 里手做的 "调 `/v1/memory/index` → 自己按 score topK → 调 `/v1/memory/fetch`" **搬进后端一个端点**,并把"自己按 score topK"换成**已有的 `select_memory_index_items`(语义挑,route B/enclave 同款)**。

---

## 1. 复用映射(现成函数,别重写)

| 步骤 | 复用 | 出处 |
|---|---|---|
| 出摘要候选(接 enclave 解密) | `memory_readside_core.memory_index_core(store, api_key, payload, post_enclave=...)` → `{items, limit, truncated, user_card_count}` | `backend/memory_readside_core.py:198` |
| **语义挑(就是它,别新造)** | `memory_index_selector.select_memory_index_items(query, index_items, cap=, include_sensitive=)` → `{selected_ids, trace}`(mode `memory_index_selector_v1`) | `backend/memory_index_selector.py:173` |
| 取正文(接 enclave 解密) | `memory_readside_core.memory_fetch_core(store, api_key, {ids}, post_enclave=...)` → `{items, missing_ids, unavailable_ids}` | `backend/memory_readside_core.py:235` |
| enclave 回调 | `_memory_readside_post_enclave`(routes.py 里已有的那个 wrapper) | `backend/memory/routes.py:102` 区 |
| **整条流水线的现成参考** | `_select_context_memories_via_readside(moments, latest_user_text, cap)` —— route B 就这么 index→select→fetch 的 | `backend/enclave_app.py:892` |

> route B fallback(`hosted/chat_routes.py:72`)和 enclave 都已 import 同一个 `select_memory_index_items` → **recall 用它 = 三方真正同一个 selector**,不分叉。

---

## 2. 请求

```
POST /v1/memory/recall
Headers: X-API-Key: <FEEDLING_API_KEY>     # 与 index/fetch 同款鉴权(auth.require_user)
Content-Type: application/json
```
```jsonc
{
  "query": "用户这句话 / 召回 query",   // 必填,非空字符串(空 → 400 或返回空)
  "top_k": 5,                          // 选填,挑选后返回几张卡(= selector cap)。默认 5
  "limit": 50,                         // 选填,index 候选窗口(传给 index_core / effective_readside_limit;0=全开 + HARD_MAX)。默认走 effective_readside_limit
  "include_sensitive": false           // 选填,默认 false(敏感卡默认不取)
}
```

## 3. 响应(200)

```jsonc
{
  "items": [ /* 完整卡正文,字段与 /v1/memory/fetch 的 items 一致 */ ],
  "ids":   ["m_xxx", "m_yyy"],         // selected_ids,按挑选顺序
  "trace": {
    "mode": "memory_index_selector_v1",
    "query": "...",
    "selected":      [ { "id","score","confidence","reason","matched_units","matched_phrases","summary","is_sensitive" } ],
    "skipped_sample":[ { "id","reason","score","confidence","summary" } ],   // 截断样本
    "index_count":   123,              // 进入挑选的候选数(= index items 数)
    "candidate_total": 200,            // user_card_count(总可用卡)
    "truncated": true,                 // index_core 的 truncated(候选被窗口截断)
    "limit": 50,
    "top_k": 5
  },
  "missing_ids":     [],               // 来自 fetch_core
  "unavailable_ids": []                // 来自 fetch_core(enclave 解不开 / 已归档等)
}
```

> `trace` = selector 自带的 `trace`(selected / skipped_sample)+ index 元信息(index_count/candidate_total/truncated/limit/top_k)。**这份 trace 就是验收"agent-first 真的活 / 兜底挑了什么"的观测点。**

## 4. 错误

| 情况 | 码 | body |
|---|---|---|
| enclave 不可用(`RuntimeError`) | 503 | `{"error": "..."}`(同 index/fetch) |
| 参数非法(query 空 / limit 非法) | 400 | `{"error": "..."}` |

## 5. handler 伪码(放进 `backend/memory/routes.py`,挨着 index/fetch)

```python
@bp.route("/v1/memory/recall", methods=["POST"])
def memory_recall():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or "").strip()
    if not query:
        return jsonify({"items": [], "ids": [], "trace": {"mode": "memory_index_selector_v1", "query": ""}}), 200
    top_k = max(1, int(payload.get("top_k") or 5))
    include_sensitive = bool(payload.get("include_sensitive", False))
    try:
        # 1) 候选摘要(接 enclave)
        index_resp = memory_readside_core.memory_index_core(
            store, api_key,
            {"query": query,
             "limit": memory_readside_core.effective_readside_limit(payload.get("limit")),
             "include_sensitive": include_sensitive},
            post_enclave=_memory_readside_post_enclave,
        )
        # 2) 语义挑(现成 selector,别另写)
        selection = select_memory_index_items(
            query, index_resp.get("items") or [],
            cap=top_k, include_sensitive=include_sensitive,
        )
        ids = selection.get("selected_ids") or []
        if not ids:
            return jsonify({"items": [], "ids": [], "trace": {**selection.get("trace", {}),
                            "index_count": len(index_resp.get("items") or []),
                            "candidate_total": index_resp.get("user_card_count"),
                            "truncated": index_resp.get("truncated")}}), 200
        # 3) 取正文(接 enclave)
        fetch_resp = memory_fetch_core(
            store, api_key, {"ids": ids},
            post_enclave=_memory_readside_post_enclave,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    trace = {**selection.get("trace", {}),
             "index_count": len(index_resp.get("items") or []),
             "candidate_total": index_resp.get("user_card_count"),
             "truncated": index_resp.get("truncated"),
             "limit": index_resp.get("limit"), "top_k": top_k}
    return jsonify({
        "items": fetch_resp.get("items") or [],
        "ids": ids,
        "trace": trace,
        "missing_ids": fetch_resp.get("missing_ids") or [],
        "unavailable_ids": fetch_resp.get("unavailable_ids") or [],
    }), 200
```

## 6. 谁来调(及迁移)

- **consumer 兜底(M-2)**:`_memory_recall_for_message` 删掉自己的 `index + score topK + fetch`,改成**一发 `POST /v1/memory/recall`**,只负责把返回的 `items` 拼成 prompt block。consumer 不再排序/挑选。
- **route B fallback**:**本来就在进程内用 `_select_context_memories_via_readside`(同一个 selector)→ 不必为了"统一"硬走一次内部 HTTP**。recall 端点主要服务**进程外调用方**(consumer、将来 proactive)。行为等价即可。
- **proactive(里程碑后)**:已有 index/fetch adapter,后续统一到 recall,别成第三套。

## 7. 待 Codex 确认

1. **2 次 enclave 往返**(index 出摘要 + fetch 取正文)在 recall 里可接受吗?M-1 先这样(和 consumer 现状一样),要不要把 selector 推进 enclave 内做成 1 次往返,留作后续优化?
2. route B fallback **保持进程内**(不走 recall HTTP),只确认"用的是同一个 selector、行为一致" —— 同意吗?
3. `top_k` 默认 5 / `limit` 默认沿用 `effective_readside_limit` —— 数值合理吗?(consumer 现状:LIMIT=50、TOP_K=5)
4. 响应里要不要直接给 consumer 一个**拼好的 text block**,还是只给结构化 `items` 让 consumer 自己拼?(我倾向**只给 items**,保持端点纯净、各调用方自己排版。)
