import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.validate_outputs import generate_fixture_site, main, validate_outputs


REPO = Path(__file__).parents[2]
MIXED_WATCHLIST = REPO / "tests" / "fixtures" / "watchlist-mixed.json"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, value, *, allow_nan=False):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=allow_nan) + "\n",
        encoding="utf-8",
    )


def generated(tmp_path):
    site_root = tmp_path / "site"
    data_root = generate_fixture_site(site_root, MIXED_WATCHLIST)
    return site_root, data_root


def test_mixed_fixture_meets_deep_contract_and_is_a_complete_servable_site(tmp_path):
    site_root, data_root = generated(tmp_path)

    assert validate_outputs(data_root) == []
    assert all(
        (site_root / relative).is_file()
        for relative in (
            "index.html",
            "manifest.webmanifest",
            "robots.txt",
            "sw.js",
            "assets/app.js",
            "assets/core.js",
            "assets/styles.css",
            "assets/icon.svg",
        )
    )
    dashboard = load(data_root / "dashboard.json")
    details = [load(data_root / "assets" / f"{item['id']}.json") for item in dashboard["assets"]]
    assert dashboard["asset_count"] == 4
    assert [item["asset_type"] for item in dashboard["assets"]].count("stock") == 2
    assert {item["state"] for item in dashboard["assets"]} >= {"优先研究", "风险偏高", "暂不纳入"}
    assert any(item["stale"] for item in dashboard["assets"])
    assert any(not item["stale"] for item in dashboard["assets"])
    failed_stock = next(item for item in details if item["asset"]["id"] == "stock-cn-000001-sz")
    assert failed_stock["source_status"]["uzi"]["error"] == "direct_uzi_unavailable"
    fund = next(item for item in details if item["asset"]["asset_type"] == "fund")
    assert fund["market"]["holding_report_date"] == "SYNTHETIC-2000-Q1"
    assert fund["score"]["coverage"]["holding_uzi_pct"] >= 60
    for detail in details:
        assert set(detail["news"]) == {"CN", "INTL"}
        assert all(detail["news"][region] for region in ("CN", "INTL"))
        assert all(
            url.startswith("https://") and ".example/" in url
            for status in detail["source_status"].values()
            for url in status["source_urls"]
        )


def test_fixture_generation_is_byte_deterministic_and_does_not_touch_production_watchlist(tmp_path):
    before = (REPO / "data" / "watchlist.json").read_bytes()
    first = generate_fixture_site(tmp_path / "one", MIXED_WATCHLIST)
    second = generate_fixture_site(tmp_path / "two", MIXED_WATCHLIST)

    first_files = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    assert (REPO / "data" / "watchlist.json").read_bytes() == before


def test_cli_generates_a_repeatable_browser_demo_and_prints_its_path(tmp_path, capsys):
    site_root = tmp_path / "browser-demo"

    assert main(["--generate-demo", str(site_root)]) == 0

    output = capsys.readouterr().out
    assert "OUTPUTS_OK" in output
    assert f"DEMO_SITE={site_root.resolve()}" in output
    assert validate_outputs(site_root / "data") == []


def test_validator_accepts_checked_in_production_seed():
    assert validate_outputs(REPO / "data") == []
    watchlist = load(REPO / "data" / "watchlist.json")
    assert [asset["id"] for asset in watchlist["assets"]] == ["fund-cn-025209"]
    detail = load(REPO / "data" / "assets" / "fund-cn-025209.json")
    assert detail["market"]["holdings"] == []
    assert detail["score"]["coverage"]["holding_uzi_pct"] == 0


def test_validator_reports_parity_duplicate_orphan_and_unsafe_ids(tmp_path):
    _, data_root = generated(tmp_path)
    watchlist = load(data_root / "watchlist.json")
    watchlist["assets"].append(deepcopy(watchlist["assets"][0]))
    watchlist["assets"].append({**deepcopy(watchlist["assets"][0]), "id": "../unsafe"})
    dump(data_root / "watchlist.json", watchlist)
    dashboard = load(data_root / "dashboard.json")
    dashboard["assets"] = dashboard["assets"][1:]
    dashboard["asset_count"] -= 1
    dump(data_root / "dashboard.json", dashboard)
    (data_root / "assets" / "orphan.json").write_text("{}\n", encoding="utf-8")
    (data_root / "assets" / "fund-cn-025209.json").unlink()

    errors = validate_outputs(data_root)

    assert any("duplicate" in error for error in errors)
    assert any("unsafe" in error for error in errors)
    assert any("missing detail" in error for error in errors)
    assert any("orphan detail" in error for error in errors)
    assert any("dashboard parity" in error for error in errors)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda detail: detail["score"].__setitem__("overall", 101), "score.overall"),
        (lambda detail: detail["score"].__setitem__("confidence", -0.1), "score.confidence"),
        (lambda detail: detail["recommendation"].__setitem__("state", "立即买入"), "recommendation.state"),
        (lambda detail: detail["recommendation"].__setitem__("confidence", 1.1), "recommendation.confidence"),
        (lambda detail: detail["news"]["CN"][0].__setitem__("article_url", ""), "news.CN"),
        (lambda detail: detail["news"]["INTL"][0].__setitem__("source_url", "ftp://bad.example/x"), "news.INTL"),
        (lambda detail: detail["news"]["CN"][0].__setitem__("published_at", "not-a-time"), "published_at"),
        (lambda detail: detail["source_status"]["market"].__setitem__("provider", ""), "source_status.market"),
        (lambda detail: detail["source_status"]["market"].__setitem__("attempted_at", "not-a-time"), "attempted_at"),
    ],
)
def test_validator_rejects_deep_invalid_fields(tmp_path, mutate, expected):
    _, data_root = generated(tmp_path)
    path = data_root / "assets" / "stock-cn-600519-sh.json"
    detail = load(path)
    mutate(detail)
    dump(path, detail)

    assert any(expected in error for error in validate_outputs(data_root))


def test_validator_requires_holding_report_date_and_direct_uzi_failure_provenance(tmp_path):
    _, data_root = generated(tmp_path)
    fund_path = data_root / "assets" / "fund-cn-025209.json"
    fund = load(fund_path)
    fund["market"].pop("holding_report_date")
    dump(fund_path, fund)
    stock_path = data_root / "assets" / "stock-cn-000001-sz.json"
    stock = load(stock_path)
    stock["source_status"]["uzi"]["error"] = ""
    stock["source_status"]["uzi"]["stale"] = False
    dump(stock_path, stock)

    errors = validate_outputs(data_root)

    assert any("holding_report_date" in error for error in errors)
    assert any("direct UZI failure" in error for error in errors)


def test_validator_rejects_non_finite_json_and_heavy_dashboard(tmp_path):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "stock-cn-600519-sh.json"
    detail = load(detail_path)
    detail["score"]["overall"] = float("nan")
    dump(detail_path, detail, allow_nan=True)
    dashboard_path = data_root / "dashboard.json"
    dashboard = load(dashboard_path)
    dashboard["assets"][0]["embedded_detail"] = "x" * 140_000
    dump(dashboard_path, dashboard)

    errors = validate_outputs(data_root)

    assert any("finite JSON" in error for error in errors)
    assert any("lightweight" in error for error in errors)


def test_validator_rejects_exponent_overflow_anywhere_in_detail(tmp_path):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "stock-cn-600519-sh.json"
    text = detail_path.read_text(encoding="utf-8")
    text = text.replace(
        '"fixture_notice": "SYNTHETIC STALE FIXTURE: not live market data"',
        '"fixture_notice": "SYNTHETIC STALE FIXTURE: not live market data",\n'
        '    "deep_overflow": 1e999',
        1,
    )
    detail_path.write_text(text, encoding="utf-8")

    assert any("finite JSON" in error for error in validate_outputs(data_root))


def test_validator_rejects_dashboard_detail_summary_mismatch(tmp_path):
    _, data_root = generated(tmp_path)
    dashboard_path = data_root / "dashboard.json"
    dashboard = load(dashboard_path)
    dashboard["assets"][0]["confidence"] = 0.1234
    dump(dashboard_path, dashboard)

    assert any("summary parity" in error for error in validate_outputs(data_root))
