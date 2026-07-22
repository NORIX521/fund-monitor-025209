import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.validate_outputs import generate_fixture_site, main, validate_outputs


REPO = Path(__file__).parents[2]
MIXED_WATCHLIST = REPO / "tests" / "fixtures" / "watchlist-mixed.json"
FIXTURE_NOW = "2026-07-22T00:00:00+00:00"


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


def assert_cli_rejects(data_root, capsys):
    assert main([str(data_root)]) == 1
    output = capsys.readouterr().out
    assert "OUTPUT_ERROR:" in output
    assert "OUTPUTS_OK" not in output


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


@pytest.mark.parametrize(
    "unsafe_id",
    ["../escaped", "../../data/watchlist", "C:/absolute-target"],
)
def test_fixture_generation_rejects_unsafe_ids_before_any_write(tmp_path, unsafe_id):
    fixture = load(MIXED_WATCHLIST)
    fixture["assets"][0]["id"] = unsafe_id
    fixture_path = tmp_path / "unsafe-fixture.json"
    dump(fixture_path, fixture)
    destination = tmp_path / "site"
    production_before = (REPO / "data" / "watchlist.json").read_bytes()

    with pytest.raises(ValueError, match="asset id"):
        generate_fixture_site(destination, fixture_path)

    assert not destination.exists()
    assert not (tmp_path / "escaped.json").exists()
    assert (REPO / "data" / "watchlist.json").read_bytes() == production_before


def test_fixture_generation_rejects_duplicate_ids_before_any_write(tmp_path):
    fixture = load(MIXED_WATCHLIST)
    fixture["assets"].append(deepcopy(fixture["assets"][0]))
    fixture_path = tmp_path / "duplicate-fixture.json"
    dump(fixture_path, fixture)
    destination = tmp_path / "site"

    with pytest.raises(ValueError, match="duplicate asset id"):
        generate_fixture_site(destination, fixture_path)

    assert not destination.exists()


@pytest.mark.parametrize("destination", [REPO, REPO / "data", REPO / "data" / "demo"])
def test_fixture_generation_rejects_repository_root_and_production_data_destinations(destination):
    production_before = (REPO / "data" / "watchlist.json").read_bytes()

    with pytest.raises(ValueError, match="destination"):
        generate_fixture_site(destination, MIXED_WATCHLIST)

    assert (REPO / "data" / "watchlist.json").read_bytes() == production_before


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
    dashboard = load(REPO / "data" / "dashboard.json")
    assert dashboard["pipeline_version"] == "4.1"
    detail = load(REPO / "data" / "assets" / "fund-cn-025209.json")
    assert detail["market"] == {"history": [], "holdings": []}
    assert detail["market"]["holdings"] == []
    assert detail["score"]["overall"] is None
    assert detail["score"]["components"] == {}
    assert detail["score"]["coverage"]["holding_uzi_pct"] == 0
    serialized = json.dumps({"dashboard": dashboard, "detail": detail}, ensure_ascii=False).lower()
    assert "synthetic" not in serialized
    assert "fixture" not in serialized
    assert ".example" not in serialized


def test_validator_accepts_fresh_stock_and_fund_outputs_from_real_pipeline(tmp_path):
    from scripts.providers.eastmoney import ProviderResult
    from scripts.providers.news import NewsItem
    from scripts.update_monitor import run_pipeline

    stock = {
        "id": "stock-cn-600519-sh",
        "code": "600519.SH",
        "name": "新鲜股票夹具",
        "asset_type": "stock",
        "market": "CN",
        "sector": "消费",
        "note": "producer-validator integration fixture",
        "enabled": True,
    }
    fund = {
        "id": "fund-cn-025209",
        "code": "025209",
        "name": "新鲜基金夹具",
        "asset_type": "fund",
        "market": "CN",
        "sector": "半导体",
        "note": "producer-validator integration fixture",
        "enabled": True,
    }
    watchlist = {"version": 1, "updated_at": FIXTURE_NOW, "assets": [stock, fund]}

    class FreshFundProvider:
        def fetch_fund(self, asset):
            return ProviderResult(
                data={
                    "asset": asset,
                    "history": [
                        {"date": "2026-07-20", "nav": 1.0},
                        {"date": "2026-07-21", "nav": 1.05},
                        {"date": "2026-07-22", "nav": 1.1},
                    ],
                    "holdings": [
                        {
                            "code": "600519.SH",
                            "name": "贵州茅台",
                            "weight_pct": 20,
                            "latest_price": 1500,
                            "change_pct": 1,
                        }
                    ],
                    "holding_report_date": "2026 Q2",
                },
                source_urls=["https://fund-provider.example/025209"],
                retrieved_at=FIXTURE_NOW,
                errors={},
            )

    def fresh_news(asset, region):
        return [
            NewsItem(
                title=f"{asset['code']} {region} sourced update",
                article_url=f"https://article.example/{asset['id']}/{region.lower()}",
                source=f"{region} publisher",
                source_url=f"https://publisher.example/{region.lower()}",
                published_at=FIXTURE_NOW,
                retrieved_at=FIXTURE_NOW,
                region=region,
            )
        ]

    data_root = tmp_path / "data"
    data_root.mkdir()
    dump(data_root / "watchlist.json", watchlist)
    run_pipeline(
        watchlist,
        {},
        {
            "now": FIXTURE_NOW,
            "fund_provider": FreshFundProvider(),
            "market_data": {
                stock["id"]: {
                    "quality_valuation": 75,
                    "trend_momentum": 70,
                    "risk_signals": 80,
                    "news_events": 65,
                }
            },
            "market_source_urls": {
                stock["id"]: ["https://market-provider.example/600519"]
            },
            "uzi": {stock["id"]: {"overall": 82}},
            "holding_uzi": {"600519.SH": {"overall": 82}},
            "news_provider": fresh_news,
            "output_dir": data_root,
            "write": True,
        },
    )

    assert validate_outputs(data_root) == []
    dashboard = load(data_root / "dashboard.json")
    stock_detail = load(data_root / "assets" / f"{stock['id']}.json")
    fund_detail = load(data_root / "assets" / f"{fund['id']}.json")
    assert all(not status["stale"] for status in stock_detail["source_status"].values())
    assert all(not status["stale"] for status in fund_detail["source_status"].values())
    assert dashboard["source_status"]["pipeline"]["source_urls"]
    assert stock_detail["source_status"]["market"]["source_urls"] == [
        "https://market-provider.example/600519"
    ]
    assert stock_detail["source_status"]["uzi"]["source_urls"]
    assert stock_detail["source_status"]["news_CN"]["source_urls"] == [
        "https://publisher.example/cn"
    ]
    assert fund_detail["source_status"]["market"]["source_urls"] == [
        "https://fund-provider.example/025209"
    ]


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


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda status: status.update(
                {"stale": False, "error": "provider_failed"}
            ),
            "fresh status cannot contain an error",
        ),
        (
            lambda status: status.update(
                {"stale": False, "retrieved_at": "", "last_success_at": ""}
            ),
            "fresh status requires retrieved_at and last_success_at",
        ),
        (
            lambda status: status.update({"stale": False, "source_urls": []}),
            "fresh status requires source URLs",
        ),
    ],
)
def test_validator_rejects_contradictory_fresh_source_status(
    tmp_path, capsys, mutate, expected
):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "stock-cn-600519-sh.json"
    detail = load(detail_path)
    mutate(detail["source_status"]["market"])
    dump(detail_path, detail)

    assert any(expected in error for error in validate_outputs(data_root))
    assert_cli_rejects(data_root, capsys)


def test_validator_rejects_incorrect_coverage_arithmetic(tmp_path, capsys):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "fund-cn-025209.json"
    detail = load(detail_path)
    detail["source_status"]["quotes"]["coverage"] = {
        "covered": 2,
        "total": 2,
        "pct": 1.0,
    }
    dump(detail_path, detail)

    assert any("coverage pct" in error for error in validate_outputs(data_root))
    assert_cli_rejects(data_root, capsys)


@pytest.mark.parametrize("field", ["reasons", "invalidation_rules"])
def test_validator_requires_nonempty_recommendation_evidence_lists(
    tmp_path, capsys, field
):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "fund-cn-025209.json"
    detail = load(detail_path)
    detail["recommendation"][field] = []
    dump(detail_path, detail)

    assert any(f"recommendation.{field}" in error for error in validate_outputs(data_root))
    assert_cli_rejects(data_root, capsys)


@pytest.mark.parametrize("field", ["hard_flags", "warnings", "hard_failures"])
def test_validator_rejects_non_text_recommendation_risk_lists(tmp_path, capsys, field):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "fund-cn-025209.json"
    detail = load(detail_path)
    detail["recommendation"]["risk"][field] = [123]
    dump(detail_path, detail)

    assert any(f"recommendation.risk.{field}" in error for error in validate_outputs(data_root))
    assert_cli_rejects(data_root, capsys)


@pytest.mark.parametrize("asset_id", ["fund-cn-025209", "etf-cn-510300"])
def test_validator_rejects_direct_uzi_payload_for_fund_products(
    tmp_path, capsys, asset_id
):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / f"{asset_id}.json"
    detail = load(detail_path)
    detail["uzi"] = {"overall": 99}
    dump(detail_path, detail)

    assert any("fund products must not contain direct UZI" in error for error in validate_outputs(data_root))
    assert_cli_rejects(data_root, capsys)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda detail: detail["source_status"]["uzi"].update(
                {"stale": True, "error": "uzi_failed", "retrieved_at": ""}
            ),
            "direct UZI score requires successful UZI status",
        ),
        (
            lambda detail: detail["uzi"].clear(),
            "explicit direct UZI failure is required",
        ),
    ],
)
def test_validator_requires_stock_uzi_payload_status_agreement(
    tmp_path, capsys, mutate, expected
):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "stock-cn-600519-sh.json"
    detail = load(detail_path)
    mutate(detail)
    dump(detail_path, detail)

    assert any(expected in error for error in validate_outputs(data_root))
    assert_cli_rejects(data_root, capsys)


def test_malformed_source_status_returns_errors_and_cli_exit_one(tmp_path, capsys):
    _, data_root = generated(tmp_path)
    detail_path = data_root / "assets" / "stock-cn-000001-sz.json"
    detail = load(detail_path)
    detail["source_status"] = []
    dump(detail_path, detail)

    errors = validate_outputs(data_root)

    assert isinstance(errors, list)
    assert errors
    assert any("source_status provenance" in error for error in errors)
    assert main([str(data_root)]) == 1
    output = capsys.readouterr().out
    assert "OUTPUT_ERROR:" in output
    assert "Traceback" not in output


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
