"""正規化後的資料模型。所有 adapter 最後都要轉成這些格式，後面的流程才不必認得資料來源。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

Market = Literal["TWSE", "TPEX"]
SecurityType = Literal["stock", "etf", "other"]


class DailyQuote(BaseModel):
    symbol: str
    name: str
    security_type: SecurityType
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    change: Decimal | None
    is_no_compare: bool
    volume: int      # 股
    turnover: int    # 元
    trade_count: int | None


class IndexQuote(BaseModel):
    index_code: str
    close: Decimal | None
    change: Decimal | None
    change_pct: Decimal | None


class MarketBreadth(BaseModel):
    advancers: int | None
    decliners: int | None
    unchanged: int | None
    limit_up: int | None
    limit_down: int | None
    no_trade: int | None
    total_volume: int | None
    total_turnover: int | None
    total_trades: int | None


class InstitutionalFlow(BaseModel):
    symbol: str
    name: str
    foreign_buy: int | None
    foreign_sell: int | None
    foreign_net: int | None
    trust_buy: int | None
    trust_sell: int | None
    trust_net: int | None
    dealer_buy: int | None
    dealer_sell: int | None
    dealer_net: int | None
    total_net: int | None


class MarketDaySnapshot(BaseModel):
    """一個交易日的全市場盤後資料（行情＋指數＋廣度）。"""
    market: Market
    trade_date: date
    quotes: list[DailyQuote]
    indices: list[IndexQuote]
    breadth: MarketBreadth


class InstitutionalDay(BaseModel):
    market: Market
    trade_date: date
    flows: list[InstitutionalFlow]


# ---- 除權息（TWT49U 除權除息計算結果表）--------------------------------------
ActionType = Literal["dividend", "rights", "both"]  # 息 / 權 / 權息


class CorporateAction(BaseModel):
    symbol: str
    name: str
    ex_date: date
    action_type: ActionType
    prev_close: Decimal          # 除權息前收盤價
    ref_price: Decimal           # 除權息參考價
    value: Decimal               # 權值＋息值（元）；正常情況 ≈ prev_close − ref_price
    limit_up: Decimal | None = None     # 官方漲停價格（除權息生效日）
    limit_down: Decimal | None = None   # 官方跌停價格
    open_ref: Decimal | None = None     # 開盤競價基準
    div_ref: Decimal | None = None      # 減除股利參考價（不含現金增資；還原價用這個）


class CorporateActionPeriod(BaseModel):
    """一次查詢區間（通常是一個月）內所有的除權息。區間內沒有的列，重跑時會從資料庫移除。"""
    market: Market
    start: date
    end: date
    actions: list[CorporateAction]


# ---- 公司基本資料與產業別 ----------------------------------------------------
class CompanyProfile(BaseModel):
    symbol: str
    name: str                    # 公司簡稱（與行情表的證券名稱一致，例：台積電）
    industry_code: str           # 官方產業代碼，例：24
    industry_name: str           # 例：半導體業
    listed_date: date | None


class CompanySnapshot(BaseModel):
    market: Market
    as_of: date
    companies: list[CompanyProfile]
