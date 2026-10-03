"""Command line interface for the insurer sandbox."""

import argparse
import json
import os
from typing import Any

import uvicorn

from insurer.api import create_app
from insurer.bordereaux import export_bordereau
from insurer.ledger import Ledger
from insurer.policies import PolicyService
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
    return parser


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


if __name__ == "__main__":
    main()
