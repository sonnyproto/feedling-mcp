# 聊天文件上传（后端支持）设计 v1 — 2026-07-10

对接 iOS PR feedling-mcp-ios#72（已 merge）：客户端已按
`content_type=file` + `file_name` + `file_mime` + `file_byte_count` + `file_b64`
发送文件消息，后端目前在 `chat_core` 被 `content_type must be 'text' or 'image'` 拒掉。
本设计补齐后端这一半。

## Scope

- **只做"当轮可读"**：文件 = 一条 user turn，agent 当场读内容回复。
  与 image 现有行为完全对齐（PR 72 契约注释同款："seal the bytes as one user turn"）。
- 不做文件库 / 后续重新下载 / 文件内容进 capture 记忆管线。

## 架构原则：完全复制 image 管线

文件 bytes 原样过管道，**服务端永远不解析内容**（VPS 客户端加密路由因此天然兼容）；
consumer 解密后按 agent 能力适配。不新建管线，只在 image 的每个环节旁边加 `file` 分支。

Image 现有链路（file 逐段照抄）：

```
iOS payload → hosted/turn.py:_model_api_image_payload（校验）
→ hosted/chat_send_core.py（bytes 封信封，mime 存明文 extra，caption 单独封信封）
→ append_chat(content_type="image") → 唤醒 consumer
→ enclave/routes/chat.py（ctype=="image" 分支吐 image_b64/image_mime，caption 解出填 content）
→ tools/chat_resident_consumer.py（落盘 /tmp/feedling_chat_images/，按 agent 入口适配：
   CLI=精确 Read 指令；OpenAI-compat HTTP=multimodal block；不支持=诚实降级话术）
```

## 类型策略（v1 最终版）

判定以**扩展名 + 内容嗅探**为准（MIME 由 iOS 猜测、不可信，仅作参考）。

| 组 | 判定 | 处理 |
|---|---|---|
| 图片 jpg/png/webp/**gif** | image/* mime 或扩展名 | **转入现有 image 管线**（`content_type=image`，享受视觉）；image mime 白名单加 `gif`（Claude 视觉原生支持 jpeg/png/gif/webp 四种） |
| 图片 heic | — | 400 + `hint: 转码 jpeg 后再发`。任何模型都不直读 heic，后端转码要加 pillow-heif 依赖，不值；客户端如何转码归 iOS 侧设计（见"能力契约"节） |
| PDF | `.pdf` | 落盘原文件，Claude CLI Read 原生支持（分页读） |
| Word | `.docx` | consumer 用 `zipfile`（标准库，零新依赖）抽 `word/document.xml` 纯文本 → 落 `.txt`；抽失败落原文件 + 诚实降级 |
| Excel | `.xlsx` | consumer 用 `zipfile` 抽 `xl/sharedStrings.xml` + `xl/worksheets/sheet*.xml` → 拼 TSV 落 `.txt`；**截断保护**：只抽前 5 张表、每表前 2000 行，截断时文末注明"已截断，共 X 行" |
| 纯文本（md/txt/csv/json/一切源码/配置） | **内容嗅探**：UTF-8 可解码 + 无 NUL 字节 | 落盘原文件名，agent Read 直读。不维护扩展名清单——一条规则覆盖全部源码，且天然挡住可执行/压缩包等二进制 |
| 拒绝 | `.doc` `.xls`（OLE 老二进制）、嗅探不过的其他二进制 | 400 + 机器可读 detail（提示另存为 .docx/.xlsx）；iOS 已有 `chat.attachment.error` 弹窗兜话术 |

现状备注：image 白名单目前是 jpeg/jpg/png/webp（`turn.py:1755`），但 iOS 发图路径在
客户端统一转码 JPEG（≤400KB）再发，后端实际只见过 jpeg——heic 相册照片一直是 iOS 转的。

## 大小上限

后端 `MODEL_API_MAX_FILE_BYTES = 10_000_000`（10MB），超了 413。

理由：iOS PR 72 放了 25MB，但 25MB → b64 后 ~33MB JSON body，还要封信封、进库、
每次 enclave 解密史全量吐回（image 侧已有"图片史 payload 可达数 MB"的抱怨注释），
25MB 会放大十倍。10MB 覆盖绝大多数文档。413 detail 带 `max_bytes`，客户端提示语
如何对齐归 iOS 侧设计（本次不动 iOS）。

## 改动点清单

| 文件 | 改动 |
|---|---|
| `backend/hosted/turn.py` | 新增 `_model_api_file_payload()`：类型判定（图片转 image / 白名单 / 嗅探）、b64 校验、10MB 上限、文件名清洗（照 consumer 现有 `[^A-Za-z0-9_.-]` 思路，防路径穿越）。image mime 白名单加 gif |
| `backend/hosted/chat_send_core.py` | file 分支：bytes 封信封，`file_mime`/`file_name` 存明文 extra（拍板：文件名明文，同 image_mime 档，iOS 历史列表不解密也能显示气泡），`content_type="file"`；随附文字走现有 caption 信封机制；image/* 直接转 `content_type=image` |
| `backend/chat/chat_core.py:305,395` | content_type 白名单加 `"file"`；**VPS 路由 append_chat 要把 payload 里的 `file_name`/`file_mime` 收进 extra**（现在该路径不传 extra，image 没此需求但 file 有） |
| `backend/enclave/routes/chat.py` | `ctype=="file"` 分支：吐 `file_b64` + `file_mime` + `file_name`（照 image 分支，含 caption） |
| `tools/chat_resident_consumer.py` | `_file_paths_for_msg()`：落盘 `/tmp/feedling_chat_files/`（**保留原扩展名**，Read 靠扩展名识别 PDF；顺手修 image 落盘 webp 被存成 `.jpg` 的扩展名瑕疵）；docx/xlsx 抽文本；CLI 给精确 Read 指令（照抄 image 那段防编造措辞）；无文字时 placeholder "User sent a file."；HTTP 入口见下节 |

## VPS 兼容（硬约束）

- **服务端看不到明文** → 一切内容解析（docx/xlsx 抽取、嗅探后的落盘决策）都在
  consumer/agent 侧。服务端只做入口校验（cloud 路由收到的是明文 payload 才能校验；
  VPS 路由信封不透明，只能校验 content_type 和 extra 元数据，类型拒绝靠 iOS 客户端
  在加密前自行执行同一套白名单——见"能力契约"节）。
- consumer 是 host/VPS **同一份** `chat_resident_consumer.py`，改一处两边都有；
  但 **VPS resident-runtime 的 consumer 是手动 pin commit 部署的，上线要 bump**。
- enclave 解密史是 hosted 和 VPS resident 共用路径，`enclave/routes/chat.py` 改一次全覆盖。
- agent 沙箱禁网（codex 路径）不影响本功能：文件已落本地盘，Read 是本地读。
- 碰信封 → 按红线，上 test 后必须**真实部署 e2e**（md + pdf + docx + xlsx + 图片文件各发一个，agent 回复能引用内容），本地 fake-decrypt 只验流程。

## 低智模型兼容（硬约束）

- **OpenAI-compat HTTP 入口**（deepseek 等，无文件系统无工具）：
  - 纯文本 / docx / xlsx 抽出的文本 → **直接内联进 prompt**，
    设 `FILE_INLINE_MAX_CHARS`（默认 ~30k 字符）截断 + 文末注明，防小上下文模型爆窗。
  - PDF 无法内联 → 诚实降级一行："此 connector 暂不支持读 PDF"（照视觉降级话术）。
  - 图片文件走 image 管线原有逻辑（multimodal block / 视觉降级）。
- **CLI 入口 + 弱模型**：指令必须死简单、确定性——这正是 docx/xlsx 选"consumer 抽好
  文本落 .txt"而不是"agent 自己 Bash unzip 现场发挥"的原因（image 管线既定哲学：
  consumer 适配 payload，agent 不即兴；且 HTTP 入口根本没有 Bash）。
  Read 指令照抄 image 的防编造措辞：读这个精确路径 / 读不到就明说 / 不许假装读过。
- 每轮单文件（iOS 本来就一次发一个），不给弱模型多文件组合任务。

## 提示词拼接（文件元数据必须说全）

原则：**永远报原始文件名**（用户视角的那个）；凡系统动过手脚的事实
（docx/xlsx 抽取、截断）全部显式声明——弱模型不会自己推理出".txt 是 docx 抽的"，
不声明就会在回复里穿帮（"你发的这个 txt…"）或拿半张表当全量下结论。

CLI 入口模板（防编造措辞照 image 现有那段）：

```
用户在 IO Chat 发来一个文件：
- 文件名：<原始文件名>
- 类型：<友好类型标签>（[若抽取] 已由系统抽取为纯文本，原始格式/图片未保留）
- 大小：<KB/MB>
- 本地路径：<落盘绝对路径>

用 Read 工具读上面这个精确路径后再回复。读不到就直说，
不要假装读过、不要编造文件内容。
[若截断] 注意：抽取文本在 <N> 行处截断，原表共 <X> 行。
```

HTTP 入口模板（内联）：

```
[用户发来文件「<原始文件名>」（<类型>，<大小>），以下是抽取的纯文本内容，
原始格式未保留[若截断]，在 <N> 字符处截断：]
<<<文件内容>>>
[文件内容结束。请基于以上内容回复用户。]
```

用户随附文字（caption）照 image 现有机制解出来放正文，文件块跟在其后。

## 错误处理

- 类型不在白名单 / 超 10MB / b64 无效 → 400/413 + 机器可读 detail，iOS 弹窗。
- docx/xlsx 抽取失败 → 不报错（内容已收下），落原文件，agent 按降级话术明说读不了。
- 信封/解密失败 → 走 image 现有 per-item error 路径，不动。

## 测试

- 单测照 `test_chat_resident_consumer_image.py` / `test_asgi_hosted_chat_send.py`
  克隆 file 版：docx/xlsx 抽取（含截断）、嗅探（UTF-8 过 / NUL 拒）、白名单拒绝、
  大小 413、文件名清洗、HTTP 内联截断、image/* 转管线。
- flow trace：`route.decided` detail 加 `has_file`（对齐 `has_image`）。
- 真实部署 e2e 见 VPS 节。

## 对客户端暴露的能力契约（iOS 端本次不动，之后专门设计对接）

后端只负责把能力和边界暴露清楚；客户端如何消费（转码、提示语、预检）归 iOS 侧
未来的专门设计，本次不派活、不改 iOS 代码。

**Cloud 路由 `POST /v1/model_api/chat/send`：**

- 入参：`content_type=file` + `file_name` + `file_mime` + `file_byte_count` + `file_b64`
  （PR ios#72 已发的形状，原样接受）+ 可选 `message`（caption）。
- 接受类型：图片 jpeg/png/webp/gif（转入 image 管线）、`.pdf`、`.docx`、`.xlsx`、
  任何 UTF-8 纯文本（源码/md/csv/json…）。
- 拒绝（400，机器可读 detail）：
  - `unsupported_file_type` + `hint`：heic（建议客户端转码 jpeg）、`.doc`/`.xls`
    （建议另存新格式）、其他二进制。
  - `invalid_file`：b64 无效 / 空文件。
- 413：超 `MODEL_API_MAX_FILE_BYTES`（10MB），detail 带 `max_bytes`
  （客户端当前 25MB 提示与此不一致，由 iOS 侧设计自行对齐）。

**VPS 路由 `POST /v1/chat/send`（客户端封信封）：**

- `content_type=file` 放行；信封明文 = 文件原始 bytes。
- payload 顶层带明文元数据 `file_name`/`file_mime`（同 image_mime 档），后端收进 extra。
- 服务端看不见内容 → 类型/大小预检只能在客户端加密前做，规则同上表；
  后端对信封不透明体不做内容校验（漏网的二进制由 consumer 侧嗅探兜底降级，不报错）。
