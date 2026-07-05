# rrr_dotnet_reactor

小型在线授权服务器，面向单软件、少量客户、一个授权绑定一台机器的使用场景。

## 已实现功能

- FastAPI 服务端 + SQLite 数据库。
- 管理员登录后台。
- 授权列表、授权详情、新增卡密页面。
- 全局日志页，可按事件类型和结果筛选最近授权日志。
- 卡密只完整显示一次，数据库仅保存卡密哈希和后四位展示值。
- Hardware ID 仅保存哈希和哈希前缀展示值，不保存原始机器码。
- 授权状态：`unused`、`active`、`expired`、`disabled`、`deleted`。
- 首次激活绑定 Hardware ID，并使用数据库条件更新避免并发请求覆盖机器绑定。
- 启动验证和运行中心跳验证。
- 到期、禁用、硬件不匹配时返回失败原因。
- 续期 30/90/365 天、自定义到期时间、设置/取消永久。
- 禁用、恢复、解绑机器、逻辑删除未激活卡密。
- 授权列表页可直接执行详情、续期、禁用/恢复、解绑、删除未激活卡密。
- 授权日志记录：创建、激活、验证、心跳、续期、禁用、解绑、删除。
- 后台状态变更表单带 CSRF token。

## 客户端接口

### 激活

`POST /api/v1/activate`

```json
{
  "license_key": "XXXX-XXXX-XXXX-XXXX",
  "hardware_id": "client-hardware-id",
  "client_version": "1.0.0"
}
```

### 启动验证

`POST /api/v1/verify`

```json
{
  "license_key": "XXXX-XXXX-XXXX-XXXX",
  "hardware_id": "client-hardware-id",
  "client_version": "1.0.0"
}
```

也可以用 `license_id` 替代 `license_key`：

```json
{
  "license_id": 1,
  "hardware_id": "client-hardware-id",
  "client_version": "1.0.0"
}
```

### 心跳验证

`POST /api/v1/heartbeat`

请求体与启动验证相同。客户端建议每 10 分钟调用一次；服务端只判断授权状态，客户端自行实现“连续心跳失败超过 10 小时退出”的本地策略。

### 通用响应

```json
{
  "success": true,
  "valid": true,
  "code": "ok",
  "message": "授权有效",
  "license_id": 1,
  "status": "active",
  "expire_at": "2026-08-03T12:00:00Z",
  "is_permanent": false,
  "remaining_days": 30,
  "server_time": "2026-07-04T12:00:00Z"
}
```

业务失败时 HTTP 仍返回 200，`success` / `valid` 为 `false`，`code` 返回稳定英文错误码，`message` 返回中文原因。`server_time` 和 `expire_at` 统一为 UTC ISO-8601 `Z` 格式，例如 `2026-07-04T12:00:00Z`。

常见错误码：

- `license_not_found`：卡密或授权不存在
- `already_used`：卡密已被使用
- `disabled`：授权已禁用
- `not_activated`：授权未激活，可由客户端清缓存后重新激活
- `hardware_mismatch`：硬件不匹配
- `expired`：授权已过期
- `deleted`：授权已删除
- `invalid_hardware_id`：Hardware ID 为空或非法

协议错误，例如 `/verify` 缺少 `license_key` 和 `license_id`，返回 HTTP 400，但仍使用同一 JSON 结构：

```json
{
  "success": false,
  "valid": false,
  "code": "missing_license_identifier",
  "message": "license_key 或 license_id 必须提供一个",
  "license_id": null,
  "status": null,
  "expire_at": null,
  "is_permanent": false,
  "remaining_days": null,
  "server_time": "2026-07-04T12:00:00Z"
}
```

## 本地运行

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.lock.txt
uvicorn app.main:app --reload
```

打开后台：`http://127.0.0.1:8000/admin/licenses`

全局日志：`http://127.0.0.1:8000/admin/logs`

默认管理员：

- 用户名：`admin`
- 密码：`admin123`

生产环境必须通过环境变量修改默认密码和密钥；设置 `AUTH_ENV=production` 后，默认配置会直接拒绝启动。

## Docker / Linux 部署

Docker 镜像默认以 `AUTH_ENV=production` 和 `AUTH_COOKIE_SECURE=true` 启动，并使用非 root 用户运行。必须显式传入生产密钥和管理员密码，否则容器启动会失败：

```bash
docker build -t reactor-auth-server .
docker run --rm -p 8000:8000 \
  -v reactor-auth-data:/data \
  -e AUTH_SECRET_KEY='replace-with-long-random-secret' \
  -e AUTH_HASH_PEPPER='replace-with-long-random-pepper' \
  -e AUTH_ADMIN_PASSWORD='replace-with-strong-password' \
  reactor-auth-server
```

如果服务位于可信反向代理后，并且需要记录真实客户端 IP，再设置 `AUTH_TRUST_PROXY_HEADERS=true`。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTH_DB_PATH` | `./authorization.db` | SQLite 文件路径 |
| `AUTH_SECRET_KEY` | `change-this-secret-before-production` | 后台登录 Cookie 签名密钥 |
| `AUTH_HASH_PEPPER` | 同 `AUTH_SECRET_KEY` | 卡密和 Hardware ID 哈希 pepper；生产环境设置后不要随意变更 |
| `AUTH_ADMIN_USERNAME` | `admin` | 首个管理员用户名 |
| `AUTH_ADMIN_PASSWORD` | `admin123` | 首个管理员密码 |
| `AUTH_SESSION_COOKIE` | `auth_admin_session` | 后台 Cookie 名称 |
| `AUTH_SESSION_MAX_AGE_SECONDS` | `604800` | 登录有效期 |
| `AUTH_ENV` | `development` | 设置为 `production` 时会拒绝默认 secret、hash pepper 和管理员密码 |
| `AUTH_COOKIE_SECURE` | `false` | 设置为 `true` 后后台登录 Cookie 添加 Secure 标记；生产环境必须为 `true` |
| `AUTH_TRUST_PROXY_HEADERS` | `false` | 设置为 `true` 后才信任 `X-Forwarded-For`，仅在可信反向代理后开启 |

后台所有状态变更 POST 表单都带 CSRF token；缺少或错误 token 会返回 403。

`AUTH_ENV` 只能是 `development` 或 `production`；布尔变量只能是 `true`、`false`、`1`、`0`。生产环境会强制要求 `AUTH_COOKIE_SECURE=true`。

当前版本只支持 SQLite。SQLite 连接初始化时会启用 `foreign_keys=ON`、`busy_timeout=5000` 和 WAL journal mode。

## 数据库表

- `admin_users`：管理员账号。
- `licenses`：授权主表。
- `license_logs`：授权事件日志。

启动时会自动建表；如果数据库中还没有管理员，会按环境变量创建首个管理员。

## .NET Reactor 使用建议

本服务端按“纯在线授权”实现，不依赖 Reactor license 文件作为主授权判断。推荐使用 Reactor 做：

- Anti Tamper
- Control Flow Obfuscation
- String Encryption
- Resource Encryption
- Anti Debug
- Anti Decompile

C# 客户端应在启动和运行中请求本服务端，由服务端根据 Hardware ID、状态和到期时间决定是否允许继续运行。

不要把以下内容放进客户端：

- Master Key
- LicenseGen.dll
- 服务器签名私钥
- 数据库连接信息
- 后台账号

如果后续确实需要生成 Reactor license 文件，LicenseGen.dll 和 Master Key 也应只放在可信服务器侧。

## 测试

```bash
pytest
```
