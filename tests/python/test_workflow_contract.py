import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "update-and-deploy.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")
UZI_SHA = "fce996c33e70eddce8e375f53cd252b549eb3d7c"


def _position(text: str) -> int:
    position = WORKFLOW.find(text)
    assert position >= 0, f"missing workflow contract: {text}"
    return position


def test_workflow_has_all_required_triggers_and_authorization_gates():
    assert re.search(r"(?m)^\s{2}issues:\s*$", WORKFLOW)
    assert re.search(r"(?m)^\s{4}types:\s*\[opened\]\s*$", WORKFLOW)
    assert re.search(r"(?m)^\s{2}workflow_dispatch:\s*$", WORKFLOW)
    assert re.search(r"(?m)^\s{8}type:\s*choice\s*$", WORKFLOW)
    assert re.search(r"(?m)^\s{8}options:\s*$[\s\S]*?^\s{10}- lite\s*$[\s\S]*?^\s{10}- medium\s*$", WORKFLOW)
    assert 'cron: "30 13 * * 1-5"' in WORKFLOW
    assert 'cron: "15 2 * * 6"' in WORKFLOW
    assert re.search(r"(?m)^\s{2}push:\s*$[\s\S]*?^\s{4}branches:\s*\[main\]\s*$", WORKFLOW)
    assert "startsWith(github.event.issue.title, '[watchlist-import]')" in WORKFLOW
    for association in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert association in WORKFLOW
    assert "unauthorized-import-feedback:" in WORKFLOW
    assert "permissions:\n      issues: write" in WORKFLOW
    assert "github.rest.issues.createComment" in WORKFLOW
    assert "refresh-and-stage:" in WORKFLOW


def test_workflow_uses_secure_issue_file_import_and_closes_only_after_success():
    assert "context.payload.issue.body" in WORKFLOW
    assert "RUNNER_TEMP" in WORKFLOW
    assert "writeFileSync" in WORKFLOW
    assert "--issue-body-file" in WORKFLOW
    assert "--summary-file" in WORKFLOW
    assert "WATCHLIST_IMPORT_V1" not in WORKFLOW
    assert "${{ github.event.issue.body }}" not in WORKFLOW
    assert "github.rest.issues.update" in WORKFLOW
    assert "state: 'closed'" in WORKFLOW
    assert "github.run_id" in WORKFLOW


def test_workflow_pins_uzi_bounds_depth_and_restores_only_its_cache():
    assert UZI_SHA in WORKFLOW
    assert "https://github.com/wbh604/UZI-Skill.git" in WORKFLOW
    assert "requirements-actions.txt" in WORKFLOW
    assert re.search(r"git\s+[^\n]*checkout --detach", WORKFLOW)
    timeout = re.search(r"timeout-minutes:\s*(\d+)", WORKFLOW)
    assert timeout and int(timeout.group(1)) <= 330
    assert "actions/cache@v4" in WORKFLOW
    cache_block = re.search(r"(?ms)^\s{6}- name: Restore UZI cache\s*$.*?(?=^\s{6}- name:|^\s{2}\w)", WORKFLOW)
    assert cache_block
    assert "UZI-Skill/skills/deep-analysis/scripts/.cache" in cache_block.group(0)
    assert ".venv" not in cache_block.group(0)
    assert "~/.cache" not in cache_block.group(0)
    assert "python scripts/run_uzi_guarded.py data/watchlist.json" in WORKFLOW
    assert "--depth \"$DEPTH\"" in WORKFLOW
    assert "--result-file" in WORKFLOW
    assert "--uzi-result" in WORKFLOW
    depth_input = re.search(r"(?ms)^\s{6}depth:\s*$.*?(?=^\s{2}schedule:)", WORKFLOW)
    assert depth_input and "deep" not in depth_input.group(0)


def test_workflow_tests_before_generated_only_commit_and_deploys_same_run():
    for action in (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/github-script@v7",
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v4",
        "actions/deploy-pages@v4",
    ):
        assert action in WORKFLOW
    tests = _position("python -m pytest tests/python -q")
    node_tests = _position("npm test")
    commit = _position("git commit")
    assert tests < commit and node_tests < commit
    assert "git add data/watchlist.json data/dashboard.json data/assets data/uzi data/new_alerts.json" in WORKFLOW
    assert "git add ." not in WORKFLOW
    assert "git add -A" not in WORKFLOW
    assert "_site" in WORKFLOW
    assert "cp -R . _site" not in WORKFLOW
    for public_path in ("index.html", "manifest.webmanifest", "sw.js", "assets", "data"):
        assert public_path in WORKFLOW
    for excluded in (".git", ".github", ".uzi", "tests", "scripts", ".venv"):
        assert excluded in WORKFLOW
    upload = _position("actions/upload-pages-artifact@v4")
    deploy = _position("actions/deploy-pages@v4")
    assert upload < deploy
    assert re.search(r"(?m)^\s{4}needs:\s*refresh-and-stage\s*$", WORKFLOW)


def test_actions_dependency_lock_uses_exact_direct_versions_and_main_checkout():
    lock = (ROOT / "requirements-actions.txt").read_text(encoding="utf-8")
    dependencies = [
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert dependencies
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=<>~!]+", line) for line in dependencies)
    for package in ("requests", "beautifulsoup4", "pytest", "akshare", "pandas", "ddgs", "playwright", "rich"):
        assert any(line.lower().startswith(f"{package}==") for line in dependencies)
    checkout = re.search(r"(?ms)- name: Checkout\s+uses: actions/checkout@v4\s+with:\s+ref: main", WORKFLOW)
    assert checkout


def test_import_cli_atomically_merges_and_writes_machine_summary(tmp_path):
    watchlist = tmp_path / "watchlist.json"
    body = tmp_path / "issue-body.txt"
    summary = tmp_path / "summary.json"
    watchlist.write_text(
        json.dumps(
            {
                "version": 1,
                "assets": [
                    {
                        "id": "fund-cn-025209",
                        "code": "025209",
                        "name": "old",
                        "asset_type": "fund",
                        "market": "CN",
                        "sector": "",
                        "note": "",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    body.write_text(
        '<!-- WATCHLIST_IMPORT_V1 {"assets": ['
        '{"code":"025209","name":"new","asset_type":"fund"},'
        '{"code":"600519.SH","name":"stock","asset_type":"stock"}'
        "]} -->",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/import_watchlist.py",
            "--issue-body-file",
            str(body),
            "--watchlist",
            str(watchlist),
            "--summary-file",
            str(summary),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    merged = json.loads(watchlist.read_text(encoding="utf-8"))
    machine = json.loads(summary.read_text(encoding="utf-8"))
    assert [asset["id"] for asset in merged["assets"]] == [
        "fund-cn-025209",
        "stock-cn-600519-sh",
    ]
    assert machine == {
        "status": "success",
        "imported_count": 2,
        "added_count": 1,
        "updated_count": 1,
        "total_count": 2,
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_monitor_cli_and_uzi_cli_expose_typed_workflow_arguments():
    commands = {
        "scripts/import_watchlist.py": ("--issue-body-file", "--summary-file"),
        "scripts/update_monitor.py": ("--watchlist", "--data-dir", "--uzi-cache", "--uzi-result"),
        "scripts/run_uzi_guarded.py": ("watchlist_json", "--depth", "--uzi-root", "--result-file"),
    }
    for script, expected in commands.items():
        result = subprocess.run(
            [sys.executable, script, "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        for argument in expected:
            assert argument in result.stdout


def test_monitor_help_does_not_require_network_provider_dependencies():
    result = subprocess.run(
        [sys.executable, "-S", "scripts/update_monitor.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--uzi-cache" in result.stdout
