"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__, demo, preflight, snapshot_io
from .api import GraphClient, GraphError
from .checks import all_checks, run_all
from .config import Settings, Thresholds, load_env_file
from .fetch import fetch_snapshot
from .report import RENDERERS

DEFAULT_WINDOW_DAYS = 30


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metaaudit",
        description=(
            "Read-only audit of a Meta ad account. Finds structural waste and "
            "refuses to make claims the sample size cannot support."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The audit never writes to the account: it issues GET requests only.\n"
            "Credentials come from the environment or --env-file, never from "
            "arguments,\nso they do not land in your shell history."
        ),
    )
    parser.add_argument("--version", action="version", version=f"metaaudit {__version__}")
    parser.add_argument("--account", help="Ad account id (act_123... or 123...)")
    parser.add_argument("--env-file", help="Read credentials from this .env file")
    parser.add_argument("--api-version", help="Graph API version, e.g. v23.0")
    parser.add_argument(
        "--window", type=int, default=None, metavar="DAYS",
        help="Audit window in days, ending yesterday (default: 30)",
    )
    parser.add_argument(
        "--include-paused", action="store_true",
        help="Also fetch paused entities (slower; most findings only apply to delivering ones)",
    )
    parser.add_argument(
        "--format", choices=sorted(RENDERERS), default="text", help="Output format"
    )
    parser.add_argument("--out", help="Write the report here instead of stdout")
    parser.add_argument(
        "--only", action="append", metavar="CHECK_ID",
        help="Run only this check (repeatable). --list-checks shows the ids.",
    )
    parser.add_argument("--list-checks", action="store_true", help="List check ids and exit")
    parser.add_argument(
        "--check-auth", action="store_true",
        help="Verify the token and account with two cheap reads, then exit",
    )
    parser.add_argument("--thresholds", help="JSON file overriding the default thresholds")
    parser.add_argument(
        "--save-snapshot", metavar="PATH",
        help="Write the fetched data here so checks can be re-run offline",
    )
    parser.add_argument(
        "--from-snapshot", metavar="PATH",
        help="Run the checks against a saved snapshot; makes no API calls",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help=(
            "Audit a synthetic account instead of a real one. Needs no token "
            "and makes no API calls — it shows what the report looks like."
        ),
    )
    parser.add_argument(
        "--fail-on", choices=["never", "critical", "high", "medium", "low", "any"],
        default="never",
        help="Exit non-zero when a finding at or above this severity exists (for CI)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def _exit_code(findings, fail_on: str) -> int:
    if fail_on == "never":
        return 0
    from .checks.base import Severity

    floor = {
        "any": Severity.INFO,
        "low": Severity.LOW,
        "medium": Severity.MEDIUM,
        "high": Severity.HIGH,
        "critical": Severity.CRITICAL,
    }[fail_on]
    return 1 if any(f.severity >= floor for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.list_checks:
        for check_id, title, _ in all_checks():
            print(f"{check_id:34s} {title}")
        return 0

    try:
        thresholds = Thresholds.load(args.thresholds)
    except (OSError, ValueError) as exc:
        print(f"error: bad --thresholds file: {exc}", file=sys.stderr)
        return 2

    known = {check_id for check_id, _, _ in all_checks()}
    if args.only:
        unknown = set(args.only) - known
        if unknown:
            print(
                f"error: unknown check id(s): {', '.join(sorted(unknown))}\n"
                f"run --list-checks to see valid ids",
                file=sys.stderr,
            )
            return 2

    if args.from_snapshot and args.check_auth:
        print(
            "error: --check-auth talks to the API; it cannot run against a "
            "saved snapshot",
            file=sys.stderr,
        )
        return 2

    if args.demo:
        # Refused rather than quietly ignored: silently dropping --account
        # would let someone believe they had just audited the account they
        # named.
        for flag, present, why in (
            ("--from-snapshot", bool(args.from_snapshot),
             "both name where the data comes from; pick one"),
            ("--check-auth", args.check_auth,
             "the demo makes no API calls, so there is no token to check"),
            ("--account", bool(args.account),
             "the demo audits a synthetic account, not one of yours"),
            ("--window", args.window is not None,
             f"the demo data covers a fixed {demo.WINDOW_DAYS}-day window"),
        ):
            if present:
                print(
                    f"error: --demo cannot be combined with {flag}: {why}",
                    file=sys.stderr,
                )
                return 2

    window_days = DEFAULT_WINDOW_DAYS if args.window is None else args.window

    if args.demo:
        snapshot = demo.build_demo_account()
    elif args.from_snapshot:
        try:
            snapshot = snapshot_io.load(args.from_snapshot)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"error: could not read snapshot: {exc}", file=sys.stderr)
            return 2
    else:
        env = dict(os.environ)
        if args.env_file:
            try:
                env.update(load_env_file(args.env_file))
            except (OSError, ValueError) as exc:
                print(f"error: bad --env-file: {exc}", file=sys.stderr)
                return 2
        try:
            settings = Settings.from_env(
                account=args.account,
                api_version=args.api_version,
                window_days=window_days,
                thresholds=thresholds,
                env=env,
            )
        except SystemExit as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        client = GraphClient(
            settings.access_token,
            settings.api_version,
            app_secret=settings.app_secret,
        )
        if args.check_auth:
            code, report = preflight.run(client, settings.ad_account_id)
            print(report)
            return code

        try:
            snapshot = fetch_snapshot(
                client,
                settings.ad_account_id,
                window_days=settings.window_days,
                include_paused=args.include_paused,
            )
        except GraphError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3

    if args.save_snapshot and not args.from_snapshot:
        written = snapshot_io.save(snapshot, args.save_snapshot)
        note = (
            "synthetic data — safe to share"
            if args.demo
            else "contains account data — it is gitignored by default"
        )
        print(f"snapshot written to {written} ({note})", file=sys.stderr)

    results = run_all(snapshot, thresholds, only=args.only)
    report = RENDERERS[args.format](snapshot, results, thresholds)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(report)

    from .report import collect

    return _exit_code(collect(results), args.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
