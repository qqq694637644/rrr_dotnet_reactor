# rrr_dotnet_reactor

小型在线授权服务器，面向单软件、少量客户、一个授权绑定一台机器的使用场景。

## 已实现功能

- FastAPI 服务端 + SQLite 数据库。
- 管理员登录后台。
- 授权列表、授权详情、新增卡密页面。
- 卡密只完整显示一次，数据库仅保存卡密哈希和后四位展示值。
- 授权状态：`unused`、`active`、`expired`、`disabled`。
- 首次激活绑定 Hardware ID。
- 启动验证和运行中心跳验证。
- 到期、禁用、硬件不匹配时返回失败原因。
- 续期 30/90/365 天、自定义到期时间、设置/取消永久。
- 禁用、恢复、解绑机器、删除未激活卡密。
- 授权日志记录：创建、激活、验证、心跳、续期、禁用、解绑。

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
  "message": "授权有效",
  "license_id": 1,
  "status": "active",
  "expire_at": "2026-08-03T12:00:00",
  "is_permanent": false,
  "remaining_days": 30,
  "server_time": "2026-07-04T12:00:00"
}
```

失败时 HTTP 仍返回 200，`success` / `valid` 为 `false`，`message` 会返回原因，例如：

- `卡密不存在`
- `卡密已被使用`
- `卡密已禁用`
- `授权不存在`
- `授权未激活`
- `硬件不匹配`
- `授权已过期`
- `授权已禁用`

## 本地运行

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开后台：`http://127.0.0.1:8000/admin/licenses`

默认管理员：

- 用户名：`admin`
- 密码：`admin123`

生产环境必须通过环境变量修改默认密码和密钥。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTH_DATABASE_URL` | 空 | SQLAlchemy 数据库地址；为空时使用 SQLite |
| `AUTH_DB_PATH` | `./authorization.db` | SQLite 文件路径 |
| `AUTH_SECRET_KEY` | `change-this-secret-before-production` | 后台登录 Cookie 签名密钥 |
| `AUTH_HASH_PEPPER` | 同 `AUTH_SECRET_KEY` | 卡密和 Hardware ID 哈希 pepper；生产环境设置后不要随意变更 |
| `AUTH_ADMIN_USERNAME` | `admin` | 首个管理员用户名 |
| `AUTH_ADMIN_PASSWORD` | `admin123` | 首个管理员密码 |
| `AUTH_SESSION_COOKIE` | `auth_admin_session` | 后台 Cookie 名称 |
| `AUTH_SESSION_MAX_AGE_SECONDS` | `604800` | 登录有效期 |

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
