"""等权指数自测：发布规则、换篮、拆股复权、断点截断、补价重发、子段一致。"""

from __future__ import annotations

from tests.conftest import action, post_actions, post_quotes, quote

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def publish(client, **overrides):
    body = {
        "index_code": "EQW1",
        "name": "等权测试指数",
        "trade_dates": DAYS,
        "initial_constituents": ["AAA", "BBB"],
        "rebalances": [],
    }
    body.update(overrides)
    return client.post("/index/publications", json=body)


def points_map(resp):
    assert resp.status_code == 200, resp.text
    return {p["trade_date"]: p["level"] for p in resp.json()["points"]}


def test_basic_publication_levels(client):
    """首日 100 点；逐日等权平均收益率递推，点位与手算一致。"""
    # AAA: 100 -> 110 (+10%) -> 115.5 (+5%) -> 115.5 (0%)
    # BBB:  40 ->  44 (+10%) -> 42.24 (-4%) -> 44.352 (+5%)
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),
        quote(code="AAA", date="2026-01-06", close=110.0),
        quote(code="AAA", date="2026-01-07", close=115.5),
        quote(code="AAA", date="2026-01-08", close=115.5),
        quote(code="BBB", date="2026-01-05", close=40.0),
        quote(code="BBB", date="2026-01-06", close=44.0),
        quote(code="BBB", date="2026-01-07", close=42.24),
        quote(code="BBB", date="2026-01-08", close=44.352),
    ]
    r = post_quotes(client, rows)
    assert r.status_code == 201, r.text

    r = publish(client)
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["status"] == "complete"
    assert pub["cutoff_batch_id"] == 1  # 缺省取发布时最新批次并定格
    assert pub["point_count"] == 4
    assert pub["last_published_date"] == "2026-01-08"
    assert pub["break_info"] is None

    pid = pub["publication_id"]
    got = points_map(client.get(f"/index/publications/{pid}/points"))
    assert got == {
        "2026-01-05": 100.0,
        "2026-01-06": 110.0,                    # 100 * (1 + (10%+10%)/2)
        "2026-01-07": 110.55,                   # 110 * (1 + (5%-4%)/2)
        "2026-01-08": 113.31375,                # 110.55 * (1 + (0%+5%)/2)
    }


def test_rebalance_takes_effect_after_close(client):
    """换篮日收盘后生效：换篮日当天仍用旧篮子，次日起用新篮子，点位连续。"""
    # 01-06 收盘后换篮 [AAA, BBB] -> [AAA, CCC]
    # AAA: 01-05..01-08 每日 +10%；BBB 只有前两天（若误用旧篮子会断点）
    # CCC: 200 -> 180 (-10%) -> 198 (+10%)
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),
        quote(code="AAA", date="2026-01-06", close=110.0),
        quote(code="AAA", date="2026-01-07", close=121.0),
        quote(code="AAA", date="2026-01-08", close=133.1),
        quote(code="BBB", date="2026-01-05", close=100.0),
        quote(code="BBB", date="2026-01-06", close=90.0),   # 旧篮子成员，-10%
        quote(code="CCC", date="2026-01-06", close=200.0),
        quote(code="CCC", date="2026-01-07", close=180.0),  # 新篮子成员，-10%
        quote(code="CCC", date="2026-01-08", close=198.0),  # +10%
    ]
    assert post_quotes(client, rows).status_code == 201

    r = publish(
        client,
        rebalances=[{"trade_date": "2026-01-06",
                     "constituents": ["AAA", "CCC"]}],
    )
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["status"] == "complete"  # 换篮后不再依赖 BBB 行情

    got = points_map(
        client.get(f"/index/publications/{pub['publication_id']}/points")
    )
    # 01-06（换篮日当天，仍旧篮子）：(+10% + -10%)/2 = 0 -> 100
    # 01-07（新篮子）：(+10% + -10%)/2 = 0 -> 100
    # 01-08（新篮子）：(+10% + +10%)/2 = +10% -> 110
    assert got == {
        "2026-01-05": 100.0,
        "2026-01-06": 100.0,
        "2026-01-07": 100.0,
        "2026-01-08": 110.0,
    }


def test_split_adjusted_on_ex_date(client):
    """除权日（1 拆 2）当天收益率按当日基准复权：前收折算为 100/2=50。"""
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),  # 除权前
        quote(code="AAA", date="2026-01-06", close=60.0),   # 除权后（+20%）
        quote(code="BBB", date="2026-01-05", close=50.0),
        quote(code="BBB", date="2026-01-06", close=55.0),   # +10%
    ]
    assert post_quotes(client, rows).status_code == 201
    assert post_actions(
        client, [action(code="AAA", ex="2026-01-06", ratio=2.0)]
    ).status_code == 201

    r = publish(client, trade_dates=["2026-01-05", "2026-01-06"])
    assert r.status_code == 201, r.text
    got = points_map(
        client.get(f"/index/publications/{r.json()['publication_id']}/points")
    )
    # AAA 复权收益 = 60/(100/2) - 1 = 20%；等权平均 (20%+10%)/2 = 15%
    assert got == {"2026-01-05": 100.0, "2026-01-06": 115.0}


def test_late_announcement_keeps_old_version_unchanged(client):
    """发布后补报公告：旧版（定格截止批次）点位不变，新版才反映公告。"""
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),
        quote(code="AAA", date="2026-01-06", close=60.0),   # 表面 -40%
        quote(code="BBB", date="2026-01-05", close=50.0),
        quote(code="BBB", date="2026-01-06", close=55.0),   # +10%
    ]
    assert post_quotes(client, rows).status_code == 201

    # 第 1 版：无公告，AAA 收益 = 60/100 - 1 = -40%，平均 (-40%+10%)/2 = -15%
    r = publish(client, trade_dates=["2026-01-05", "2026-01-06"])
    assert r.status_code == 201
    v1 = r.json()
    assert v1["cutoff_batch_id"] == 1

    # 补报 AAA 1 拆 2（除权日 01-06）-> 批次 2
    assert post_actions(
        client, [action(code="AAA", ex="2026-01-06", ratio=2.0)]
    ).status_code == 201

    # 旧版原样回查：点位与成分安排不变
    got_v1 = points_map(
        client.get(f"/index/publications/{v1['publication_id']}/points")
    )
    assert got_v1 == {"2026-01-05": 100.0, "2026-01-06": 85.0}
    detail = client.get(f"/index/publications/{v1['publication_id']}").json()
    assert detail["cutoff_batch_id"] == 1
    assert detail["initial_constituents"] == ["AAA", "BBB"]

    # 新版（缺省取最新批次 2）：AAA 复权收益 +20%，平均 +15%
    r = publish(client, trade_dates=["2026-01-05", "2026-01-06"])
    assert r.status_code == 201
    v2 = r.json()
    assert v2["publication_id"] != v1["publication_id"]
    assert v2["cutoff_batch_id"] == 2
    got_v2 = points_map(
        client.get(f"/index/publications/{v2['publication_id']}/points")
    )
    assert got_v2 == {"2026-01-05": 100.0, "2026-01-06": 115.0}

    # 旧版再次回查，仍然 85.0（未被改写）
    again = points_map(
        client.get(f"/index/publications/{v1['publication_id']}/points")
    )
    assert again["2026-01-06"] == 85.0


def test_missing_close_truncates_at_break(client):
    """缺收盘价：只发布断点前点位，给出首个算不出的交易日、缺价证券与报价日期。"""
    # BBB 缺 01-07 收盘 -> 01-07 算不出；01-06 可算
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),
        quote(code="AAA", date="2026-01-06", close=110.0),
        quote(code="AAA", date="2026-01-07", close=121.0),
        quote(code="BBB", date="2026-01-05", close=50.0),
        quote(code="BBB", date="2026-01-06", close=55.0),
    ]
    assert post_quotes(client, rows).status_code == 201

    r = publish(client, trade_dates=DAYS[:3])
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["status"] == "partial"
    assert pub["point_count"] == 2
    assert pub["last_published_date"] == "2026-01-06"
    info = pub["break_info"]
    assert info["first_broken_date"] == "2026-01-07"
    assert info["missing"] == [
        {"security_code": "BBB", "missing_dates": ["2026-01-07"]}
    ]

    got = points_map(
        client.get(f"/index/publications/{pub['publication_id']}/points")
    )
    assert got == {"2026-01-05": 100.0, "2026-01-06": 110.0}

    # 旧版定格：即使 BBB 的 01-07 行情随后被补入，旧版点位也不变
    assert post_quotes(
        client, [quote(code="BBB", date="2026-01-07", close=60.5)]
    ).status_code == 201
    again = points_map(
        client.get(f"/index/publications/{pub['publication_id']}/points")
    )
    assert again == {"2026-01-05": 100.0, "2026-01-06": 110.0}


def test_republish_after_backfill(client):
    """补齐缺价后可发新版走全段；旧版照原样回查。"""
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),
        quote(code="AAA", date="2026-01-06", close=110.0),
        quote(code="BBB", date="2026-01-05", close=50.0),
        quote(code="BBB", date="2026-01-06", close=55.0),
    ]
    assert post_quotes(client, rows).status_code == 201

    r = publish(client, trade_dates=DAYS[:3])
    v1 = r.json()
    assert v1["status"] == "partial"
    assert v1["break_info"]["first_broken_date"] == "2026-01-07"

    # 补齐 AAA/BBB 的 01-07 行情（新批次）后重发
    assert post_quotes(
        client,
        [quote(code="AAA", date="2026-01-07", close=121.0),
         quote(code="BBB", date="2026-01-07", close=60.5)],
    ).status_code == 201
    r = publish(client, trade_dates=DAYS[:3])
    v2 = r.json()
    assert v2["publication_id"] != v1["publication_id"]
    assert v2["status"] == "complete"
    assert v2["point_count"] == 3
    got_v2 = points_map(
        client.get(f"/index/publications/{v2['publication_id']}/points")
    )
    assert got_v2 == {
        "2026-01-05": 100.0,
        "2026-01-06": 110.0,
        "2026-01-07": 121.0,  # 两日收益均 +10%
    }

    # 两版都在列表里，旧版状态原样
    pubs = client.get("/index/publications").json()
    assert [p["publication_id"] for p in pubs] == [
        v1["publication_id"], v2["publication_id"],
    ]
    assert pubs[0]["status"] == "partial"
    assert pubs[1]["status"] == "complete"


def test_subsection_matches_full_section(client):
    """按发布号查任意子段：点位与全段一致。"""
    rows = [
        quote(code="AAA", date="2026-01-05", close=100.0),
        quote(code="AAA", date="2026-01-06", close=110.0),
        quote(code="AAA", date="2026-01-07", close=115.5),
        quote(code="AAA", date="2026-01-08", close=115.5),
        quote(code="BBB", date="2026-01-05", close=40.0),
        quote(code="BBB", date="2026-01-06", close=44.0),
        quote(code="BBB", date="2026-01-07", close=42.24),
        quote(code="BBB", date="2026-01-08", close=44.352),
    ]
    assert post_quotes(client, rows).status_code == 201
    pid = publish(client).json()["publication_id"]

    full = points_map(client.get(f"/index/publications/{pid}/points"))
    sub = client.get(
        f"/index/publications/{pid}/points",
        params={"start_date": "2026-01-06", "end_date": "2026-01-07"},
    )
    assert sub.status_code == 200
    body = sub.json()
    assert body["start_date"] == "2026-01-06"
    assert body["end_date"] == "2026-01-07"
    sub_map = {p["trade_date"]: p["level"] for p in body["points"]}
    assert sub_map == {d: full[d] for d in ("2026-01-06", "2026-01-07")}


def test_validation_errors(client):
    """交易日无序/重复、成分重复、换篮日越界或重复、截止批次不存在。"""
    post_quotes(client, [quote(code="AAA", date="2026-01-05", close=100.0)])

    assert publish(
        client, trade_dates=["2026-01-06", "2026-01-05"]
    ).status_code == 422
    assert publish(
        client, trade_dates=["2026-01-05", "2026-01-05"]
    ).status_code == 422
    assert publish(
        client, initial_constituents=["AAA", "AAA"]
    ).status_code == 422
    assert publish(
        client,
        rebalances=[{"trade_date": "2026-02-01", "constituents": ["AAA"]}],
    ).status_code == 422
    assert publish(
        client,
        rebalances=[
            {"trade_date": "2026-01-06", "constituents": ["AAA"]},
            {"trade_date": "2026-01-06", "constituents": ["BBB"]},
        ],
    ).status_code == 422
    assert publish(client, cutoff_batch=99).status_code == 404
    # pydantic 层：空交易日、空成分
    assert publish(client, trade_dates=[]).status_code == 422
    assert publish(client, initial_constituents=[]).status_code == 422

    # 未产生任何发布
    assert client.get("/index/publications").json() == []


def test_publish_on_empty_db_and_missing_publication(client):
    assert publish(client).status_code == 404  # 尚无入库批次
    assert client.get("/index/publications/1").status_code == 404
    assert client.get("/index/publications/1/points").status_code == 404


def test_points_subsection_bad_range(client):
    post_quotes(client, [quote(code="AAA", date="2026-01-05", close=100.0)])
    pid = publish(client, trade_dates=["2026-01-05"]).json()["publication_id"]
    r = client.get(
        f"/index/publications/{pid}/points",
        params={"start_date": "2026-01-08", "end_date": "2026-01-05"},
    )
    assert r.status_code == 422
