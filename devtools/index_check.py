"""等权指数端到端冒烟（对真实 uvicorn 进程发 HTTP 请求，需全新库）。

场景（交易日 2026-01-05 .. 01-09，首期 [AAA, BBB]，01-06 收盘后换篮 [AAA, CCC]）：
1. 导入行情（批次1）：AAA 01-07 为 1拆2 除权日（未补报）；CCC 01-09 行情迟到
2. 发布 v1（cutoff=1）：换篮生效，01-07 起用 CCC；无拆股公告，01-07 AAA 表面 -45%
3. 补报 AAA 拆股公告（批次2）-> v1 回查不变
4. 发布 v2（cutoff=2）：01-07 AAA 复权收益 +10%，全段 100/100/100/110
5. 子段查询与全段一致
6. 发布 v3（交易日含 01-09，cutoff=2）：CCC 01-09 缺价 -> 断点截断
7. 补价（批次3）后发 v4 -> complete；v3 原样回查
8. /docs 可访问
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8077"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        sys.exit(1)


def q(code, d, c):
    return {"security_code": code, "trade_date": d, "revision": 0,
            "open": c, "high": c, "low": c, "close": c, "volume": 1000.0}


DAYS4 = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
DAYS5 = DAYS4 + ["2026-01-09"]
REBAL = [{"trade_date": "2026-01-06", "constituents": ["AAA", "CCC"]}]


def publish(days):
    return call("POST", "/index/publications", {
        "index_code": "EQW1", "name": "等权冒烟指数", "trade_dates": days,
        "initial_constituents": ["AAA", "BBB"], "rebalances": REBAL})


def levels(pid, start=None, end=None):
    qs = ""
    if start:
        qs = f"?start_date={start}&end_date={end}"
    s, r = call("GET", f"/index/publications/{pid}/points{qs}")
    assert s == 200, r
    return {p["trade_date"]: p["level"] for p in r["points"]}


# 1. 行情（批次1）：AAA 01-07 除权（60.5 = 除权后价），CCC 01-09 迟到
s, r = call("POST", "/imports/quotes", {"quotes": [
    q("AAA", "2026-01-05", 100.0), q("AAA", "2026-01-06", 110.0),
    q("AAA", "2026-01-07", 60.5), q("AAA", "2026-01-08", 66.55),
    q("AAA", "2026-01-09", 73.205),
    q("BBB", "2026-01-05", 100.0), q("BBB", "2026-01-06", 90.0),
    q("CCC", "2026-01-06", 200.0), q("CCC", "2026-01-07", 180.0),
    q("CCC", "2026-01-08", 198.0),
]})
check("导入行情 201", s == 201 and r["batch_id"] == 1 and r["row_count"] == 10, r)

# 2. 发布 v1（cutoff=1）：换篮生效（01-07 起用 CCC），无拆股公告
s, v1 = call("POST", "/index/publications", {
    "index_code": "EQW1", "trade_dates": DAYS4,
    "initial_constituents": ["AAA", "BBB"], "rebalances": REBAL})
check("发布 v1 201", s == 201 and v1["status"] == "complete"
      and v1["cutoff_batch_id"] == 1 and v1["point_count"] == 4, v1)
got = levels(v1["publication_id"])
# 01-06 旧篮子 [AAA,BBB]：(+10%,-10%)->100；01-07 新篮子 [AAA,CCC]：(-45%,-10%)->72.5
# 01-08：(+10%,+10%)->79.75
check("v1 换篮+未复权点位", got == {"2026-01-05": 100.0, "2026-01-06": 100.0,
                                    "2026-01-07": 72.5, "2026-01-08": 79.75}, got)

# 3. 补报 AAA 1拆2（除权日 01-07）-> 批次2；v1 回查不变
s, r = call("POST", "/imports/corporate-actions", {"actions": [{
    "security_code": "AAA", "event_no": "SP-AAA", "ex_date": "2026-01-07",
    "ratio": 2.0, "revision": 0, "action_type": "split"}]})
check("补报拆股 201", s == 201 and r["batch_id"] == 2, r)
got = levels(v1["publication_id"])
check("v1 不受补报影响", got["2026-01-07"] == 72.5 and got["2026-01-08"] == 79.75, got)

# 4. 发布 v2（cutoff=2）：01-07 AAA 复权收益 = 60.5/(110/2)-1 = +10%
s, v2 = publish(DAYS4)
check("发布 v2 201", s == 201 and v2["cutoff_batch_id"] == 2
      and v2["status"] == "complete", v2)
got = levels(v2["publication_id"])
check("v2 拆股复权点位", got == {"2026-01-05": 100.0, "2026-01-06": 100.0,
                                 "2026-01-07": 100.0, "2026-01-08": 110.0}, got)

# 5. 子段与全段一致
sub = levels(v2["publication_id"], "2026-01-06", "2026-01-07")
check("v2 子段一致", sub == {"2026-01-06": 100.0, "2026-01-07": 100.0}, sub)

# 6. 发布 v3（含 01-09，cutoff=2）：CCC 01-09 缺价 -> 断点截断
s, v3 = publish(DAYS5)
check("v3 partial", s == 201 and v3["status"] == "partial"
      and v3["point_count"] == 4
      and v3["last_published_date"] == "2026-01-08", v3)
info = v3["break_info"]
check("v3 断点信息", info["first_broken_date"] == "2026-01-09"
      and info["missing"] == [{"security_code": "CCC",
                               "missing_dates": ["2026-01-09"]}], info)
got = levels(v3["publication_id"])
check("v3 只发布断点前点位", got == {"2026-01-05": 100.0, "2026-01-06": 100.0,
                                     "2026-01-07": 100.0, "2026-01-08": 110.0}, got)

# 7. 补价（批次3）后发 v4 -> complete；v3 原样回查
s, r = call("POST", "/imports/quotes",
            {"quotes": [q("CCC", "2026-01-09", 217.8)]})
check("补价 201", s == 201 and r["batch_id"] == 3, r)
s, v4 = publish(DAYS5)
check("v4 complete", s == 201 and v4["status"] == "complete"
      and v4["cutoff_batch_id"] == 3 and v4["point_count"] == 5, v4)
got = levels(v4["publication_id"])
check("v4 全段点位", got["2026-01-09"] == 121.0
      and got["2026-01-08"] == 110.0, got)
got = levels(v3["publication_id"])
check("v3 原样回查", got.get("2026-01-09") is None
      and got["2026-01-08"] == 110.0, got)

# 发布清单：4 版，状态各异
s, pubs = call("GET", "/index/publications")
check("发布清单", s == 200 and [p["status"] for p in pubs]
      == ["complete", "complete", "partial", "complete"], pubs)

# /docs
with urllib.request.urlopen(BASE + "/docs") as resp:
    check("/docs 可访问", resp.status == 200 and b"Swagger" in resp.read())

print("\nALL INDEX E2E CHECKS PASSED")
