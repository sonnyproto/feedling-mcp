# Privacy 备忘:perception_state 明文存外部 RDS(含健康桶值)

> 状态:**不紧急,记录待修**。2026-06-22 审计(Claude)发现并核实。供工程师评估存储隐私分层方案。
> 这条是"感知数据**存储**隐私",和"resident agent 怎么**调**感知工具"是两条独立线,别混。

## 一句话
**原始精确信号(GPS/BSSID)在端上就丢了、绝不出设备(✓ 很强);但解析后的粗感知状态——包括健康桶值、日历详情——是明文 jsonb 存在外部 AWS RDS(TEE 外)。** 内容信封(聊天/记忆/帧)是密文,安全;perception_state 不是信封、是明文。

## 核实到的事实(含代码定位)

1. **库在 TEE 外,是外部 AWS RDS**(`deploy/DEPLOYMENTS.md`):
   - test:`feedling-mcp-test-...rds.amazonaws.com:5432`,且 **Publicly accessible + SG inbound 5432**。
   - prod:`DATABASE_URL` 注入的外部库。
   - 架构注(同文件):"content-layer envelopes sealed to `enclave_content_pk` 才是隐私边界"——**库本身不是信任边界**,隐私靠信封加密。

2. **`perception_state` 是明文 jsonb**(`backend/perception/store.py`:`STATE = "perception_state"`,写入 `user_blobs.doc`,无加密;`merge_state_guarded` 直接落明文)。与 chat/memory 的"密文信封"不同。

3. **明文落库的字段**(都在外部 RDS):
   - 低敏:`time` / `battery` / `broadcast_state` / `motion_state` / `weather` / `focus(in_focus)` / `now_playing` / `country`
   - 中敏(位置模式):`place_label`(home/work)/ `wifi_label` / `wifi_anchor_id`
   - **高敏**:`calendar_next_event`(标题/时间/人数)、`audio_route.device_name`、**`health_sleep / health_workout / health_vitals` 桶值(睡眠分钟/静息心率/步数/运动类型)**

4. **确认安全的部分**:
   - V2 里 iOS **端上**就把坐标/BSSID/地址粗化丢弃,只把粗值加密进信封(`PerceptionContextSnapshot.swift`)→ **原始精确信号连 enclave 都不到**。
   - 聊天/记忆/帧仍是密文存库(就算库在外也安全)。
   - enclave `/v1/envelope/decrypt` 把明文返回给**主后端**做 resolve(主后端 + enclave 同在 CVM 内);但 resolve 后的粗状态**持久化进了外部 RDS 明文**——这是 seam 所在。

## 为什么值得记
- 产品隐私卖点是"内容不出信任边界"。内容(聊天/记忆)做到了;但**健康桶值明文在 TEE 外的(且 test 还是公网可达的)RDS**,是这两天新接 weather/health 时**大概率没专门评估过**的存储隐私扩张。健康数据(即便桶化)明文外置,off-brand 且有现实风险。
- 位置标签(home/work)明文是较软的取舍——可辩护为"粗",但泄露仍暴露作息。

## 建议处理(分层,不要全加密也不要全明文)
全加密会给 `perception.now` 等热路径/快档每次 pull 加一次 enclave 往返;全明文把健康放外面。所以分层:

- **A. 留明文(低敏 + 热路径,可接受)**:time/battery/motion/weather/place_label/wifi_label/country/focus/now_playing。**但在隐私披露里写明**:粗在场标签 + 天气明文存服务端。
- **B. 必须封(敏感,且本就慢档、接受延迟)**:
  - **健康桶值(第一优先)**、日历详情、audio_route.device_name。
  - 实现复用现有"**加密信封 + pull 时经 enclave 解密**"模式(memory/frame 那套),别新造 at-rest 加密路径。倾向:**不缓存明文粗值,保留密文信封、按需经 enclave 解**。
  - 做法:给 catalog 的 `Signal` 加 `sensitive` / `persist_plaintext` 标记,ingest 按标记决定"落明文粗值"还是"留密文不落明文"。

- **立刻可做、零架构改动**:**关掉 test RDS 的 public accessibility**(它 internet-facing 且存明文感知含健康,无任何好处)。

### 优先级
1. 现在(无脑收益):关 test RDS 公网访问。
2. 尽快:健康桶值不再明文落库(走 B)。
3. 其次:日历详情 / audio device_name 同样处理。
4. 接受 + 声明:位置标签 + 天气明文(A)。

## 相关代码
- `backend/perception/store.py`(`STATE` 明文 blob)
- `backend/perception/service.py`(`ingest_snapshot_v2` 解密 → `_apply` resolve → `merge_state` 落明文)
- `backend/perception/resolve.py`(各 resolver 产出的粗字段)
- `backend/perception/catalog.py`(`SIGNALS` outputs;若加 `sensitive` 标记在此)
- `backend/core/enclave.py`(`_decrypt_envelope_via_enclave` 返回明文给后端)
- `deploy/DEPLOYMENTS.md`(RDS 位置 + "信封才是隐私边界")
