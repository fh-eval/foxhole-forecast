from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .collector import collect_once
from .config import Settings
from .dashboard import build_dashboard_data
from .forecasting import run_forecast_cohort, salvage_invalid_run
from .foxholestats import SOURCE_URL, import_foxholestats_html
from .scoring import settle_and_score


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foxhole-forecast")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("collect", help="Poll the official War API once")
    forecast = subcommands.add_parser("forecast", help="Run a forecast cohort if due")
    forecast.add_argument("--force", action="store_true", help="Ignore the three-hour slot guard")
    forecast.add_argument("--series", help="Run only one configured model series")
    salvage = subcommands.add_parser(
        "salvage-run", help="Revalidate one invalid run from its stored response"
    )
    salvage.add_argument("--run-id", required=True)
    subcommands.add_parser("score", help="Settle matured forecasts and rebuild scores")
    subcommands.add_parser("build-dashboard", help="Generate the static dashboard JSON")
    importer = subcommands.add_parser("import-foxholestats", help="Import a saved FoxholeStats event-log page")
    importer.add_argument("--html", type=Path, required=True, help="Saved FoxholeStats HTML file")
    importer.add_argument("--source-url", default=SOURCE_URL, help="URL the saved page came from")
    run = subcommands.add_parser("run", help="Collect, forecast if due, score, and build dashboard")
    run.add_argument("--force-forecast", action="store_true")
    run.add_argument("--series", help="Run only one configured model series")
    args = parser.parse_args(argv)
    settings = Settings.load()

    try:
        if args.command == "collect":
            result = collect_once(settings)
        elif args.command == "forecast":
            result = run_forecast_cohort(settings, force=args.force, series_id=args.series)
        elif args.command == "salvage-run":
            result = salvage_invalid_run(settings, args.run_id)
        elif args.command == "score":
            result = settle_and_score(settings)
        elif args.command == "build-dashboard":
            result = build_dashboard_data()
        elif args.command == "import-foxholestats":
            result = import_foxholestats_html(args.html, settings, args.source_url)
        elif args.command == "run":
            result = {
                "collection": collect_once(settings),
                "forecast": run_forecast_cohort(
                    settings,
                    force=args.force_forecast,
                    series_id=args.series,
                ),
                "scores": settle_and_score(settings),
                "dashboard": build_dashboard_data(),
            }
        else:
            parser.error("Unknown command")
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
