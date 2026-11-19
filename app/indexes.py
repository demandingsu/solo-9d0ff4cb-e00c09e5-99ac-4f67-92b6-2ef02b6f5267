"""等权指数发布与回查。

规则
====
1. 发布时提交有序交易日、首期成分与换篮安排（换篮日在列表内，当日收盘后生效，
   下一交易日起按新篮子计算）；首日定为 100 点。
2. 每个交易日 d（前一交易日 p）：取 p 收盘后生效的篮子，以 d 为基准日复权
   （除权日满足 ``p < 除权日 <= d`` 的公告折算 p 日收盘价），成分收益率
   ``r_i = 当日收盘 / 复权后前收 - 1``，等权平均后 ``level_d = level_{d-1} * (1 + mean(r_i))``。
   Decimal 计算，点位四舍五入保留 5 位小数。
3. 每版定格交易日、成分安排与入库截止批次：行情与公告只取
   ``ingest_batch_id <= cutoff_batch_id`` 的已提交数据，之后补报不影响本版。
4. 缺收盘价即断点：只发布断点前的点位，记录首个算不出的交易日、
   缺价证券与报价日期；补齐数据后只能发新版（新发布号），旧版原样回查。
5. 点位在发布时算好落库，子段查询只是切片，故任意子段与全段一致。

一致性
======
发布在单个 ``BEGIN IMMEDIATE`` 事务内完成「取截止批次 + 计算 + 落库」，
与导入串行；回查走只读快照事务。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from . import db
from .models import (
    IndexBreakInfo,
    IndexPointsResponse,
    IndexPointOut,
    IndexPublicationDetail,
    IndexPublicationOut,
    IndexPublishRequest,
    MissingQuote,
    RebalanceOut,
)
from .services import QUANT, Rejected, _effective_actions_for_day, _resolve_cutoff

BASE_LEVEL = Decimal("100")  # 首日点位


# ---------------------------------------------------------------- 校验


def _validate_request(req: IndexPublishRequest) -> None:
    dates = req.trade_dates
    if len(set(dates)) != len(dates):
        raise Rejected("交易日列表存在重复日期")
    if dates != sorted(dates):
        raise Rejected("交易日列表必须按升序排列")

    def _check_constituents(codes: list[str], label: str) -> None:
        if any(not c or not c.strip() for c in codes):
            raise Rejected(f"{label}存在空白证券代码")
        if len(set(codes)) != len(codes):
            raise Rejected(f"{label}存在重复证券代码")

    _check_constituents(req.initial_constituents, "首期成分")

    date_set = set(dates)
    seen_rebalance_dates: set[str] = set()
    for rb in req.rebalances:
        if rb.trade_date not in date_set:
            raise Rejected(
                f"换篮日 {rb.trade_date} 不在交易日列表内，换篮无法生效"
            )
        if rb.trade_date in seen_rebalance_dates:
            raise Rejected(f"换篮日 {rb.trade_date} 重复安排")
        seen_rebalance_dates.add(rb.trade_date)
        _check_constituents(rb.constituents, f"换篮日 {rb.trade_date} 成分")


def _basket_at_close(
    initial: list[str], rebalances: list[dict], prev_date: str
) -> list[str]:
    """前一交易日 ``prev_date`` 收盘后生效的篮子（首期或换篮日 <= prev_date 的最近换篮）。"""
    basket = initial
    for rb in rebalances:  # 已按换篮日升序
        if rb["trade_date"] <= prev_date:
            basket = rb["constituents"]
        else:
            break
    return basket


# ---------------------------------------------------------------- 行情取数（截止批次）


def _effective_close(
    conn: sqlite3.Connection, code: str, trade_date: str, cutoff: int
) -> float | None:
    """某证券某交易日、截至 cutoff 批次的生效收盘价；无行情返回 None。"""
    row = conn.execute(
        """
        SELECT q.close
        FROM daily_quotes q
        JOIN (
            SELECT security_code, trade_date, MAX(revision) AS max_rev
            FROM daily_quotes
            WHERE ingest_batch_id <= ?
            GROUP BY security_code, trade_date
        ) m ON m.security_code = q.security_code
           AND m.trade_date = q.trade_date
           AND q.revision = m.max_rev
        WHERE q.ingest_batch_id <= ?
          AND q.security_code = ?
          AND q.trade_date = ?
        """,
        (cutoff, cutoff, code, trade_date),
    ).fetchone()
    return None if row is None else float(row["close"])


def _adjustment_factor(
    conn: sqlite3.Connection, code: str, prev_date: str, cur_date: str, cutoff: int
) -> Decimal:
    """以当日 cur_date 为基准日折算前一交易日价格的系数（1/各公告比例之积）。"""
    factor = Decimal(1)
    for a in _effective_actions_for_day(conn, code, prev_date, cur_date, cutoff):
        factor /= Decimal(str(a["ratio"]))
    return factor


# ---------------------------------------------------------------- 发布


def publish_index(
    req: IndexPublishRequest, db_file: str | None = None
) -> IndexPublicationOut:
    """发布一版等权指数：定格截止批次，算全段点位并落库（单事务）。"""
    _validate_request(req)
    dates = req.trade_dates
    rebalances = sorted(
        ({"trade_date": rb.trade_date, "constituents": list(rb.constituents)}
         for rb in req.rebalances),
        key=lambda r: r["trade_date"],
    )
    initial = list(req.initial_constituents)

    with db.get_conn(db_file) as conn:
        with db.write_transaction(conn):
            cutoff_id = _resolve_cutoff(conn, req.cutoff_batch)

            # 首日 100 点，不需要行情
            points: list[tuple[str, Decimal]] = [(dates[0], BASE_LEVEL)]
            break_info: IndexBreakInfo | None = None

            for prev_date, cur_date in zip(dates, dates[1:]):
                basket = _basket_at_close(initial, rebalances, prev_date)
                returns: list[Decimal] = []
                missing: list[MissingQuote] = []

                for code in basket:
                    close_prev = _effective_close(conn, code, prev_date, cutoff_id)
                    close_cur = _effective_close(conn, code, cur_date, cutoff_id)
                    if close_prev is None or close_cur is None:
                        missing_dates = []
                        if close_prev is None:
                            missing_dates.append(prev_date)
                        if close_cur is None:
                            missing_dates.append(cur_date)
                        missing.append(
                            MissingQuote(security_code=code,
                                         missing_dates=missing_dates)
                        )
                        continue
                    factor = _adjustment_factor(
                        conn, code, prev_date, cur_date, cutoff_id
                    )
                    adj_prev = Decimal(str(close_prev)) * factor
                    returns.append(
                        (Decimal(str(close_cur)) - adj_prev) / adj_prev
                    )

                if missing:
                    # 断点：只发布断点前的点位，记录首个算不出的交易日
                    break_info = IndexBreakInfo(
                        first_broken_date=cur_date, missing=missing
                    )
                    break

                avg_ret = sum(returns) / Decimal(len(returns))
                prev_level = points[-1][1]
                level = (prev_level * (1 + avg_ret)).quantize(
                    QUANT, rounding=ROUND_HALF_UP
                )
                points.append((cur_date, level))

            status = "complete" if break_info is None else "partial"
            cur = conn.execute(
                """
                INSERT INTO index_publications(
                    index_code, name, trade_dates, initial_constituents,
                    rebalances, cutoff_batch_id, status, first_broken_date,
                    missing_quotes, last_published_date, point_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.index_code,
                    req.name,
                    json.dumps(dates),
                    json.dumps(initial),
                    json.dumps(rebalances),
                    cutoff_id,
                    status,
                    break_info.first_broken_date if break_info else None,
                    (json.dumps([m.model_dump() for m in break_info.missing])
                     if break_info else None),
                    points[-1][0],
                    len(points),
                ),
            )
            publication_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO index_points(publication_id, trade_date, level) "
                "VALUES (?, ?, ?)",
                [(publication_id, d, float(lv)) for d, lv in points],
            )
            created_at = conn.execute(
                "SELECT created_at FROM index_publications WHERE id = ?",
                (publication_id,),
            ).fetchone()["created_at"]

    return IndexPublicationOut(
        publication_id=publication_id,
        index_code=req.index_code,
        name=req.name,
        cutoff_batch_id=cutoff_id,
        status=status,
        first_trade_date=dates[0],
        last_published_date=points[-1][0],
        point_count=len(points),
        break_info=break_info,
        created_at=created_at,
    )


# ---------------------------------------------------------------- 回查


def _break_info_from_row(row: sqlite3.Row) -> IndexBreakInfo | None:
    if row["first_broken_date"] is None:
        return None
    missing = [MissingQuote(**m) for m in json.loads(row["missing_quotes"])]
    return IndexBreakInfo(
        first_broken_date=row["first_broken_date"], missing=missing
    )


def _publication_out(row: sqlite3.Row) -> IndexPublicationOut:
    dates = json.loads(row["trade_dates"])
    return IndexPublicationOut(
        publication_id=row["id"],
        index_code=row["index_code"],
        name=row["name"],
        cutoff_batch_id=row["cutoff_batch_id"],
        status=row["status"],
        first_trade_date=dates[0],
        last_published_date=row["last_published_date"],
        point_count=row["point_count"],
        break_info=_break_info_from_row(row),
        created_at=row["created_at"],
    )


def list_publications(db_file: str | None = None) -> list[IndexPublicationOut]:
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        rows = conn.execute(
            "SELECT * FROM index_publications ORDER BY id"
        ).fetchall()
    return [_publication_out(r) for r in rows]


def get_publication(
    publication_id: int, db_file: str | None = None
) -> IndexPublicationDetail:
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        row = conn.execute(
            "SELECT * FROM index_publications WHERE id = ?", (publication_id,)
        ).fetchone()
    if row is None:
        raise Rejected(f"发布号 {publication_id} 不存在", 404)
    base = _publication_out(row)
    return IndexPublicationDetail(
        **base.model_dump(),
        trade_dates=json.loads(row["trade_dates"]),
        initial_constituents=json.loads(row["initial_constituents"]),
        rebalances=[RebalanceOut(**rb) for rb in json.loads(row["rebalances"])],
    )


def get_points(
    publication_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
    db_file: str | None = None,
) -> IndexPointsResponse:
    """按发布号查点位子段；缺省返回全段。子段与全段为同一份已发布数据。"""
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        pub = conn.execute(
            "SELECT * FROM index_publications WHERE id = ?", (publication_id,)
        ).fetchone()
        if pub is None:
            raise Rejected(f"发布号 {publication_id} 不存在", 404)

        start = start_date or json.loads(pub["trade_dates"])[0]
        end = end_date or pub["last_published_date"]
        if start > end:
            raise Rejected("start_date 不能晚于 end_date")

        rows = conn.execute(
            "SELECT trade_date, level FROM index_points "
            "WHERE publication_id = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date",
            (publication_id, start, end),
        ).fetchall()

        out = _publication_out(pub)
    return IndexPointsResponse(
        publication_id=out.publication_id,
        index_code=out.index_code,
        cutoff_batch_id=out.cutoff_batch_id,
        status=out.status,
        start_date=start,
        end_date=end,
        points=[IndexPointOut(trade_date=r["trade_date"], level=r["level"])
                for r in rows],
        break_info=out.break_info,
    )
