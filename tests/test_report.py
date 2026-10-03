import copy
from typing import Any

import pytest

from insurer.cli import main
from insurer.report import _kpis, render_report
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


def test_report_chart_supports_negative_monthly_values(results: dict[str, Any]):
    modified = copy.deepcopy(results)
    modified["monthly"][0]["gwp_cents"] = -5000
    modified["monthly"][0]["loss_ratio"] = -0.4

    html = render_report(modified)

    assert 'height="-' not in html
    assert "−€50.00" in html


def test_report_shows_final_decision_for_resolved_referral(results: dict[str, Any]):
    modified = copy.deepcopy(results)
    claim = modified["claims_sample"][0]
    claim["decision"] = "refer"
    claim["final_decision"] = "approve"

    html = render_report(modified)

    assert "data-decision='refer'" in html
    assert (
        "<td class='l'><span class='pill refer'>refer</span> → "
        "<span class='pill approve'>approve</span></td>"
    ) in html


def test_report_accepts_results_without_final_decision(results: dict[str, Any]):
    modified = copy.deepcopy(results)
    for claim in modified["claims_sample"]:
        claim.pop("final_decision")

    assert "<!doctype html>" in render_report(modified)


def test_kpis_label_zero_true_ibnr_as_not_applicable(results: dict[str, Any]):
    book = results["book"] | {"true_ibnr_cents": 0}

    html = _kpis(book)

    assert "true €0.00 (n/a)" in html
    assert "+0.0%" not in html


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
