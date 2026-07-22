from pathlib import Path

import pytest

from scripts.import_watchlist import merge_watchlist, parse_issue_body, parse_rows


def test_mixed_import_normalizes_and_deduplicates():
    rows = parse_rows(
        "600519,贵州茅台,stock\n025209,永赢先锋半导体智选混合C,fund\n600519.SH,,stock",
        "text",
    )
    merged = merge_watchlist({"version": 1, "assets": []}, rows)

    assert [asset["code"] for asset in merged["assets"]] == ["600519.SH", "025209"]
    assert [asset["asset_type"] for asset in merged["assets"]] == ["stock", "fund"]


def test_import_rejects_more_than_fifty():
    with pytest.raises(ValueError, match="50"):
        parse_rows("\n".join(f"{600000 + index},x,stock" for index in range(51)), "text")


def test_issue_requires_machine_block():
    with pytest.raises(ValueError, match="WATCHLIST_IMPORT_V1"):
        parse_issue_body("普通 issue 内容")


def test_csv_fixture_accepts_chinese_aliases_and_emits_complete_schema():
    text = (Path(__file__).parents[1] / "fixtures" / "watchlist-import.csv").read_text(
        encoding="utf-8"
    )

    assets = parse_rows(text, "csv")

    assert assets[0]["code"] == "000001.SZ"
    assert assets[0]["asset_type"] == "stock"
    assert assets[1]["asset_type"] == "fund"
    assert set(assets[0]) == {
        "id",
        "code",
        "name",
        "asset_type",
        "market",
        "sector",
        "note",
        "enabled",
    }


def test_issue_block_parses_only_machine_readable_assets():
    assets = parse_issue_body(
        "请导入\n<!-- WATCHLIST_IMPORT_V1\n"
        '{"assets":[{"code":"00700.HK","name":"腾讯","asset_type":"股票"}]}'
        "\n-->"
    )

    assert assets == [
        {
            "id": "stock-hk-00700-hk",
            "code": "00700.HK",
            "name": "腾讯",
            "asset_type": "stock",
            "market": "HK",
            "sector": "",
            "note": "",
            "enabled": True,
        }
    ]


def test_etf_import_keeps_six_digit_fund_code():
    asset = parse_rows("510300,沪深300ETF,etf", "text")[0]

    assert asset["code"] == "510300"
    assert asset["market"] == "CN"


def test_lof_import_keeps_six_digit_fund_code():
    asset = parse_rows("161725,招商中证白酒LOF,lof", "text")[0]

    assert asset["code"] == "161725"
    assert asset["market"] == "CN"


@pytest.mark.parametrize("code", ["000001", "000001.SZ"])
def test_untyped_six_digit_cn_code_requires_asset_type(code):
    with pytest.raises(ValueError, match="asset_type"):
        parse_rows(code, "text")


@pytest.mark.parametrize("code", ["510300", "510300.SH", "161725", "161725.SZ"])
def test_known_cn_fund_namespaces_cannot_be_declared_stock(code):
    with pytest.raises(ValueError, match="fund namespace"):
        parse_rows(f"{code},mislabeled,stock", "text")


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("025209", "fund"),
        ("510300", "etf"),
        ("161725", "lof"),
    ],
)
def test_explicit_fund_types_keep_unsuffixed_six_digit_codes(code, kind):
    asset = parse_rows(f"{code},named,{kind}", "text")[0]

    assert asset["code"] == code
    assert asset["asset_type"] == kind
    assert asset["market"] == "CN"


@pytest.mark.parametrize(
    ("code", "expected_code", "expected_market"),
    [
        ("600519", "600519.SH", "CN"),
        ("000001", "000001.SZ", "CN"),
        ("600519.SH", "600519.SH", "CN"),
        ("00700.HK", "00700.HK", "HK"),
        ("AAPL", "AAPL", "US"),
    ],
)
def test_explicit_stock_paths_remain_valid(code, expected_code, expected_market):
    asset = parse_rows(f"{code},named,stock", "text")[0]

    assert asset["code"] == expected_code
    assert asset["asset_type"] == "stock"
    assert asset["market"] == expected_market
