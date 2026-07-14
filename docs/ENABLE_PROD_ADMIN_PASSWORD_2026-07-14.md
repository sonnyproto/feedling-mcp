# 启用 prod admin 面板密码登录（给 @zhihao 志豪）

目标：让 `https://api.feedling.app/admin/login` 能用密码登录（和 test 同一个密码），
不用再在 URL 带 admin_key。

## 现状 / 为什么现在不行
- 登录**代码已经在 prod**（`/admin/login`、`_sign_admin_session` 读 `FEEDLING_ADMIN_PASSWORD`），无需改代码、无需重编镜像。
- 但**密码这套配置当初只给 test 加了**（test 用 secret `TEST_FEEDLING_ADMIN_PASSWORD`）。**prod 的 compose、部署 job、GitHub secret 都没有 `FEEDLING_ADMIN_PASSWORD`**，所以 prod POST 密码现在返回 401。
- 密钥通过 `phala deploy -e`（加密 env 通道）在**部署那一刻**注入 CVM，TEE CVM 无法给运行中进程加 env → **必须在下一次 prod 部署时带上**。

## 要做的 3 件事（部署 prod 时一起做）

### 1) 新建 prod GitHub secret（Repo Settings → Secrets and variables → Actions → New repository secret）
- Name：`FEEDLING_ADMIN_PASSWORD`（**不带 TEST_ 前缀**，prod job 用无前缀名）
- Value：`enclave-enclave-lantern-7945`
- ⚠️ 只放 GitHub Secret，别提交进仓库。

### 2) `deploy/docker-compose.phala.yaml`（prod compose）加 env 占位
在 backend 服务 env 里、`FEEDLING_ADMIN_TOKEN` 那行（约 174 行）**下面**加一行：
```yaml
      FEEDLING_ADMIN_TOKEN: "${FEEDLING_ADMIN_TOKEN:-}"
      FEEDLING_ADMIN_PASSWORD: "${FEEDLING_ADMIN_PASSWORD:-}"    # ← 新增
```
（照抄 test 的 `deploy/docker-compose.phala.test.yaml:181`。）

### 3) `.github/workflows/ci.yml` 的 **`deploy-cvm`（prod）job** 加两处
这个 job 大约在 294 行起。参照它现有的 `FEEDLING_ADMIN_TOKEN` 两处，**紧挨着各加一行**：

- **env: 块**（约 480 行 `FEEDLING_ADMIN_TOKEN: ${{ secrets.FEEDLING_ADMIN_TOKEN }}` 下面）：
```yaml
          FEEDLING_ADMIN_TOKEN: ${{ secrets.FEEDLING_ADMIN_TOKEN }}
          FEEDLING_ADMIN_PASSWORD: ${{ secrets.FEEDLING_ADMIN_PASSWORD }}   # ← 新增
```
- **phala deploy 的 -e 参数**（约 543 行 `-e "FEEDLING_ADMIN_TOKEN=$FEEDLING_ADMIN_TOKEN" \` 下面）：
```bash
            -e "FEEDLING_ADMIN_TOKEN=$FEEDLING_ADMIN_TOKEN" \
            -e "FEEDLING_ADMIN_PASSWORD=$FEEDLING_ADMIN_PASSWORD" \
```
⚠️ 只改 **`deploy-cvm`（prod）** 这个 job；`deploy-test-cvm`（约 725-786 行）已经有了，别动。
⚠️ 别改到 **runner CVM** job——runner 不需要 admin 密码（DEPLOYMENTS 有说明）。

### 4) 部署
2、3 改了 compose → compose_hash 变 → prod `deploy-cvm` 会 `phala deploy` 一次，把 `FEEDLING_ADMIN_PASSWORD` 注入 prod backend CVM（in-place 同 CVM，短暂 cycle）。
- secret（第 1 步）**必须在这次部署之前设好**，否则注入的是空值、密码仍 401。

## 验证（部署后）
```bash
# 应从 401 变成 303（登录成功、种 cookie）
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://api.feedling.app/admin/login \
  --data-urlencode "password=enclave-enclave-lantern-7945" --data-urlencode "next=/admin/data-track"
```

## 备注
- 期间 admin_key 直链一直可用：`https://api.feedling.app/admin/data-track?admin_key=<FEEDLING_ADMIN_TOKEN>`。
- 安全：这是共享的面板密码（能看全体用户 metadata）。密码只放 GitHub Secret；如需更强控制可再加 IP allowlist。
- 若不想改 ci.yml/compose，也可让 claude 先把第 2、3 步的仓库改动通过 test→main 预置好，届时你只需设 secret + 部署（但那次 main 合并会触发 prod 部署，需你确认时机）。
