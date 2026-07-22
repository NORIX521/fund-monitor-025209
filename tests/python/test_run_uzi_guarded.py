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
        return {"status": "completed"}

    monkeypatch.setattr(run_uzi_guarded, "_run_uzi_portfolio", fake_runner, raising=False)

    result = run_uzi_guarded.run_watchlist(
        _make_uzi_root(tmp_path), _write_watchlist(tmp_path), "lite"
    )

    assert result == {"status": "completed"}
    assert captured["rows"] == [
        {"ticker": "600519.SH", "weight": "1.0", "note": "贵州茅台"}
    ]
    assert not captured["path"].exists()


def test_fund_only_watchlist_never_calls_private_uzi_runner(tmp_path, monkeypatch):
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

    with pytest.raises(ValueError, match="enabled stock"):
        run_uzi_guarded.run_watchlist(_make_uzi_root(tmp_path), watchlist, "lite")


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
    monkeypatch.setattr(run_uzi_guarded, "run_watchlist", lambda *args: result, raising=False)

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
