"""Command line interface for the insurer sandbox."""

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import uvicorn

from insurer.agentrisk.contracts import (
    NormalizedEvidence,
    evidence_inputs,
    normalized_evidence,
)
from insurer.agentrisk.exam import (
    parse_promptfoo_results,
    run_exam,
)
from insurer.agentrisk.incidents import find_incidents
from insurer.agentrisk.manifest import AgentManifest, load_manifest
from insurer.agentrisk.service import (
    _agent_incident_evidence as _agent_incident_evidence_impl,
)
from insurer.agentrisk.service import (
    _validate_agent_claims_policy as _validate_agent_claims_policy_impl,
)
from insurer.agentrisk.service import (
    agent_scorecard_profile,
    score_from_evidence,
)
from insurer.agentrisk.service import (
    run_agent_claims as _run_agent_claims,
)
from insurer.agentrisk.traces import TraceData, load_trace_files
from insurer.api import create_app
from insurer.bordereaux import export_bordereau
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.policies import PolicyService
from insurer.products import Product, load_agent_ops_product, load_product
from insurer.recalibrate import recalibrate, write_candidate_product
from insurer.report import write_report
from insurer.simulate import simulate_book, write_results
from insurer.storage import connect


def _agent_scorecard_profile(scorecard: Any) -> dict[str, Any]:
    if isinstance(scorecard, (str, Path)):
        scorecard = json.loads(Path(scorecard).read_text(encoding="utf-8"))
    return agent_scorecard_profile(scorecard)


_agent_incident_evidence = _agent_incident_evidence_impl
_validate_agent_claims_policy = _validate_agent_claims_policy_impl


def run_agent_claims(
    claims: ClaimsService | None,
    policy_id: str,
    manifest: AgentManifest,
    trace_data: TraceData,
    notified_date: date,
    overrides: dict[str, int],
    product: Product,
) -> list[dict[str, Any]]:
    return _run_agent_claims(
        claims,
        policy_id,
        manifest,
        trace_data,
        notified_date,
        overrides,
        product,
        incident_finder=find_incidents,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insurer", description="Agent Spend Cover sandbox"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    simulate = commands.add_parser("simulate", help="simulate a policy book")
    simulate.add_argument("--agents", type=int, default=1000)
    simulate.add_argument("--months", type=int, default=12)
    simulate.add_argument("--seed", type=int, default=42)
    simulate.add_argument("--start", default="2027-01-01")
    simulate.add_argument("--product")
    simulate.add_argument("--llm-sample", type=int, default=0)
    simulate.add_argument("--db", default=":memory:")
    simulate.add_argument("--out", default="results.json")
    simulate.add_argument("--years", type=int, default=1000)
    simulate.add_argument("--quota-share", type=float, default=0.5)
    simulate.add_argument("--qs-commission", type=float, default=0.30)

    quote = commands.add_parser("quote", help="rate one profile")
    quote.add_argument("--profile", required=True, help="JSON risk profile")
    quote.add_argument("--start", default="2027-01-01")
    quote.add_argument("--db", default=":memory:")

    serve = commands.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=os.getenv("INSURER_DB", "insurer.db"))

    bordereaux = commands.add_parser("bordereaux", help="export a monthly CSV")
    bordereaux.add_argument("kind", choices=("premium", "claims"))
    bordereaux.add_argument("--month", required=True)
    bordereaux.add_argument("--db", default="insurer.db")

    trial_balance = commands.add_parser("trial-balance", help="show ledger balances")
    trial_balance.add_argument("--db", default="insurer.db")

    report = commands.add_parser("report", help="render a self-contained HTML report")
    report.add_argument("--results", default="results.json")
    report.add_argument("--out", default="report.html")

    recalibrate_command = commands.add_parser(
        "recalibrate", help="propose experience-based frequency factor changes"
    )
    recalibrate_command.add_argument("--db", required=True)
    recalibrate_command.add_argument("--product")
    recalibrate_command.add_argument("--as-of", type=date.fromisoformat)
    recalibrate_command.add_argument(
        "--full-credibility-claims", type=int, default=1082
    )
    recalibrate_command.add_argument("--max-change", type=float, default=0.25)
    recalibrate_command.add_argument("--out", default="proposal.json")
    recalibrate_command.add_argument("--write-product")
    recalibrate_command.add_argument("--effective-from", type=date.fromisoformat)

    agent_exam = commands.add_parser(
        "agent-exam", help="run the insurer-owned agent risk exam"
    )
    agent_exam.add_argument("--manifest", required=True)
    agent_exam.add_argument("--endpoint", required=True)
    agent_exam.add_argument("--out", required=True)
    agent_exam.add_argument("--battery")
    agent_exam.add_argument("--config-out")

    agent_score = commands.add_parser(
        "agent-score", help="compute an agent's per-peril evidence scorecard"
    )
    agent_score.add_argument("--manifest", required=True)
    agent_score.add_argument("--traces", nargs="*", default=[])
    agent_score.add_argument("--exam")
    agent_score.add_argument("--evals", nargs="*", default=[])
    agent_score.add_argument("--battery")
    agent_score.add_argument("--out", required=True)

    agent_evidence = commands.add_parser(
        "agent-evidence",
        help="normalize agent-risk evidence into its versioned contract",
    )
    agent_evidence.add_argument("--manifest", required=True)
    agent_evidence.add_argument("--traces", nargs="*", default=[])
    agent_evidence.add_argument("--exam")
    agent_evidence.add_argument("--evals", nargs="*", default=[])
    agent_evidence.add_argument("--out", required=True)

    agent_quote = commands.add_parser(
        "agent-quote", help="rate and optionally bind Agent Ops Cover"
    )
    agent_quote.add_argument("--db", required=True)
    agent_quote.add_argument("--scorecard", required=True)
    agent_quote.add_argument("--start-date", required=True, type=date.fromisoformat)
    agent_quote.add_argument("--product")
    agent_quote.add_argument("--bind", action="store_true")

    agent_claims = commands.add_parser(
        "agent-claims", help="find and adjust Agent Ops Cover incidents"
    )
    agent_claims.add_argument("--db", required=True)
    agent_claims.add_argument("--policy", required=True)
    agent_claims.add_argument("--manifest", required=True)
    agent_claims.add_argument("--traces", action="append", required=True)
    agent_claims.add_argument("--notified-date", required=True, type=date.fromisoformat)
    agent_claims.add_argument("--product")
    agent_claims.add_argument(
        "--claimed-cents", action="append", default=[], metavar="SPAN_ID=CENTS"
    )
    agent_claims.add_argument("--dry-run", action="store_true")

    return parser


def _print_proposal(proposal: dict[str, Any]) -> None:
    print(
        f"{'factor':<22} {'level':<12} {'exposure':>10} {'claims':>7} "
        f"{'current':>8} {'indicated [95% CI]':>30} {'Z':>7} {'proposed':>9}"
    )
    for row in proposal["factors"]:
        ci = row["ci95"]
        print(
            f"{row['factor']:<22} {row['level']:<12} "
            f"{row['exposure_years']:>10.3f} {row['claims']:>7d} "
            f"{row['current']:>8.2f} "
            f"{row['indicated']:.2f} [{ci[0]:.2f}, {ci[1]:.2f}] "
            f"{row['credibility']:>7.3f} {row['proposed']:>9.2f}"
        )


def _print_agent_score(scorecard: dict[str, Any]) -> None:
    print(f"{'peril':<8} {'exam':<12} {'controls':<12} {'grade':>6}")
    for peril in scorecard["perils"]:
        print(
            f"{peril['id']:<8} {peril['exam_level']:<12} "
            f"{peril['control_level']:<12} {str(peril['grade'] or '—'):>6}"
        )
    print(f"{'agent':<8} {'':<12} {'':<12} {str(scorecard['agent_grade'] or '—'):>6}")


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "simulate":
        try:
            result = simulate_book(
                agents=args.agents,
                months=args.months,
                seed=args.seed,
                start=args.start,
                llm_sample=args.llm_sample,
                product=load_product(args.product) if args.product else None,
                database=args.db,
                years=args.years,
                quota_share=args.quota_share,
                qs_commission=args.qs_commission,
            )
        except ValueError as error:
            parser.error(str(error))
        write_results(result, args.out)
        print(f"Wrote {args.out}")
    elif args.command == "quote":
        profile: dict[str, Any] = json.loads(args.profile)
        connection = connect(args.db)
        print(
            json.dumps(PolicyService(connection).quote(profile, args.start), indent=2)
        )
    elif args.command == "serve":
        os.environ["INSURER_DB"] = args.db
        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
    elif args.command == "bordereaux":
        connection = connect(args.db)
        print(export_bordereau(connection, args.kind, args.month), end="")
    elif args.command == "trial-balance":
        connection = connect(args.db)
        print(json.dumps(Ledger(connection).trial_balance(), indent=2))
    elif args.command == "report":
        try:
            write_report(args.results, args.out)
        except FileNotFoundError as error:
            parser.error(str(error))
        print(f"Wrote {args.out}")
    elif args.command == "recalibrate":
        if not Path(args.db).is_file():
            parser.error(f"database file not found: {args.db}")
        database_path = Path(args.db).resolve()
        if Path(args.out).resolve() == database_path:
            parser.error("--out must not overwrite the database file")
        if args.write_product and Path(args.write_product).resolve() == database_path:
            parser.error("--write-product must not overwrite the database file")
        if args.effective_from and not args.write_product:
            parser.error("--effective-from requires --write-product")
        product = load_product(args.product) if args.product else load_product()
        try:
            proposal = recalibrate(
                args.db,
                product,
                as_of=args.as_of,
                full_credibility_claims=args.full_credibility_claims,
                max_change=args.max_change,
            )
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as destination:
            json.dump(proposal, destination, indent=2)
            destination.write("\n")
        _print_proposal(proposal)
        print(f"Wrote {args.out}")
        if args.write_product:
            effective_from = args.effective_from or (
                date.fromisoformat(proposal["as_of"]) + timedelta(days=1)
            )
            write_candidate_product(
                product, proposal, args.write_product, effective_from
            )
            print(f"Wrote {args.write_product}")
    elif args.command == "agent-exam":
        try:
            run_exam(
                manifest=load_manifest(args.manifest),
                endpoint=args.endpoint,
                out=args.out,
                battery_path=args.battery,
                config_out=args.config_out,
            )
        except (OSError, RuntimeError, ValueError) as error:
            parser.error(str(error))
        print(f"Wrote {args.out}")
    elif args.command == "agent-score":
        try:
            manifest_path = Path(args.manifest)
            manifest = load_manifest(manifest_path)
            trace_data = (
                load_trace_files(args.traces, manifest)
                if args.traces
                else TraceData((), ())
            )
            exam_results = (
                parse_promptfoo_results(args.exam) if args.exam is not None else []
            )
            eval_results = [
                result
                for path in args.evals
                for result in parse_promptfoo_results(path)
            ]
            evidence = NormalizedEvidence(
                trace_data=trace_data,
                exam_results=exam_results,
                ci_eval_results=eval_results,
                exam_present=args.exam is not None,
                inputs=evidence_inputs(
                    args.manifest, args.traces, args.exam, args.evals
                ),
            )
            scorecard = score_from_evidence(
                manifest,
                evidence,
                Path(args.battery) if args.battery is not None else None,
            )
            output_path = Path(args.out)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(scorecard, indent=2, sort_keys=True), encoding="utf-8"
            )
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
        _print_agent_score(scorecard)
        print(f"Wrote {args.out}")
    elif args.command == "agent-evidence":
        try:
            manifest = load_manifest(args.manifest)
            trace_data = (
                load_trace_files(args.traces, manifest)
                if args.traces
                else TraceData((), ())
            )
            exam_results = (
                parse_promptfoo_results(args.exam) if args.exam is not None else []
            )
            eval_results = [
                result
                for path in args.evals
                for result in parse_promptfoo_results(path)
            ]
            inputs = evidence_inputs(args.manifest, args.traces, args.exam, args.evals)
            normalized_document = normalized_evidence(
                trace_data,
                exam_results,
                eval_results,
                exam_present=args.exam is not None,
                inputs=inputs,
            )
            output_path = Path(args.out)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(normalized_document, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(f"Wrote {args.out}")
    elif args.command == "agent-quote":
        try:
            scorecard = json.loads(Path(args.scorecard).read_text(encoding="utf-8"))
            profile = agent_scorecard_profile(scorecard)
            connection = connect(args.db)
            policies = PolicyService(connection, load_agent_ops_product(args.product))
            quote = policies.quote(profile, args.start_date)
            quote_output: dict[str, Any] = {"quote": quote}
            if args.bind and quote["status"] == "quoted":
                quote_output["policy"] = policies.bind(quote["quote_id"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.error(str(error))
        print(json.dumps(quote_output, indent=2))
    elif args.command == "agent-claims":
        try:
            manifest = load_manifest(args.manifest)
            trace_data = load_trace_files(args.traces, manifest)
            overrides: dict[str, int] = {}
            for item in args.claimed_cents:
                span_id, separator, amount = item.partition("=")
                if not separator:
                    raise ValueError("--claimed-cents must use SPAN_ID=CENTS format")
                overrides[span_id] = int(amount)
            product = (
                load_agent_ops_product()
                if args.dry_run
                else load_agent_ops_product(args.product)
            )
            claims: ClaimsService | None = None
            if not args.dry_run:
                claims = ClaimsService(connect(args.db), product)
            claims_output = run_agent_claims(
                claims,
                args.policy,
                manifest,
                trace_data,
                args.notified_date,
                overrides,
                product,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.error(str(error))
        print(json.dumps(claims_output, indent=2))


if __name__ == "__main__":
    main()
