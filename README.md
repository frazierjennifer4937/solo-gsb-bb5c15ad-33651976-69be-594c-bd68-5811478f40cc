# 实验室样本混样追踪 API（Lab Sample Mixing Tracker）

基于 FastAPI + SQLite 的样本混样/ pooling 追踪服务。客户端提交样本的初始体积、标记物浓度以及按时间排序的转移操作，服务在**体积守恒**约束下逐步计算每次转移后的：

- 各样本的当前体积与**混合标记物浓度**（按来源体积加权的平均值）
- 各样本的**来源占比**（当前体积分别来自哪个初始样本、各占多少）
- 每一步的总体积 / 总标记物质量（用于核对守恒）

转移量超出当前余量、时间逆序、引用未声明样本时，返回 **HTTP 422** 及定位明确的错误（第几个转移、哪个字段、上下文数值）。所有运行（含失败）都持久化到 SQLite，可随时查询。

无需任何第三方账号；一条命令即可启动。

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后访问：

- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

SQLite 数据保存在名为 `labtrack-data` 的卷中（容器内路径 `/data/labtrack.db`），容器重建后记录不丢失。停止服务：`docker compose down`（加 `-v` 会一并删除数据卷）。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest            # 运行测试
uvicorn app.main:app --reload --port 8000
```

本地运行时数据库默认写入 `./data/labtrack.db`，可用环境变量 `DATABASE_PATH` 覆盖。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/simulations` | 提交并执行一次模拟；成功返回 `201` + 完整结果，业务校验失败返回 `422` + 定位错误（失败记录同样入库） |
| `GET` | `/api/v1/simulations/{run_id}` | 按 id 查询某次运行的完整记录（请求、逐步结果或错误） |
| `GET` | `/api/v1/simulations` | 分页列出所有运行（`limit` / `offset`，按创建时间倒序） |
| `GET` | `/health` | 健康检查 |

### 提交模拟：`POST /api/v1/simulations`

```bash
curl -s -X POST http://localhost:8000/api/v1/simulations \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "pooling-demo",
    "samples": [
      {"id": "A", "initial_volume_ul": 1000.0, "marker_concentration_ng_per_ul": 2.0},
      {"id": "B", "initial_volume_ul":  500.0, "marker_concentration_ng_per_ul": 0.8},
      {"id": "C", "initial_volume_ul":    0.0, "marker_concentration_ng_per_ul": 0.0}
    ],
    "transfers": [
      {"time": "2026-09-18T09:00:00Z", "source": "A", "destination": "C", "volume_ul": 200.0},
      {"time": "2026-09-18T09:05:00Z", "source": "B", "destination": "C", "volume_ul": 100.0},
      {"time": "2026-09-18T09:10:00Z", "source": "C", "destination": "A", "volume_ul": 150.0}
    ]
  }'
```

约定：

- 体积单位为微升（µL），浓度单位为 ng/µL，字段名中已标明。
- 所有参与转移的样本（含空的接收管）都必须先在 `samples` 中声明；`initial_volume_ul` 可为 0。
- `time` 为 ISO 8601 时间；不带时区时按 UTC 处理。转移必须按时间**非递减**排列。
- 同源样本不可向自身转移（`source != destination`），`volume_ul` 必须为正数。

### 成功响应（`201`）

响应即持久化的运行记录，核心在 `result`：

```jsonc
{
  "run_id": "9f1c…",
  "status": "completed",
  "created_at": "2026-09-18T10:00:00+00:00",
  "request": { /* 原样回显请求 */ },
  "result": {
    "initial": {                      // 初始快照
      "total_volume_ul": 1500.0,
      "total_marker_ng": 2400.0,
      "samples": [ /* 各样本状态 */ ]
    },
    "steps": [                        // 每次转移后的快照
      {
        "transfer": {"index": 1, "time": "…", "source": "B", "destination": "C", "volume_ul": 100.0},
        "snapshot": {
          "total_volume_ul": 1500.0,  // 每步总体积恒定 → 体积守恒
          "total_marker_ng": 2400.0,
          "samples": [
            {
              "id": "C",
              "volume_ul": 300.0,
              "marker_concentration_ng_per_ul": 1.6,
              "sources": [
                {"source_id": "A", "volume_ul": 200.0, "fraction": 0.666667},
                {"source_id": "B", "volume_ul": 100.0, "fraction": 0.333333}
              ]
            }
          ]
        }
      }
    ],
    "final": { /* 最后一次转移后的快照 */ }
  },
  "errors": null
}
```

空样本（体积为 0）的 `marker_concentration_ng_per_ul` 为 `null`。数值输出保留 6 位小数。

### 业务错误响应（`422`）

```jsonc
{
  "detail": "Simulation failed domain validation; see 'errors' for located issues",
  "run_id": "7b3e…",                 // 失败记录同样入库，可凭此 id 查询
  "errors": [
    {
      "code": "INSUFFICIENT_VOLUME",
      "message": "transfer #0 requests 1500.0 uL from sample 'A' but only 1000.0 uL is available",
      "transfer_index": 0,            // 第几个转移（从 0 计）
      "field": "volume_ul",           // 出问题的字段
      "context": {"source": "A", "requested_volume_ul": 1500.0, "available_volume_ul": 1000.0}
    }
  ]
}
```

| `code` | 含义 | 定位信息 |
| --- | --- | --- |
| `UNKNOWN_SAMPLE` | 转移引用了未在 `samples` 中声明的样本 | `transfer_index` + `field`（`source`/`destination`）+ `context.sample_id`；一次报出所有此类问题 |
| `TIME_OUT_OF_ORDER` | 转移时间早于前一条转移 | `transfer_index` + `context`（本次与前次时间）；一次报出所有此类问题 |
| `INSUFFICIENT_VOLUME` | 转移量超过来源样本**当前**余量（按之前所有转移逐步推算） | `transfer_index` + `context`（请求量 / 当前可用量） |

请求体格式/约束不合法（如体积为负、样本 id 重复、源与目标相同）由 Pydantic 校验，同样返回 `422`，结构为 FastAPI 默认的 `{"detail": [{"loc": …, "msg": …}]}`。

### 查询结果

```bash
curl -s http://localhost:8000/api/v1/simulations            # 列表（?limit=&offset= 分页）
curl -s http://localhost:8000/api/v1/simulations/<run_id>   # 单条完整记录
```

## 计算规则

1. 每个样本的内容被追踪为「来源样本 → 体积」的映射，初始时只含其自身。
2. 一次转移把来源样本中**每个来源按比例**（转移量 / 来源当前体积）移动到目标样本，因此总体积与总标记物质量始终守恒。
3. 任意时刻某样本的混合浓度 = Σ(来源体积 × 来源初始浓度) / 当前体积；来源占比 = 各来源体积 / 当前体积。
4. 转移可行性按**顺序**逐步判定：第 k 次转移的余量检查基于前 k−1 次转移之后的状态。

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由
  models.py    # Pydantic 请求/响应/错误模型
  engine.py    # 体积守恒模拟引擎（纯计算，无框架依赖）
  storage.py   # SQLite 持久化（标准库 sqlite3）
tests/
  test_api.py  # 端到端 API 测试（含浓度/占比数值断言与三类错误定位）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/labtrack.db`，本地 `data/labtrack.db` | SQLite 数据库文件路径 |
