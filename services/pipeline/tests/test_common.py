from datetime import date
from decimal import Decimal

from radar.normalizers.common import (
    parse_decimal, parse_int, parse_roc_date, parse_sign, parse_ymd, security_type,
)


def test_parse_numbers():
    assert parse_int("45,210,000") == 45210000
    assert parse_int("-2,050,000") == -2050000
    assert parse_int("--") is None
    assert parse_int("") is None
    assert parse_decimal("1,005.00") == Decimal("1005.00")
    assert parse_decimal("X0.50") == Decimal("0.50")
    assert parse_decimal('="0050"') == Decimal("50")


def test_parse_sign_html():
    assert parse_sign("<p style= color:red>+</p>") == (1, False)
    assert parse_sign("<p style= color:green>-</p>") == (-1, False)
    assert parse_sign("<p>X</p>") == (0, True)
    assert parse_sign("<p> </p>") == (0, False)


def test_dates():
    assert parse_roc_date("115/09/24") == date(2026, 9, 24)
    assert parse_roc_date("115年07月01日") == date(2026, 7, 1)     # TWT49U 資料日期
    assert parse_roc_date("1150924") == date(2026, 9, 24)          # OpenAPI 出表日期
    assert parse_ymd("20260924") == date(2026, 9, 24)


def test_security_type():
    assert security_type("2330") == "stock"
    assert security_type("0050") == "etf"
    assert security_type("00878") == "etf"
    assert security_type("00679B") == "etf"
    assert security_type("2881A") == "other"
