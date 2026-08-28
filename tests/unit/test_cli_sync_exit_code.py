"""Unit tests for the `powerschool sync` failure contract.

The cron watchdog reads "exit 0 with empty stdout" as success, so any sync
failure MUST exit non-zero and write the error to stderr. A silent failure once
hid an 11-day outage.
"""

import sys
import types

import pytest
from click.testing import CliRunner

from src.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    return CliRunner()


def _install_fake_scraper(monkeypatch, run_full_scrape):
    scrape_full = types.ModuleType("scripts.scrape_full")
    scrape_full.run_full_scrape = run_full_scrape
    load_data = types.ModuleType("scripts.load_data")
    load_data.load_scraped_data = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "scripts.scrape_full", scrape_full)
    monkeypatch.setitem(sys.modules, "scripts.load_data", load_data)


def test_sync_exits_nonzero_when_scrape_raises(runner, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("BrowserType.launch: Executable doesn't exist")

    _install_fake_scraper(monkeypatch, boom)

    result = runner.invoke(cli, ["sync", "--headless"])

    assert result.exit_code == 1
    assert "BrowserType.launch" in result.output


def test_sync_exits_nonzero_on_import_error(runner, monkeypatch):
    # A None entry in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "scripts.scrape_full", None)
    monkeypatch.setitem(sys.modules, "scripts.load_data", None)

    result = runner.invoke(cli, ["sync"])

    assert result.exit_code == 1


def test_sync_exits_zero_on_success(runner, monkeypatch):
    _install_fake_scraper(monkeypatch, lambda **kwargs: None)

    result = runner.invoke(cli, ["sync", "--headless"])

    assert result.exit_code == 0
