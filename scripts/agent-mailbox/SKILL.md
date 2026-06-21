# Skill: 双 Agent 协作(tmux 唤醒 + 本地邮箱)

让**两个终端里的编码 agent**(典型:Codex 当实现者、Claude Code 当审查员)在同一个
仓库里可靠地来回协作。配套参考文档:`docs/AGENT_MAILBOX.md`(机制规格)。本 skill
偏"**怎么搭 + 怎么用 + 怎么不翻车**",工程师照这份就能配起来并上手。

---

## 1. 心智模型(先理解这两层分离,后面才不会用错)

整套东西只有两个职责,**严格分开**:

1. **持久邮箱 = 磁盘上的 `.agents/mailbox/`**。消息的"真身"在这里,落盘、可重读、被
   git 忽略。这是**真相来源**。
2. **tmux `send-keys` = 只是"叫醒"的传输**。它往对方终端**只注入一行固定的读取命令**
   (`Run scripts/agent-mailbox/read.sh ...`),**消息正文永远不经过终端**。

> 一句话:**邮箱负责"信",tmux 只负责"敲门"。** 敲门可能没敲响(对方在忙),但信
> 已经在信箱里了——这是整套设计能可靠的根本原因。

为什么不直接 `tmux send-keys` 把消息打过去?因为终端注入长文本会被换行/转义/焦点
问题搅坏,且无法重读、无法存档。所以正文走磁盘,终端只传一个短命令。

---

## 2. 目录结构

```
.agents/mailbox/                 # git-ignored,本地 scratch
├── config.env                   # 两个 pane 的 tmux 目标(本地,不提交)
├── messages/<id>.md             # 消息真身(canonical)
├── inbox/<agent>/<id>.md        # 收件箱副本(read.sh 读这里)
├── outbox/<agent>/<id>.md       # 发件箱副本
├── archive/<agent>/<id>.md      # ack 后归档到这
└── tmp/
```

脚本(`scripts/agent-mailbox/`,**已提交、可共享**):
- `setup.sh` — 登记两个 agent 的 tmux pane 目标
- `post.sh` — 发消息(写盘 + 唤醒对方)
- `read.sh` — 读消息(list / latest / 指定 id / --all)
- `ack.sh` — 确认并归档

---

## 3. 一次性配置(每台机器、每个 session 搭一次)

### 3.1 两个 agent 各开一个 tmux pane
在 tmux 里把两个 agent 跑起来(同一仓库根目录),例如左边跑 Codex、右边跑 Claude Code。

### 3.2 查 pane 目标并登记
```sh
# 列出所有 pane,找到两个 agent 各自的 target(形如 session:window.pane)
tmux list-panes -a -F '#S:#I.#P #{pane_current_command}'

# 方式 A:一次性登记两个
scripts/agent-mailbox/setup.sh --codex-pane dev:0.0 --claude-pane dev:0.1

# 方式 B:每个 agent 在自己 pane 里登记自己(最省心,不用数 pane)
scripts/agent-mailbox/setup.sh --self codex     # 在 Codex 的 pane 里跑
scripts/agent-mailbox/setup.sh --self claude    # 在 Claude 的 pane 里跑
```
写入 `.agents/mailbox/config.env`(`CODEX_PANE` / `CLAUDE_PANE`),**不提交**。

> pane 目标会变(关窗/重排)。换了布局就重跑一次 `setup.sh`。`post.sh` 发现 pane
> 没配/失效时**仍会把消息写盘**,只是跳过唤醒——对方下次主动 `read.sh --list` 也能看到。

---

## 4. 核心操作

### 4.1 发消息(正文走 stdin / heredoc,绝不走命令行参数)
```sh
scripts/agent-mailbox/post.sh \
  --from codex --to claude \
  --type review_request \
  --subject "PR1 DB substrate ready" <<'EOF'
请审 PR1。重点:
- lease CAS
- stale turn 回收
- 不泄漏 proactive_jobs 状态
EOF
```
长正文建议先写进文件再喂进去(便于复用/修改):
```sh
scripts/agent-mailbox/post.sh --from claude --to codex --type task \
  --subject "Backend: wire X" < /tmp/spec.md
```
`post.sh` 会:① 写 `messages/` + 双方 `inbox/outbox`;② 给对方 pane `send-keys` 一行
唤醒命令 + Enter;③ 打印 `woke <agent> at <pane>` 或 `wake skipped/failed`。

### 4.2 读 + 确认
```sh
scripts/agent-mailbox/read.sh claude --list      # 列收件箱所有 id
scripts/agent-mailbox/read.sh claude latest       # 读最新一条
scripts/agent-mailbox/read.sh claude <id>         # 读指定
scripts/agent-mailbox/ack.sh  claude <id>         # 处理完→归档(真身保留在 messages/)
```

### 4.3 消息类型(`--type`)
`review_request`(求审)· `review_result`(PASS/失败+blocker)· `pushback`(质疑设计/实现)·
`decision`(已定的工程决策)· `status`(进度)· `task`(派活)。保持**短、可执行**。

---

## 5. ⚠️ 操作纪律(实战里最容易翻车,务必照做)

### 5.1 发送方:**必须确认唤醒真的被"提交"了,不是停在输入框**
这是最大的坑。`post.sh` 虽然 `send-keys ... Enter`,但当对方 agent 的 TUI **正在忙**
或输入框有焦点时,那个 Enter 经常**只换行**,或让命令停在输入缓冲(你会看到
`tab to queue message` / `Messages to be submitted after next tool call`)。结果:你以为
发了,对方其实没收到。

**所以每次 `post.sh` 之后,发送方都要回看对方 pane 确认:**
```sh
tmux capture-pane -t "$RECIPIENT_PANE" -p | tail -8
```
判读:
- 看到对方进入 `Working` / 已开始读消息 → ✅ 成功。
- 看到那行读取命令**停在输入行**(提示符 `›` 前)→ 还没提交,补一下:
  ```sh
  tmux send-keys -t "$RECIPIENT_PANE" Enter
  ```
  再 `capture-pane` 复核。
- 看到 `Messages to be submitted after next tool call` / `tab to queue message` → 这是
  **正常的排队态**(对方在忙,做完当前工具调用就会读),不用再动。

> 口诀:**发完必看 capture-pane;没提交就补 Enter;直到确认对方收到。** 别"发了就走"。

### 5.2 接收方:读完先 `ack`,再干活
`ack` 把收件箱副本归档,避免重复处理;canonical 真身仍在 `messages/`,要回溯随时能查。

### 5.3 审查握手(Codex 实现 / Claude 审 的标准回合)
1. Codex 干完 → `post --type review_request`(列改了哪些文件 + 自测结果 + 想让审的不变量)。
2. Claude 读 → 审 diff(隐私/正确性/契约/范围)→ `post --type review_result`:
   - **PASS** + "可以 commit/push" 的**明确指令**(审查员通常不替实现者提交);
   - 或列 blocker,要求改。
3. 不同意 → `--type pushback`,把理由说清,别闷头改。
4. PASS 后由**实现者**commit/push;审查员只审不提交(除非另有约定)。

### 5.4 邮箱 ≠ 仓库历史
邮箱是**本地协作 scratch**。任何正式决策/结论,最终要落进 **commit message / PR / docs
/ CHANGELOG**,不能只躺在邮箱里。两个月后别人靠 git 历史,不靠 `.agents/mailbox/`。

---

## 6. 安全 / 注意事项

- **永不提交 `.agents/mailbox/`**(已 git-ignore;`config.env` 含本地 pane,也不提交)。
- **不要把长正文用 `send-keys` 打进终端**——正文一律走磁盘(stdin/heredoc/文件)。
- **消息里不放密钥**(它是明文本地文件)。
- 对方在忙 → 唤醒会延迟,但消息已落盘,不会丢。
- 跨仓库协作(如 backend + iOS 两个 repo):邮箱建在**主仓库**即可,消息里写清楚改的是
  哪个 repo / 哪个分支,别让对方猜。

---

## 7. 端到端示例(本项目真实用过的一回合)

```sh
# 1) Claude 把后端 spec 写进文件,派给 Codex
scripts/agent-mailbox/post.sh --from claude --to codex --type task \
  --subject "Backend: ingest weather+health signals" < /tmp/spec.md
tmux capture-pane -t "$CODEX_PANE" -p | tail -8        # 确认 Codex 收到/排队

# 2) Codex 实现完,回审
scripts/agent-mailbox/post.sh --from codex --to claude --type review_request \
  --subject "weather/health ingress ready" <<'EOF'
改了 catalog/contract/resolver/ingress/tests;字段名对齐 iOS;PG16 跑 177 passed。
请审:pull-only 不 wake、enclave 解密、字段逐字对齐。
EOF

# 3) Claude 审完 PASS
scripts/agent-mailbox/read.sh claude latest            # 读
# (审 diff …)
scripts/agent-mailbox/ack.sh claude <id>
scripts/agent-mailbox/post.sh --from claude --to codex --type review_result \
  --subject "PASS: commit/push to test" <<'EOF'
PASS。字段对齐、enclave 解密、pull-only 已核。commit 上去,push 到 test。
EOF
tmux capture-pane -t "$CODEX_PANE" -p | tail -8        # 确认 Codex 收到

# 4) Codex commit/push,再 post 一条 status 收尾
```

---

## 8. 排错速查

| 现象 | 原因 / 处理 |
|---|---|
| `wake skipped: CLAUDE_PANE is not configured` | 没跑 `setup.sh` 或 pane 没登记 → 重跑 `setup.sh --self ...` |
| `wake failed` | pane target 失效(关窗/重排)→ `tmux list-panes -a` 重新登记 |
| 发了对方没反应 | 多半是 Enter 没提交 → `capture-pane` 看,停在输入行就补 `send-keys ... Enter` |
| 对方"已读未动" | 它在忙(排队态正常);或它没 ack → 让它 `read.sh <agent> --list` 看积压 |
| 想回溯历史消息 | `messages/<id>.md` 是 canonical;ack 过的在 `archive/<agent>/` |
