"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------- 导入请求 ----------


class QuoteIn(BaseModel):
    """一条日行情。revision 为修订号，同键高修订覆盖低修订。"""

    security_code: str = Field(..., min_length=1, description="证券代码")
    trade_date: str = Field(..., description="交易日，ISO 日期 YYYY-MM-DD")
    revision: int = Field(..., ge=0, description="修订号（非负整数，越高越新）")
    open: float = Field(..., description="开盘价（原值）")
    high: float = Field(..., description="最高价（原值）")
    low: float = Field(..., description="最低价（原值）")
    close: float = Field(..., description="收盘价（原值）")
    volume: float = Field(..., description="成交量（原值）")

    @field_validator("trade_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("价格必须为正数")
        return v

    @field_validator("volume")
    @classmethod
    def _non_negative_volume(cls, v: float) -> float:
        if v < 0:
            raise ValueError("成交量不能为负")
        return v


class QuoteImportRequest(BaseModel):
    quotes: list[QuoteIn] = Field(..., min_length=1)


class CorporateActionIn(BaseModel):
    """一条拆股/并股公告。

    ratio = 每股老股拆成的新股数：1 拆 2 填 2.0，2 并 1 填 0.5。
    """

    security_code: str = Field(..., min_length=1)
    event_no: str = Field(..., min_length=1, description="事件号")
    ex_date: str = Field(..., description="除权日 YYYY-MM-DD")
    ratio: float = Field(..., gt=0, description="比例：老股 1 股 -> 新股 ratio 股")
    revision: int = Field(..., ge=0, description="修订号（非负整数，越高越新）")
    action_type: Literal["split", "reverse_split"] = Field(
        default="split", description="split=拆股，reverse_split=并股"
    )

    @field_validator("ex_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v


class CorporateActionImportRequest(BaseModel):
    actions: list[CorporateActionIn] = Field(..., min_length=1)


# ---------- 导入响应 ----------


class ImportResponse(BaseModel):
    batch_id: int = Field(..., description="本次导入生成的入库批次号")
    payload_kind: Literal["quotes", "corporate_actions"]
    row_count: int = Field(..., description="实际新写入的行数（完全重复行不计）")
    received: int = Field(..., description="请求中收到的行数")


# ---------- 查询响应 ----------


class EventBasis(BaseModel):
    """折算依据：对某个行情日实际生效的一条公告。"""

    event_no: str
    ex_date: str
    ratio: float = Field(..., description="公告比例（老 1 股 -> 新 ratio 股）")
    action_type: Literal["split", "reverse_split"]
    revision: int
    ingest_batch_id: int = Field(..., description="该公告所在入库批次")


class AdjustedQuote(BaseModel):
    security_code: str
    trade_date: str
    # 原值（该截止批次下生效修订的原始价量）
    open: float
    high: float
    low: float
    close: float
    volume: float
    # 折算值（以区间末日为基准复权）
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    adjusted_volume: float
    factor: float = Field(..., description="价格折算系数 = 1/各公告比例之积")
    applied_events: list[EventBasis] = Field(..., description="事件依据，按除权日升序")
    cutoff_batch_id: int
    basis_end_date: str = Field(..., description="基准日（= 请求区间末日）")


class RawQuote(BaseModel):
    security_code: str
    trade_date: str
    revision: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ingest_batch_id: int


class CorporateActionOut(BaseModel):
    security_code: str
    event_no: str
    ex_date: str
    ratio: float
    action_type: Literal["split", "reverse_split"]
    revision: int
    ingest_batch_id: int


class BatchOut(BaseModel):
    id: int
    payload_kind: Literal["quotes", "corporate_actions"]
    row_count: int
    imported_at: str


# ---------- 等权指数发布 ----------


class RebalanceIn(BaseModel):
    """一次换篮：在指定交易日收盘后生效，下一交易日起按新篮子计算。"""

    trade_date: str = Field(..., description="换篮交易日 YYYY-MM-DD（当日收盘后生效）")
    constituents: list[str] = Field(
        ..., min_length=1, description="新篮子成分证券代码（非空、不重复）"
    )

    @field_validator("trade_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v


class IndexPublishRequest(BaseModel):
    """发布一版等权指数：交易日、成分安排与截止批次在发布时定格。"""

    index_code: str = Field(..., min_length=1, description="指数代码（标签，可多次发版）")
    name: str | None = Field(default=None, description="指数名称")
    trade_dates: list[str] = Field(
        ..., min_length=1, description="有序交易日列表（严格递增，首日定 100 点）"
    )
    initial_constituents: list[str] = Field(
        ..., min_length=1, description="首期成分证券代码（非空、不重复）"
    )
    rebalances: list[RebalanceIn] = Field(
        default_factory=list, description="换篮安排（换篮日收盘后生效）"
    )
    cutoff_batch: int | None = Field(
        default=None,
        description="入库截止批次；缺省=发布时最新已提交批次，定格进本版",
    )

    @field_validator("trade_dates")
    @classmethod
    def _check_dates(cls, v: list[str]) -> list[str]:
        from datetime import date

        for d in v:
            date.fromisoformat(d)
        return v


class MissingQuote(BaseModel):
    """断点处某成分缺失的收盘价（报价日期为前一交易日和/或当日）。"""

    security_code: str
    missing_dates: list[str] = Field(..., description="缺价的报价日期（YYYY-MM-DD）")


class IndexBreakInfo(BaseModel):
    """断点信息：首个算不出的交易日与缺价明细。"""

    first_broken_date: str = Field(..., description="首个算不出点位的交易日")
    missing: list[MissingQuote] = Field(..., description="缺价的证券与报价日期")


class IndexPublicationOut(BaseModel):
    publication_id: int = Field(..., description="发布号（自增，回查凭据）")
    index_code: str
    name: str | None
    cutoff_batch_id: int = Field(..., description="本版定格的入库截止批次")
    status: Literal["complete", "partial"] = Field(
        ..., description="complete=全段可算；partial=缺价截断，只发布断点前点位"
    )
    first_trade_date: str
    last_published_date: str = Field(..., description="最后一个有点位的交易日")
    point_count: int = Field(..., description="已发布点位数（含首日 100 点）")
    break_info: IndexBreakInfo | None = Field(
        default=None, description="断点信息（仅 partial 时给出）"
    )
    created_at: str


class RebalanceOut(BaseModel):
    trade_date: str
    constituents: list[str]


class IndexPublicationDetail(IndexPublicationOut):
    """发布详情：定格的交易日、成分安排原样回查。"""

    trade_dates: list[str]
    initial_constituents: list[str]
    rebalances: list[RebalanceOut]


class IndexPointOut(BaseModel):
    trade_date: str
    level: float = Field(..., description="指数点位（首日 100，5 位小数）")


class IndexPointsResponse(BaseModel):
    """点位子段：与全段同一份已发布数据，任意子段点位一致。"""

    publication_id: int
    index_code: str
    cutoff_batch_id: int
    status: Literal["complete", "partial"]
    start_date: str = Field(..., description="实际返回的区间起日")
    end_date: str = Field(..., description="实际返回的区间末日")
    points: list[IndexPointOut]
    break_info: IndexBreakInfo | None = None
