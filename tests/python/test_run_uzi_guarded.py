import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_uzi_guarded


def _make_uzi_root(tmp_path):
    root = tmp_path / "UZI-Skill"
    (root / "skills" / "deep-analysis" / "scripts").mkdir(parents=True)
    return root


def _write_valid_cache(root, ticker, overall=72):
    target = root / "skills" / "deep-analysis" / "scripts" / ".cache" / ticker
    target.mkdir(parents=True, exist_ok=True)
    (target / "synthesis.json").write_text(
        json.dumps({"overall_score": overall}), encoding="utf-8"
    )
    (target / "panel.json").write_text(
        json.dumps({"panel_consensus": overall}), encoding="utf-8"
    )
    return target


def _write_watchlist(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {"code": "600519.SH", "name": "贵州茅台", "asset_type": "stock", "enabled": True},
                    {"code": "AAPL", "name": "Apple", "asset_type": "stock", "enabled": False},
                    {"code": "025209", "name": "公募基金", "asset_type": "fund", "enabled": True},
                    {"code": "510300", "name": "指数ETF", "asset_type": "etf", "enabled": True},
                    {"code": "161725", "name": "白酒LOF", "asset_type": "lof", "enabled": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_watchlist_runner_builds_temporary_stock_only_portfolio(tmp_path, monkeypatch):
    captured = {}

    def fake_runner(uzi_root, portfolio_csv, depth):
        assert Path(portfolio_csv).is_file()
        captured["path"] = Path(portfolio_csv)
        with Path(portfolio_csv).open(encoding="utf-8-sig", newline="") as handle:
            captured["rows"] = list(csv.DictReader(handle))
        _write_valid_cache(Path(uzi_root), "600519.SH")
        return {"status": "completed"}

    monkeypatch.setattr(run_uzi_guarded, "_run_uzi_portfolio", fake_runner, raising=False)

    result = run_uzi_guarded.run_watchlist(
        _make_uzi_root(tmp_path),
        _write_watchlist(tmp_path),
        "lite",
        details_dir=tmp_path / "details",
    )

    assert result["status"] == "completed"
    assert result["tickers"]["600519.SH"]["status"] == "refreshed_this_run"
    assert captured["rows"] == [
        {"ticker": "600519.SH", "weight": "1.0", "note": "贵州茅台"}
    ]
    assert not captured["path"].exists()


def test_fund_only_watchlist_without_disclosed_holdings_skips_private_uzi_runner(tmp_path, monkeypatch):
    watchlist = tmp_path / "funds.json"
    watchlist.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {"code": "025209", "asset_type": "fund", "enabled": True},
                    {"code": "510300", "asset_type": "etf", "enabled": True},
                    {"code": "161725", "asset_type": "lof", "enabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        run_uzi_guarded,
        "_run_uzi_portfolio",
        lambda *args: pytest.fail("fund entity reached UZI"),
    )

    result = run_uzi_guarded.run_watchlist(
        _make_uzi_root(tmp_path), watchlist, "lite", details_dir=tmp_path / "details"
    )
    assert result["status"] == "skipped_no_targets"


@pytest.mark.parametrize("code", ["025209", "025209.SZ", "510300.SH", "161725.SZ"])
def test_code_outside_cn_equity_namespaces_never_calls_private_uzi_runner(
    tmp_path, monkeypatch, code
):
    watchlist = tmp_path / "mislabeled.json"
    watchlist.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {"code": code, "asset_type": "stock", "enabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        run_uzi_guarded,
        "_run_uzi_portfolio",
        lambda *args: pytest.fail(f"{code} reached UZI"),
    )

    with pytest.raises(ValueError, match="CN equity"):
        run_uzi_guarded.run_watchlist(_make_uzi_root(tmp_path), watchlist, "lite")


def test_private_runner_configures_utf8_and_guard_before_other_uzi_imports(tmp_path, monkeypatch):
    events = []

    def fake_configure_utf8():
        events.append("utf8")
        run_uzi_guarded.os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    profile = SimpleNamespace(
        get_profile=lambda depth: events.append(("profile", depth)) or {"depth": depth},
        apply_profile_to_env=lambda value: events.append(("apply", value)),
    )
    runner = SimpleNamespace(
        run_portfolio=lambda path, **kwargs: events.append(("run", Path(path), kwargs))
        or {"status": "completed"}
    )
    modules = {
        "lib.net_timeout_guard": SimpleNamespace(),
        "lib.analysis_profile": profile,
        "lib.portfolio_runner": runner,
    }

    monkeypatch.setattr(
        run_uzi_guarded,
        "_configure_utf8",
        fake_configure_utf8,
    )
    monkeypatch.setattr(
        run_uzi_guarded.importlib,
        "import_module",
        lambda name: events.append(("import", name)) or modules[name],
    )
    for name in ("PYTHONIOENCODING", "UZI_CLI_ONLY", "UZI_NO_AUTO_OPEN", "UZI_HTTP_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    portfolio = tmp_path / "stocks.csv"
    portfolio.write_text("ticker,weight,note\n600519.SH,1.0,test\n", encoding="utf-8")

    result = run_uzi_guarded._run_uzi_portfolio(_make_uzi_root(tmp_path), portfolio, "lite")

    assert result == {"status": "completed"}
    assert events[:4] == [
        "utf8",
        ("import", "lib.net_timeout_guard"),
        ("import", "lib.analysis_profile"),
        ("import", "lib.portfolio_runner"),
    ]
    assert run_uzi_guarded.os.environ["PYTHONIOENCODING"] == "utf-8"
    assert run_uzi_guarded.os.environ["UZI_CLI_ONLY"] == "1"
    assert run_uzi_guarded.os.environ["UZI_NO_AUTO_OPEN"] == "1"
    assert run_uzi_guarded.os.environ["UZI_HTTP_TIMEOUT"] == "15"
    assert events[-1][2] == {"depth": "lite", "auto_open": False}


def test_utf8_configuration_reconfigures_both_streams(monkeypatch):
    calls = []

    class Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        run_uzi_guarded,
        "sys",
        SimpleNamespace(stdout=Stream(), stderr=Stream()),
    )
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    run_uzi_guarded._configure_utf8()

    assert calls == [
        {"encoding": "utf-8", "errors": "backslashreplace"},
        {"encoding": "utf-8", "errors": "backslashreplace"},
    ]
    assert run_uzi_guarded.os.environ["PYTHONIOENCODING"] == "utf-8"


def test_private_runner_rejects_non_object_uzi_result(tmp_path, monkeypatch):
    modules = {
        "lib.net_timeout_guard": SimpleNamespace(),
        "lib.analysis_profile": SimpleNamespace(
            get_profile=lambda depth: {}, apply_profile_to_env=lambda value: None
        ),
        "lib.portfolio_runner": SimpleNamespace(run_portfolio=lambda *args, **kwargs: []),
    }
    monkeypatch.setattr(
        run_uzi_guarded.importlib, "import_module", lambda name: modules[name]
    )
    portfolio = tmp_path / "stocks.csv"
    portfolio.write_text("ticker,weight,note\n600519.SH,1.0,test\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-object"):
        run_uzi_guarded._run_uzi_portfolio(_make_uzi_root(tmp_path), portfolio, "lite")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "completed"}, 0),
        ({"status": "insufficient_data"}, 1),
        ({}, 1),
    ],
)
def test_cli_exit_status_requires_completed(monkeypatch, result, expected):
    monkeypatch.setattr(
        run_uzi_guarded, "run_watchlist", lambda *args, **kwargs: result, raising=False
    )

    assert run_uzi_guarded.main(["watchlist.json"]) == expected


def test_cli_rejects_raw_csv_before_uzi_invocation(tmp_path, monkeypatch):
    csv_path = tmp_path / "portfolio.csv"
    csv_path.write_text("ticker,weight,note\n025209,1.0,fund\n", encoding="utf-8")
    monkeypatch.setattr(
        run_uzi_guarded,
        "_run_uzi_portfolio",
        lambda *args: pytest.fail("raw CSV reached UZI"),
        raising=False,
    )

    assert run_uzi_guarded.main([str(csv_path), "--uzi-root", str(_make_uzi_root(tmp_path))]) == 2


def test_cli_writes_current_run_result_for_downstream_failure_provenance(tmp_path, monkeypatch):
    result_file = tmp_path / "uzi-result.json"
    expected = {
        "status": "completed",
        "loaded": 1,
        "failed": [{"ticker": "000001.SZ", "weight": 0.5}],
    }
    monkeypatch.setattr(run_uzi_guarded, "run_watchlist", lambda *args, **kwargs: expected)

    exit_code = run_uzi_guarded.main(
        ["watchlist.json", "--result-file", str(result_file)]
    )

    assert exit_code == 0
    assert json.loads(result_file.read_text(encoding="utf-8")) == expected
    assert not list(tmp_path.glob("*.tmp"))


def test_pipeline_exception_with_old_cache_is_restored_fallback_not_fresh(tmp_path, monkeypatch):
    root = _make_uzi_root(tmp_path)
    old_cache = _write_valid_cache(root, "600519.SH", overall=61)
    old_synthesis = (old_cache / "synthesis.json").read_bytes()

    def failing_runner(uzi_root, portfolio_csv, depth):
        assert not old_cache.exists(), "restored target cache reached upstream resume path"
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(run_uzi_guarded, "_run_uzi_portfolio", failing_runner)

    manifest = run_uzi_guarded.run_watchlist(
        root,
        _write_watchlist(tmp_path),
        "lite",
        details_dir=tmp_path / "details",
        run_id="run-123",
    )

    entry = manifest["tickers"]["600519.SH"]
    assert entry["status"] == "restored_fallback"
    assert entry["stale"] is True
    assert entry["error"] == "current_run_output_missing_or_invalid"
    assert entry["run_id"] == "run-123"
    assert (old_cache / "synthesis.json").read_bytes() == old_synthesis


def test_fund_disclosed_holdings_form_uzi_universe_and_empty_union_skips_runner(tmp_path, monkeypatch):
    root = _make_uzi_root(tmp_path)
    watchlist = tmp_path / "fund-watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {
                        "id": "fund-cn-025209",
                        "code": "025209",
                        "asset_type": "fund",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    details = tmp_path / "details"
    details.mkdir()
    (details / "fund-cn-025209.json").write_text(
        json.dumps({"market": {"holdings": [{"code": "600519.SH", "name": "贵州茅台"}]}}),
        encoding="utf-8",
    )
    calls = []

    def refreshing_runner(uzi_root, portfolio_csv, depth):
        with Path(portfolio_csv).open(encoding="utf-8-sig", newline="") as handle:
            calls.extend(row["ticker"] for row in csv.DictReader(handle))
        _write_valid_cache(Path(uzi_root), "600519.SH", overall=76)
        return {"status": "completed", "loaded": 1, "failed": []}

    monkeypatch.setattr(run_uzi_guarded, "_run_uzi_portfolio", refreshing_runner)
    manifest = run_uzi_guarded.run_watchlist(
        root, watchlist, "lite", details_dir=details, run_id="fund-run"
    )

    assert calls == ["600519.SH"]
    assert manifest["tickers"]["600519.SH"]["status"] == "refreshed_this_run"
    from scripts.providers.eastmoney import ProviderResult
    from scripts.update_monitor import _select_current_uzi, run_pipeline
    from scripts.uzi_adapter import publish_uzi_manifest

    public = publish_uzi_manifest(
        root / "skills" / "deep-analysis" / "scripts" / ".cache",
        manifest,
        tmp_path / "public-uzi",
    )
    _, holding_uzi = _select_current_uzi(
        json.loads(watchlist.read_text(encoding="utf-8")), public, manifest
    )

    class FundProvider:
        def fetch_fund(self, asset):
            return ProviderResult(
                data={
                    "asset": asset,
                    "history": [{"date": "2026-07-20", "nav": 1.0}, {"date": "2026-07-21", "nav": 1.1}],
                    "holdings": [{"code": "600519.SH", "name": "贵州茅台", "weight_pct": 25, "latest_price": 1500, "change_pct": 1}],
                },
                source_urls=["https://provider.test/fund"],
                retrieved_at="2026-07-22T08:00:00+00:00",
                errors={},
            )

    scored = run_pipeline(
        json.loads(watchlist.read_text(encoding="utf-8")),
        {},
        {"now": "2026-07-22T08:00:00+00:00", "fund_provider": FundProvider(), "holding_uzi": holding_uzi, "news_provider": lambda *_: []},
    )
    assert scored["assets"]["fund-cn-025209"]["score"]["coverage"]["holding_uzi_pct"] == 25.0

    (details / "fund-cn-025209.json").write_text(
        json.dumps({"market": {"holdings": []}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        run_uzi_guarded,
        "_run_uzi_portfolio",
        lambda *args: pytest.fail("empty universe reached UZI"),
    )
    skipped = run_uzi_guarded.run_watchlist(
        root, watchlist, "lite", details_dir=details, run_id="empty-run"
    )
    assert skipped["status"] == "skipped_no_targets"
    assert skipped["tickers"] == {}


def test_medium_depth_chunks_ten_and_preserves_other_batches_when_one_fails(
    tmp_path, monkeypatch
):
    root = _make_uzi_root(tmp_path)
    tickers = [f"{600000 + index:06d}.SH" for index in range(21)]
    watchlist = tmp_path / "many-stocks.json"
    watchlist.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {
                        "code": ticker,
                        "name": ticker,
                        "asset_type": "stock",
                        "enabled": True,
                    }
                    for ticker in tickers
                ],
            }
        ),
        encoding="utf-8",
    )
    for ticker in tickers[10:20]:
        _write_valid_cache(root, ticker, overall=61)

    calls = []

    def runner(uzi_root, portfolio_csv, depth):
        with Path(portfolio_csv).open(encoding="utf-8-sig", newline="") as handle:
            batch = [row["ticker"] for row in csv.DictReader(handle)]
        calls.append(batch)
        if len(calls) == 2:
            raise RuntimeError("isolated second batch failure")
        for ticker in batch:
            _write_valid_cache(Path(uzi_root), ticker, overall=80)
        return {"status": "completed"}

    monkeypatch.setattr(run_uzi_guarded, "_run_uzi_portfolio", runner)

    manifest = run_uzi_guarded.run_watchlist(
        root,
        watchlist,
        "medium",
        details_dir=tmp_path / "details",
        run_id="chunked-run",
    )

    assert [len(batch) for batch in calls] == [10, 10, 1]
    assert [batch[0] for batch in calls] == [tickers[0], tickers[10], tickers[20]]
    assert manifest["status"] == "partial"
    assert [batch["status"] for batch in manifest["batches"]] == [
        "completed",
        "failed",
        "completed",
    ]
    assert all(
        manifest["tickers"][ticker]["status"] == "refreshed_this_run"
        for ticker in [*tickers[:10], tickers[20]]
    )
    assert all(
        manifest["tickers"][ticker]["status"] == "restored_fallback"
        for ticker in tickers[10:20]
    )
    cache_root = root / "skills" / "deep-analysis" / "scripts" / ".cache"
    for ticker in tickers:
        assert run_uzi_guarded._valid_cache(cache_root / ticker, ticker)
