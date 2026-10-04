"""Command line interface for the insurer sandbox."""

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import uvicorn

from insurer.api import create_app
from insurer.bordereaux import export_bordereau
from insurer.ledger import Ledger
from insurer.policies import PolicyService
from insurer.products import load_product
from insurer.recalibrate import recalibrate, write_candidate_product
from insurer.report import write_report
from insurer.simulate import simulate_book, write_results
from insurer.storage import connect


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
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as destination:
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


if __name__ == "__main__":
    main()
