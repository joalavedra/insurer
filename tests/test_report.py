import copy
from typing import Any

import pytest

from insurer.cli import main
from insurer.report import render_report
from insurer.simulate import simulate_book, write_results


@pytest.fixture
def results() -> dict[str, Any]:
    return simulate_book(agents=60, months=6, seed=42, years=25)


def test_report_is_self_contained_and_contains_simulation_data(
    results: dict[str, Any],
):
    html = render_report(results)

    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html.lower()
    assert "<link" not in html.lower()
    for claim in results["claims_sample"]:
        assert claim["claim_id"] in html
    for row in results["trial_balance"]:
        assert row["account"] in html
    assert f"{results['book']['combined_ratio'] * 100:.1f}%" in html


def test_report_escapes_claim_reason_html(results: dict[str, Any]):
    modified = copy.deepcopy(results)
    assert modified["claims_sample"]
    modified["claims_sample"][0]["reason"] = "<script>alert(1)</script>"

    html = render_report(modified)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_renders_empty_triangle_note(results: dict[str, Any]):
    modified = results | {
        "triangle": {"origins": [], "note": "Fewer than three origins"}
    }

    assert "Fewer than three origins" in render_report(modified)


def test_report_cli_writes_html_file(tmp_path, results: dict[str, Any]):
    results_path = tmp_path / "results.json"
    output_path = tmp_path / "r.html"
    write_results(results, results_path)

    main(["report", "--results", str(results_path), "--out", str(output_path)])

    assert output_path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_report_cli_uses_default_paths(tmp_path, results: dict[str, Any], monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_results(results, "results.json")

    main(["report"])

    assert (
        (tmp_path / "report.html")
        .read_text(encoding="utf-8")
        .startswith("<!doctype html>")
    )


def test_report_cli_missing_results_uses_argparse_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["report", "--results", str(tmp_path / "missing.json")])

    assert error.value.code == 2
    assert "missing.json" in capsys.readouterr().err
