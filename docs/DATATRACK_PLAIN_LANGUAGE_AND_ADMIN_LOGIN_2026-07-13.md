# Spec — data-track 大白话改造 + app 使用时长 + admin 密码登录门

Status: DRAFT for claude↔codex (2026-07-13). Seven 要求两件本轮做完,推 test 后他手动并 main。
Owners: claude = data_track.py 渲染/文案(我的域) ; codex = db.py 聚合 SQL + routes_asgi.py 密码鉴权。
双签后我做整体验收 + 测试 + 推 test。

## 背景(为什么改)

Seven 看不懂面板黑话("已激活/原始行"),且"注册 509"误导:
- `注册 = users 表行数`,每次重装/换机/抹机 → iOS 静默重注册新号、旧号孤儿化不删 → 灌水,**非人数**。
- 账户删除是硬删(`db.delete_user` → `DELETE FROM users`),无 tombstone → **没有也无法有"已删除账户数"**。
- 真实口径:激活 203 / 发消息 192 / onboarding done 116 / 24h 活跃 48(prod 实测,总 509)。

---

## 【CLAUDE · data_track.py 渲染,已完成】

- 头条指标改「激活用户」,「注册」降级为「累计注册行(含重装孤儿·非人数)」;全部指标中英黑话→大白话。
- 头部加 `.note-box` 解释:激活 vs 注册、无"已删除账户"指标。
- 漏斗 "注册" → "注册行(含重装孤儿·非人数)"。
- 新增「App 使用时长」总览区 + `_fmt_duration_sec` 秒→"1h30m"。读 `summary["app_usage"]`,无数据优雅降级。

## 【CODEX · db.py 聚合,待做】

**D1. `admin_data_track_snapshot`(db.py ~446)** — 每用户加 app_session_end 聚合
- tracking events 在 `user_logs` 表 `stream='tracking_events'`;事件 `type='app_session_end'`,时长在 `payload->>'duration_sec'`(int)。
- 现有 :697 已按 type 聚合出 `tracking_by_type`;顺手加每用户:
  - `session_foreground_sec` = `SUM((payload->>'duration_sec')::int) FILTER (WHERE type='app_session_end')`
  - `session_count` = `COUNT(*) FILTER (WHERE type='app_session_end')`
  - `session_last_at` = 最近一次 app_session_end 的 ts(可选,给用户行展示)
- 挂到每个 user 的 snapshot dict,键名: `app_usage: {foreground_sec, sessions, last_at}`。

**D2. summary rollup(data_track.py 汇总循环 ~1131 或 db 层)** — 我在 data_track 渲染读 `summary["app_usage"]`,契约:
```
summary["app_usage"] = {
  "foreground_sec_total": int,   # 所有用户前台秒数总和
  "sessions_total": int,         # app_session_end 事件总数
  "avg_session_sec": int|float,  # foreground_sec_total / sessions_total(sessions_total>0 时)
  "users_active": int,           # 有 >=1 次 session 的用户数
  "dau_today": int,              # 今日(服务器 ingest 时区 Asia/Shanghai)有 >=1 次 session 的用户数
}
```
- 无 app_session_end 时 `sessions_total=0`,我这边已降级显示"暂无事件"。
- 按 iOS 文档(feedling-mcp-ios/Docs/analytics-app-session-end.md)注意:无客户端时间戳→按服务器 ingest 时间分天;前台被杀漏报→我文案已注"略偏低估";无 session_id→不强去重。
- rollup 放 data_track 汇总循环里(读每用户 snapshot 的 app_usage 累加)还是 db 层聚合你定,只要最终 summary 里有上面 5 个键即可。**data_track.py 由我改,你若要在汇总循环里加累加请把补丁给我,避免撞文件。**

## 【CODEX · routes_asgi.py 密码登录门,待做】

**D3. admin 密码 cookie 会话**(routes_asgi.py + admin_core 如需)
- 新增 env `FEEDLING_ADMIN_PASSWORD`（由部署 secret 注入，实际值**不入 git**）。
- `GET /admin/login`:密码表单页(HTML)。可放 data_track.py 渲染由我写,或你就地简单内联——你定;若要我写登录页 HTML 说一声。
- `POST /admin/login`:`hmac.compare_digest(supplied, FEEDLING_ADMIN_PASSWORD)` → 种签名 cookie `admin_session`:
  - 值 = HMAC-SHA256 签名(payload 含过期 epoch),密钥用现有服务端 secret(如 `FEEDLING_RUNTIME_TOKEN_SECRET` 或 `FEEDLING_ADMIN_TOKEN` 派生),**不要把明文 token/password 放 cookie**。
  - 属性:`HttpOnly; Secure; SameSite=Lax; Max-Age=604800`(7 天)。
- `_require_admin`(routes_asgi.py:45)扩展为三选一:合法 `admin_session` cookie **或** 现有 `X-Admin-Token`/`Bearer`/`admin_key`(**保留!** Seven 明确要,我查 trace 的脚本靠它)。
- `GET /admin/logout`:清 cookie → 跳登录页。
- 校验失败/无密码配置:401 到登录页(带错误提示),不要泄露是"密码错"还是"未配置"。
- 页面链接去 key:cookie 鉴权后 data_track 生成的 href/hidden field 不再带 `admin_key`(这块 href 生成在 data_track.py,**我来去**;你只需保证 cookie 鉴权通)。

## 测试(各写,我总验收)

- codex:snapshot app_session_end 聚合(含空/多事件/时长求和/今日 DAU)真实 PG 用例;密码登录 200/401、cookie 签名校验、过期、保留 key 通道、logout 的路由级 e2e(可复用 test_bootstrap_gates backend fixture 或 asgi client)。
- claude:`_fmt_duration_sec` 单测;渲染读 `app_usage` 有/无数据两路;去 key href。
- 我最后跑合并套件 + `/admin/data-track` 真 HTTP 冒烟(带 cookie 与带 key 两种),再推 test。

## 交付顺序
codex 先给 D1/D2 契约字段 + D3;我并行收尾 data_track(去 key href + 登录页 HTML 若归我 + 单测)。两边完 → 我整体审查 + 测试 + 推 test → 告知 Seven 打开 test track 看。
