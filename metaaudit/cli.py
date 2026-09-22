"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__, apply as apply_mod, plan as plan_mod, preflight, snapshot_io
from .api import GraphClient, GraphError
from .checks import all_checks, run_all
from .config import Settings, Thresholds, load_env_file
from .fetch import fetch_snapshot
from .report import RENDERERS


def _runner() -> str:
    return "py" if os.name == "nt" else "python3"


def _apply(args: argparse.Namespace, client: GraphClient) -> int:
    """Apply a reviewed plan. Confirms first unless --yes."""
    try:
        plan = plan_mod.loads(Path(args.apply).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: could not read plan: {exc}", file=sys.stderr)
        return 2

    print(plan_mod.render(plan))
    print()

    if plan.is_empty:
        print("plan contains no changes; nothing to do.", file=sys.stderr)
        return 0

    if not args.yes and not args.dry_run:
        if not sys.stdin.isatty():
            print(
                "error: --apply needs a terminal to confirm. Pass --yes if you "
                "have already reviewed this plan.",
                file=sys.stderr,
            )
            return 2
        print(
            f"About to change {len(plan.changes)} object(s) in {plan.account_id}.",
            file=sys.stderr,
        )
        answer = input("Type 'apply' to proceed: ").strip()
        if answer != "apply":
            print("aborted; nothing was changed.", file=sys.stderr)
            return 1

    rollback = args.rollback_out
    if rollback is None:
        rollback = str(Path(args.apply).with_suffix(".rollback.json"))

    outcomes = apply_mod.apply_plan(
        client, plan, rollback_path=rollback, dry_run=args.dry_run
    )
    print(apply_mod.summarize(outcomes, plan.currency))

    if not args.dry_run:
        print(f"\nundo file: {rollback}", file=sys.stderr)
    if any(o.state == "failed" for o in outcomes):
        return 3
    if any(o.state == "skipped" and o.detail != "dry run" for o in outcomes):
        return 1
    return 0


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
            "--plan proposes changes without writing anything; only --apply "
            "writes,\nand only the structural fixes a plan file already spells "
            "out.\n"
            "Credentials come from the environment or --env-file, never from "
            "arguments,\nso they do not land in your shell history."
        ),
    )
    parser.add_argument("--version", action="version", version=f"metaaudit {__version__}")
    parser.add_argument("--account", help="Ad account id (act_123... or 123...)")
    parser.add_argument("--env-file", help="Read credentials from this .env file")
    parser.add_argument("--api-version", help="Graph API version, e.g. v23.0")
    parser.add_argument(
        "--window", type=int, default=30, metavar="DAYS",
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
        "--fail-on", choices=["never", "critical", "high", "medium", "low", "any"],
        default="never",
        help="Exit non-zero when a finding at or above this severity exists (for CI)",
    )
    write = parser.add_argument_group(
        "changing the account",
        "Off by default. --plan writes nothing; --apply writes only what a "
        "plan file lists.",
    )
    write.add_argument(
        "--plan", metavar="PATH",
        help="Write a reviewable change plan here instead of only reporting",
    )
    write.add_argument(
        "--apply", metavar="PATH",
        help="Apply a plan file. Requires confirmation unless --yes is given.",
    )
    write.add_argument(
        "--allow-budget-increase", type=float, default=0.0, metavar="AMOUNT",
        help=(
            "Daily budget increase the plan may propose, in account currency. "
            "Default 0: consolidation is free and needs no allowance, raising "
            "a budget spends real money every day and needs a number here."
        ),
    )
    write.add_argument(
        "--rollback-out", metavar="PATH",
        help="Where --apply writes the undo file (default: alongside the plan)",
    )
    write.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt for --apply (for scripts)",
    )
    write.add_argument(
        "--dry-run", action="store_true",
        help="With --apply: verify every current value, write nothing",
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

    if args.apply and args.from_snapshot:
        print(
            "error: --apply writes to the live account; it cannot run against "
            "a saved snapshot",
            file=sys.stderr,
        )
        return 2

    if args.apply and args.plan:
        print(
            "error: --plan and --apply are separate steps. Build the plan, "
            "read it, then apply it.",
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

    if args.from_snapshot:
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
                window_days=args.window,
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
            code, report = preflight.run(
                client, settings.ad_account_id, env_file=args.env_file
            )
            print(report)
            return code

        if args.apply:
            return _apply(args, client)

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

        if args.save_snapshot:
            written = snapshot_io.save(snapshot, args.save_snapshot)
            print(
                f"snapshot written to {written} (contains account data — it is "
                f"gitignored by default)",
                file=sys.stderr,
            )

    results = run_all(snapshot, thresholds, only=args.only)

    if args.plan:
        built = plan_mod.build_plan(
            snapshot, results, budget_allowance=args.allow_budget_increase
        )
        Path(args.plan).parent.mkdir(parents=True, exist_ok=True)
        Path(args.plan).write_text(plan_mod.dumps(built), encoding="utf-8")
        print(plan_mod.render(built))
        print()
        print(f"plan written to {args.plan}", file=sys.stderr)
        if built.is_empty:
            print("nothing to apply.", file=sys.stderr)
        else:
            print(
                f"review it, then apply with:\n"
                f"  {_runner()} -m metaaudit"
                + (f" --env-file {args.env_file}" if args.env_file else "")
                + f" --apply {args.plan}",
                file=sys.stderr,
            )
        return 0

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
