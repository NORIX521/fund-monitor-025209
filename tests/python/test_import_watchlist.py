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
