# 拆股复权日行情与等权指数 API

基于 **FastAPI + SQLite** 的版本化行情服务：导入带修订号的日开高低收/成交量与
拆股（并股）公告，按**入库截止批次**返回拆股前后可比较的复权日行情；
并可在同一套数据上**发布等权指数**：交易日、成分安排与截止批次在发布时定格，
缺价只发布断点前点位，补齐后另发新版。后来补报或更正的公告**不会改写此前
已交付的批次与指数版本**。

## 业务规则

### 版本化导入（只追加，从不改写历史行）

| 规则 | 行为 |
| --- | --- |
| 同业务键（行情=证券+交易日，公告=证券+事件号）仅最高修订号生效 | 查询只取最高修订 |
| 同修订号但值不同 | **整批拒收（422）**，事务回滚，不产生批次 |
| 收到修订号低于库中已生效修订 | **整批拒收（422，"过期修订"）** |
| 同修订同值重复提交 | 幂等跳过，不写新行 |
| 每次成功导入 | 同一事务内生成一个**入库批次**（自增批次号） |

### 截止批次复权查询 `GET /quotes/adjusted`

- 以请求的**区间末日 `end_date` 为基准日**；
- 只使用**截至 `cutoff_batch` 已入库**（行所在批次 ≤ 截止批次）的公告与行情；
- 对行情日 `d`，生效公告满足 `d < 除权日 ≤ end_date`（除权日当天不折算当日行情）；
- `ratio` 表示老 1 股 → 新 `ratio` 股：**1 拆 2 填 `2.0`（价格减半、成交量加倍）**，
  2 并 1 填 `0.5`（价格翻倍、成交量减半）；多笔公告比例连乘；
- 返回原值、折算值（Decimal 计算后四舍五入保留 5 位小数）、折算系数与**事件依据**
  （事件号、除权日、比例、类型、修订号、所在批次）。

### 并发与一致性

- SQLite WAL 模式 + `BEGIN IMMEDIATE` 写事务 + 30s `busy_timeout`：写事务串行，
  读不阻塞；并发导入同键时唯一索引兜底，恰好一个成功，其余整批回滚；
- 每次查询在单个只读快照事务内完成，**价量与事件依据来自同一截止批次下的已提交状态**，
  读不到查询期间新提交的批次；
- 失败导入整体回滚：不留行、不留批次。

### 等权指数发布 `POST /index/publications`

一次发布提交：**有序交易日列表、首期成分、换篮安排**（换篮日须在列表内，
当日收盘后生效，下一交易日起按新篮子计算）与可选 `cutoff_batch`
（缺省=发布时最新已提交批次）。每版**定格**交易日、成分安排与截止批次，
行情及公告只取 `ingest_batch_id <= cutoff_batch` 的已提交数据。

计算规则：

1. 首日定为 **100 点**；
2. 每个交易日 d（前一交易日 p）：取 p 收盘后生效的篮子（首期或换篮日 ≤ p 的
   最近一版），以 **d 为基准日复权**（除权日满足 `p < 除权日 <= d` 的公告折算
   p 日收盘价），成分收益率 = 当日收盘 / 复权后前收 − 1，**等权平均**后
   `level_d = level_{d-1} × (1 + 平均收益率)`；Decimal 计算，点位保留 5 位小数；
3. **缺少所需收盘价**：该收益日算不出即停，只发布断点前的点位，版本标记
   `partial`，并给出**首个算不出的交易日、缺价证券与报价日期**；
4. **补齐后可发新版**（新发布号，重新定格截止批次）；**旧版照原样回查**，
   点位落库后不再变化；
5. 按发布号查**任意子段** `GET /index/publications/{id}/points?start_date&end_date`：
   与全段同一份已发布数据，点位必然一致。

```bash
# 发布一版等权指数（01-06 收盘后换篮 AAA,BBB -> AAA,CCC）
curl -X POST localhost:8000/index/publications -H 'Content-Type: application/json' -d '{
  "index_code": "EQW1", "name": "等权示例",
  "trade_dates": ["2026-01-05", "2026-01-06", "2026-01-07"],
  "initial_constituents": ["AAA", "BBB"],
  "rebalances": [{"trade_date": "2026-01-06", "constituents": ["AAA", "CCC"]}]}'

# 回查全段 / 子段
curl 'localhost:8000/index/publications/1/points'
curl 'localhost:8000/index/publications/1/points?start_date=2026-01-06&end_date=2026-01-07'
```

## 启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs ，在 Swagger UI 直接导入、查询
```

SQLite 数据文件位于命名卷 `market-data` 的 `/data/market.db`。

## 本地开发（无 Docker）

```bash
python3 -m venv --without-pip .venv
python3 -I get-pip.py                 # 如系统没有 pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -I -m pytest -q      # 14 项自测
MARKET_DB_PATH=./data/market.db .venv/bin/python -I -m uvicorn app.main:app --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/imports/quotes` | 导入日行情，成功返回 `batch_id` |
| POST | `/imports/corporate-actions` | 导入拆股/并股公告，成功返回 `batch_id` |
| GET | `/quotes/adjusted` | 复权行情（原值+折算值+事件依据），参数 `security_code/start_date/end_date/cutoff_batch` |
| GET | `/quotes/raw` | 原值日行情（截止批次下生效修订） |
| GET | `/corporate-actions` | 公告查询（每事件号生效修订） |
| GET | `/batches` | 入库批次清单 |
| POST | `/index/publications` | 发布等权指数（定格交易日、成分安排、截止批次；缺价时 partial + 断点信息） |
| GET | `/index/publications` | 全部发布清单（状态、断点） |
| GET | `/index/publications/{id}` | 发布详情（定格的交易日、成分安排、截止批次） |
| GET | `/index/publications/{id}/points` | 按发布号回查点位，支持 `start_date/end_date` 子段 |
| GET | `/health` | 健康检查 |

### 请求示例

```bash
# 1. 导入行情（批次 1）
curl -X POST localhost:8000/imports/quotes -H 'Content-Type: application/json' -d '{
  "quotes": [{"security_code": "600001", "trade_date": "2026-01-02",
              "revision": 0, "open": 100, "high": 102, "low": 98,
              "close": 100, "volume": 1000}]}'

# 2. 补报 1 拆 2 公告（批次 2，除权日 2026-01-05）
curl -X POST localhost:8000/imports/corporate-actions -H 'Content-Type: application/json' -d '{
  "actions": [{"security_code": "600001", "event_no": "SP-001",
               "ex_date": "2026-01-05", "ratio": 2.0, "revision": 0,
               "action_type": "split"}]}'

# 3. 按批次 1 查询 -> 补报不可见，折算值=原值；不传 cutoff_batch 则取最新批次
curl 'localhost:8000/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10&cutoff_batch=1'
```

## 目录结构

```
app/            FastAPI 应用（db 连接/建表、models 校验、services 行情逻辑、
                indexes 等权指数、main 路由）
tests/          24 项 pytest 自测（复权规则、补报不可变、修订、拒收回滚、并发；
                指数：手算点位、换篮、拆股复权、断点截断、补价重发、子段一致）
devtools/       端到端与并发压力脚本（对真实 uvicorn 进程发 HTTP 请求；
                index_check.py 覆盖换篮/拆股/补价全链路）
Dockerfile / docker-compose.yml / requirements.txt
```
