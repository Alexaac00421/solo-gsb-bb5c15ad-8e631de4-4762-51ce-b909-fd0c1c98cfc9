# 公交换乘票价清分服务（Bus Transfer Fare Clearing）

基于 **Python 3.12 / FastAPI / PostgreSQL** 的公交乘车记录清分服务。
支持换乘分组、组费取最高、账户当日封顶、迟到记录与跨日更正的原子重算、
冲正流水以及游标分页查询。

---

## 1. 一条命令启动

需要安装 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

该命令会：

1. 构建 API 镜像；
2. 启动 PostgreSQL 16（数据持久化在命名卷 `pgdata`）；
3. 等待数据库健康检查通过；
4. 启动 API，并在启动时**自动建表初始化**（`app/schema.sql`，幂等）。

停止 / 清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

---

## 2. 访问地址与端口

| 用途 | 地址 |
| --- | --- |
| 服务根路径 | <http://127.0.0.1:8000/> |
| 健康检查 | <http://127.0.0.1:8000/health> |
| Swagger 交互文档 | <http://127.0.0.1:8000/docs> |
| OpenAPI 描述 | <http://127.0.0.1:8000/openapi.json> |
| PostgreSQL（发布到宿主机） | `127.0.0.1:5432` |

端口可通过环境变量调整：`API_PORT`（默认 `8000`）、`POSTGRES_PORT_PUBLISH`（默认 `5432`）。

---

## 3. 配置项

所有配置通过环境变量注入（`docker-compose.yml` / `.env`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_HOST` | `db` | 数据库主机（Compose 内服务名） |
| `POSTGRES_PORT` | `5432` | 数据库端口 |
| `POSTGRES_DB` | `fare` | 数据库名 |
| `POSTGRES_USER` | `fare` | 数据库用户 |
| `POSTGRES_PASSWORD` | `farepass` | 数据库密码 |
| `API_PORT` | `8000` | 发布到宿主机的 API 端口 |
| `POSTGRES_PORT_PUBLISH` | `5432` | 发布到宿主机的 PG 端口 |
| `DAILY_CAP_CENTS` | `2000` | 账户每运营日实收封顶（分） |
| `TRANSFER_GAP_MINUTES` | `90` | 相邻两程归为同一换乘组的最大间隔（分钟） |
| `DB_CONNECT_RETRIES` | `60` | 启动时等待数据库的重试次数（每秒一次） |

可复制 `.env.example` 为 `.env` 后按需修改。

---

## 4. 业务规则

* **运营日界**：每日北京时间 **04:00** 切日。03:59 上车属于前一运营日，04:00 起属于当日。
* **日内排序**：按 `(上车时刻, 乘次号)` 升序。
* **换乘分组**：相邻两程上车时刻间隔 **≤ 90 分钟** 归入同一换乘组（链式：只与上一程比较）；
  比较范围天然限定在单个运营日内，**绝不跨日合组**。
* **组费**：组费 = 组内最高基础票价（不取和）。
* **日实收**：组费之和与 `DAILY_CAP_CENTS`（默认 2000 分）取较小值。
* **幂等**：同乘次号 + 同账户 + 同上车时刻（同一时刻，时区表达不同也算相同）+ 同票价
  的重传直接返回既有记录（HTTP 200，`idempotent=true`），不重算、不记账。
* **冲突**：同乘次号但内容不同返回 `409 TRIP_CONTENT_CONFLICT`。
* **更正/撤销**：凭乘次号与调用方看到的 `expected_version` 进行乐观并发控制；
  版本陈旧返回 `409 STALE_VERSION`；每次成功更正/撤销版本号 +1。
* **迟到记录 / 跨日更正**：自动重算所有受影响运营日（更正跨 04:00 时同时重算旧日与新日）；
  当实收金额发生变化时，先冲销旧日金额（负数 `reversal`），再记入新金额（正数 `charge`）；
  若封顶持续生效、实收不变，则不产生冗余流水。
* **原子性**：乘次状态、日结、换乘组、流水在单事务内提交；
  同账户写入通过 PostgreSQL 事务级咨询锁串行化。
* **流水不可变**：`journals` 表上的触发器禁止 `UPDATE/DELETE/TRUNCATE`；
  `seq` 为全局单调递增序列（按提交顺序），已返回流水不会被改写。
* **分页**：`GET /journals?after=<seq>&limit=n` 游标翻页，保证不重不漏。
* **时间输入**：接受 ISO-8601；无时区时间按北京时间解释；非法时间（含超出 2000–2100 年范围）
  与负票价返回 `422`。

---

## 5. HTTP API

### 5.1 健康检查

```
GET /health -> 200 {"status":"ok","db":"up"}
```

### 5.2 提交乘次

```
POST /trips
```

请求体：

```json
{
  "account_id": "acct-001",
  "trip_no": "BJ20261010A001",
  "board_time": "2026-10-10T08:00:00+08:00",
  "base_fare": 200
}
```

* 首次记录：`201`；同号同内容重传：`200 {"idempotent": true}`；同号异内容：`409`。
* 响应包含受影响运营日 `recalculated_dates` 与本事务新产生的 `journals`。

### 5.3 更正乘次

```
POST /trips/{trip_no}/correct
```

```json
{ "expected_version": 1, "board_time": "2026-10-10T09:00:00+08:00", "base_fare": 450 }
```

* 未知乘次 `404`；已撤销 `409 TRIP_REVOKED`；版本不符 `409 STALE_VERSION`。
* 跨 04:00 更正时两个运营日都会重算并体现在响应流水里。

### 5.4 撤销乘次

```
POST /trips/{trip_no}/revoke
```

```json
{ "expected_version": 2 }
```

撤销后重算当日；若当日仍有其他乘次则重新分组结算，否则实收归零。

### 5.5 查询单个乘次

```
GET /trips/{trip_no}
```

返回当前版本、状态（`active`/`revoked`）、运营日等。未知乘次 `404`。

### 5.6 查询账户运营日

```
GET /accounts/{account_id}/operating-days/{op_date}     # op_date=YYYY-MM-DD
```

返回该日全部换乘组（含组成乘次、组费）、`raw_fare`（未封顶合计）、
`charged_fare`（实收）、封顶值与乘次数；无数据时 `status="empty"`。

### 5.7 游标查询流水

```
GET /journals?after=0&limit=50&account_id=acct-001
```

* `after`：游标，取上一页响应中的 `next_cursor`，首页传 `0`；
* `limit`：每页条数（1–500，默认 50）；
* `account_id`：可选过滤；
* 响应：`items`、`next_cursor`（本页最后一条 seq）、`has_more`（是否还有下一页）。

### 5.8 错误格式

```json
{ "error": { "code": "STALE_VERSION", "message": "expected_version 1 is stale; current version is 2" } }
```

| HTTP | code |
| --- | --- |
| 404 | `TRIP_NOT_FOUND` |
| 409 | `TRIP_CONTENT_CONFLICT` / `STALE_VERSION` / `TRIP_REVOKED` |
| 422 | `VALIDATION_ERROR` / `INVALID_BOARD_TIME` |

---

## 6. 调用示例

启动服务后任选其一：

```bash
# curl 版（需要本机 curl + python3 做格式化）
bash examples/curl-demo.sh

# 纯标准库 Python 版
python3 examples/api_examples.py
```

最小 curl 速查：

```bash
# 提交
curl -X POST http://127.0.0.1:8000/trips -H 'Content-Type: application/json' \
  -d '{"account_id":"acct-001","trip_no":"A001","board_time":"2026-10-10T08:00:00+08:00","base_fare":200}'

# 查运营日
curl http://127.0.0.1:8000/accounts/acct-001/operating-days/2026-10-10

# 翻流水
curl 'http://127.0.0.1:8000/journals?account_id=acct-001&after=0&limit=50'
```

---

## 7. 本地开发与测试

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 纯业务规则单元测试（无需数据库）
python tests/run_unit_tests.py
# 或
pytest tests/test_fare_rules.py

# 完整集成测试：自动拉起免 root 的用户态 PostgreSQL（pgserver）
pytest
# 也可指定外部数据库：
TEST_DATABASE_DSN="host=localhost dbname=fare_test user=fare password=farepass" pytest
```

直接运行（自行准备 PostgreSQL）：

```bash
POSTGRES_HOST=127.0.0.1 uvicorn app.main:app --reload
```

---

## 8. 项目结构

```
app/
  config.py          # 环境变量配置
  timeutil.py        # 北京时间与 04:00 运营日界
  fare.py            # 纯规则：换乘分组、组费、封顶
  schema.sql         # 建表与 append-only 触发器（启动自动执行）
  db.py              # psycopg 异步连接池 + schema 引导
  service.py         # 事务、幂等、版本控制、重算、流水
  routers.py         # HTTP 路由
  main.py            # FastAPI 入口与异常处理
examples/            # curl / Python 调用示例
tests/               # 单元 + HTTP 集成 + 并发测试
Dockerfile
docker-compose.yml
```
