"""Command-line entry point (task 2.8).

``argparse`` rather than Click or Typer, deliberately. Task 0.1 pins exact dependency versions and
the
project has no CLI framework yet; adding one for a single ``seed`` subcommand would put a dependency
in
the runtime install path to save perhaps twenty lines. The subparser structure below is what the
later
phases attach to — ``c360 recompute`` in task 3.6 and ``c360 eval`` in task 11.11 — so the shape is
set
up for them rather than for this one command.

Configuration is *advisory* here
--------------------------------

:func:`c360.core.config.get_settings` fails fast on incomplete configuration, which is correct for
the
API: a process that will call Bedrock should not start without a model ID. It is the wrong behaviour
for
the seeder, which touches no model and no network. A developer who has not filled in
``BEDROCK_MODEL_ID`` should still be able to build a database.

So settings are consulted for defaults and a failure to load them is reported and stepped over,
rather
than being fatal. The values that matter to seeding — the database path, the customer count, the
seed —
all have explicit defaults and can all be passed on the command line.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Final

from c360.core.config import ConfigurationError, Settings, get_settings
from c360.data.engine import DatabaseFileMissingError
from c360.data.recompute import RecomputeError, recompute
from c360.domain.dates import InvalidIsoDateError, from_iso
from c360.generator.context import DEFAULT_AS_OF
from c360.generator.pipeline import DatabaseExistsError, seed

#: Fallbacks used when settings cannot be loaded. Kept in step with the defaults on
#: :class:`c360.core.config.Settings`.
_FALLBACK_DB: Final = Path("data/customer.db")
_FALLBACK_KNOWLEDGE_DB: Final = Path("data/knowledge.db")
_FALLBACK_SIGNALS_DB: Final = Path("data/signals.db")
_FALLBACK_REPORTS_DB: Final = Path("data/reports.db")
_FALLBACK_REPORTS_DIR: Final = Path("data/reports")
_FALLBACK_COUNT: Final = 100
_FALLBACK_SEED: Final = 42

_EXIT_OK: Final = 0
_EXIT_ERROR: Final = 1


def _load_settings() -> Settings | None:
    """Return settings, or ``None`` when configuration is unusable.

    The warning goes to stderr rather than through the logging subsystem: at this point logging has
    not
    been configured, and the user is at a prompt where a plain message is more useful than a JSON
    log
    line.
    """
    try:
        return get_settings()
    except ConfigurationError as error:
        print(
            f"warning: configuration could not be loaded, using defaults.\n{error}\n",
            file=sys.stderr,
        )
        return None


def _parse_as_of(value: str) -> date:
    """``--as-of`` parser that rejects everything the database's date columns would reject."""
    try:
        return from_iso(value)
    except InvalidIsoDateError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _build_parser(settings: Settings | None) -> argparse.ArgumentParser:
    default_db = settings.customer_db_path if settings else _FALLBACK_DB
    default_count = settings.seed_customer_count if settings else _FALLBACK_COUNT
    default_seed = settings.seed_random_seed if settings else _FALLBACK_SEED

    parser = argparse.ArgumentParser(
        prog="c360",
        description="Customer 360 Intelligence Platform — data and maintenance commands.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser(
        "seed",
        help="Generate a synthetic customer database.",
        description=(
            "Generate and load a seeded synthetic dataset. The same --count and --seed always "
            "produce a byte-identical database."
        ),
    )
    seed_parser.add_argument(
        "--count",
        type=int,
        default=default_count,
        help=f"Number of customers (default: {default_count}).",
    )
    seed_parser.add_argument(
        "--seed",
        type=int,
        default=default_seed,
        help=f"Random seed (default: {default_seed}).",
    )
    seed_parser.add_argument(
        "--database",
        type=Path,
        default=default_db,
        help=f"Target SQLite file (default: {default_db}).",
    )
    seed_parser.add_argument(
        "--as-of",
        type=_parse_as_of,
        default=DEFAULT_AS_OF,
        metavar="YYYY-MM-DD",
        help=(
            "Date every generated row is current at "
            f"(default: {DEFAULT_AS_OF.isoformat()}). Fixed rather than today's date so output is "
            "reproducible across days."
        ),
    )
    seed_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace the database if it already exists.",
    )

    recompute_parser = subparsers.add_parser(
        "recompute",
        help="Rebuild derived values, the search index and the graph projection.",
        description=(
            "Rebuild every derived_* table, the FTS5 customer_search index and the graph "
            "projection from the source tables. Idempotent: safe to run repeatedly, and the "
            "stored values it writes always equal a fresh computation (design §14.6)."
        ),
    )
    recompute_parser.add_argument(
        "--database",
        type=Path,
        default=default_db,
        help=f"Target SQLite file (default: {default_db}).",
    )

    default_signals_db = settings.signals_db_path if settings else _FALLBACK_SIGNALS_DB
    detect_parser = subparsers.add_parser(
        "detect-signals",
        help="Detect proactive signals and write the worklist store (Phase 17).",
        description=(
            "Run every deterministic signal detector over the read-only customer database, rank "
            "the results and upsert them into the writable signals database. Idempotent: every "
            "signal carries a deterministic dedup_key, so re-running over unchanged data updates "
            "rather than duplicates a live signal (task 17.4). The customer database is opened "
            "read-only and is never written."
        ),
    )
    detect_parser.add_argument(
        "--database",
        type=Path,
        default=default_db,
        help=f"Source customer SQLite file, opened read-only (default: {default_db}).",
    )
    detect_parser.add_argument(
        "--signals-database",
        type=Path,
        default=default_signals_db,
        help=f"Target writable signals SQLite file (default: {default_signals_db}).",
    )

    default_knowledge_db = settings.knowledge_db_path if settings else _FALLBACK_KNOWLEDGE_DB
    default_dimensions = settings.bedrock_embed_dimensions if settings else 1024
    default_target_tokens = settings.chunk_target_tokens if settings else 500
    default_overlap = settings.chunk_overlap_ratio if settings else 0.15

    ingest_parser = subparsers.add_parser(
        "ingest-knowledge",
        help="Build the knowledge database from the source corpus.",
        description=(
            "Read the knowledge/ corpus, chunk each document (heading-aware, ~500 tokens, ~15% "
            "overlap), embed the chunks and write knowledge.db (FTS5 + sqlite-vec). Idempotent: "
            "the schema is dropped and rebuilt, and chunk IDs are deterministic so re-ingestion is "
            "byte-stable. Uses the mock embedding provider by default so it runs offline with no "
            "AWS; pass --provider bedrock for real Titan embeddings."
        ),
    )
    ingest_parser.add_argument(
        "--database",
        type=Path,
        default=default_knowledge_db,
        help=f"Target knowledge SQLite file (default: {default_knowledge_db}).",
    )
    ingest_parser.add_argument(
        "--provider",
        choices=("mock", "bedrock"),
        default="mock",
        help="Embedding provider (default: mock).",
    )
    ingest_parser.add_argument(
        "--dimensions",
        type=int,
        choices=(256, 512, 1024),
        default=default_dimensions,
        help=f"Embedding dimensionality (default: {default_dimensions}).",
    )
    ingest_parser.add_argument(
        "--target-tokens",
        type=int,
        default=default_target_tokens,
        help=f"Soft per-chunk token target (default: {default_target_tokens}).",
    )
    ingest_parser.add_argument(
        "--overlap-ratio",
        type=float,
        default=default_overlap,
        help=f"Chunk overlap fraction (default: {default_overlap}).",
    )

    default_reports_db = settings.reports_db_path if settings else _FALLBACK_REPORTS_DB
    default_reports_dir = settings.reports_output_path if settings else _FALLBACK_REPORTS_DIR
    reports_parser = subparsers.add_parser(
        "run-reports",
        help="Run every due report schedule and deliver via the mock deliverer (Phase 18).",
        description=(
            "Run each due report_schedule row over the read-only customer database, re-checking "
            "each schedule owner's live entitlement at generation time, writing the branded "
            "artifact to the reports output directory and a delivery manifest via the offline file "
            "deliverer. Runs offline on LLM_PROVIDER=mock; the customer database is opened "
            "read-only and never written (task 18.4)."
        ),
    )
    reports_parser.add_argument(
        "--database",
        type=Path,
        default=default_db,
        help=f"Source customer SQLite file, opened read-only (default: {default_db}).",
    )
    reports_parser.add_argument(
        "--reports-database",
        type=Path,
        default=default_reports_db,
        help=f"Target writable reports SQLite file (default: {default_reports_db}).",
    )
    reports_parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_reports_dir,
        help=f"Directory artifacts and manifests are written to (default: {default_reports_dir}).",
    )

    _add_eval_parser(subparsers)
    return parser


def _add_eval_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Attach ``c360 eval run|report|compare|promote`` (task 11.11, design §14.6).

    A nested subparser under ``eval`` mirrors the design's command shape. ``run`` executes an
    evaluation and gates on the result; ``report`` renders a stored run; ``compare`` diffs a fresh
    challenger run against the stored baseline; ``promote`` sets the baseline when the
    not regress it.
    """
    eval_parser = subparsers.add_parser(
        "eval",
        help="Run the agent evaluation framework (Phase 11).",
        description=(
            "Run the deterministic and quality evaluation suite over a seeded panel, store the "
            "scored run, and compare or promote against the baseline (design §14)."
        ),
    )
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)

    run_p = eval_sub.add_parser("run", help="Run an evaluation and gate on the hard dimensions.")
    run_p.add_argument(
        "--mode",
        choices=("ci", "full"),
        default="ci",
        help="ci (mock, deterministic gates) or full (Bedrock, all 16 dimensions). Default: ci.",
    )
    run_p.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="Report format printed after the run (default: md).",
    )
    run_p.add_argument(
        "--set-baseline",
        action="store_true",
        help="On a passing run, set it as the comparison baseline.",
    )

    report_p = eval_sub.add_parser("report", help="Render a stored run's report.")
    report_p.add_argument("--run", default=None, help="Run id (default: the latest run).")
    report_p.add_argument("--format", choices=("md", "json"), default="md")

    compare_p = eval_sub.add_parser(
        "compare", help="Run a challenger and diff it against the baseline."
    )
    compare_p.add_argument("--mode", choices=("ci", "full"), default="full")
    compare_p.add_argument("--format", choices=("md", "json"), default="md")

    promote_p = eval_sub.add_parser(
        "promote", help="Run a challenger and promote it to baseline unless it regresses."
    )
    promote_p.add_argument("--mode", choices=("ci", "full"), default="full")


def _run_seed(args: argparse.Namespace) -> int:
    try:
        report = seed(
            database=args.database,
            count=args.count,
            seed=args.seed,
            as_of=args.as_of,
            force=args.force,
        )
    except DatabaseExistsError as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR
    except ValueError as error:
        # Raised by the cohort allocator when the count is too small to contain all eight personas.
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR

    for line in report.summary_lines():
        print(line)
    return _EXIT_OK


def _run_detect_signals(args: argparse.Namespace, settings: Settings | None) -> int:
    """Run the batch signal-detection job (task 17.4).

    Settings supply the detection thresholds and ranking weights; when they cannot be loaded (a
    developer without a full ``.env``) the job falls back to the ``Settings`` field defaults, the
    same way seeding proceeds on defaults — detection touches no model and no network.
    """
    from c360.core.config import Settings as _Settings  # noqa: PLC0415
    from c360.signals.detect import detect_config_from_settings, detect_signals  # noqa: PLC0415

    effective = settings or _Settings.model_construct()
    try:
        provenance = detect_signals(
            customer_db_path=args.database,
            signals_db_path=args.signals_database,
            config=detect_config_from_settings(effective),
        )
    except DatabaseFileMissingError as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR

    for line in provenance.summary_lines():
        print(line)
    return _EXIT_OK


def _run_reports(args: argparse.Namespace, settings: Settings | None) -> int:
    """Run every due report schedule (task 18.4).

    Builds the service container over the customer database, the report service over the writable
    reports database, and the agent runtime (mock offline, unless settings select Bedrock). Each due
    schedule's owner is re-resolved to their live principal via the seeded local provider, so a
    scheduled run is scoped and masked exactly as that user's interactive requests.
    """
    from c360.agents.runtime import build_agent_runtime  # noqa: PLC0415
    from c360.api.services import build_services  # noqa: PLC0415
    from c360.core.config import Settings as _Settings  # noqa: PLC0415
    from c360.reports.service import PrincipalResolver, build_report_service  # noqa: PLC0415
    from c360.security.local_provider import principal_for_user  # noqa: PLC0415

    effective = settings or _Settings.model_construct()
    try:
        services = build_services(effective)
    except DatabaseFileMissingError as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR

    built = build_report_service(
        args.reports_database,
        output_dir=args.output_dir,
        max_customers=effective.reports_max_customers_per_run,
        pool_size=effective.sqlite_read_pool_size,
    )
    if built is None:  # pragma: no cover - build_report_service self-initializes the file
        print("error: could not open the reports database.", file=sys.stderr)
        return _EXIT_ERROR
    report_service, _engine = built
    runtime = build_agent_runtime(effective)
    resolver = PrincipalResolver(principal_for_user)
    summary = report_service.run_due_schedules(resolver, services, runtime)
    for line in summary.summary_lines():
        print(line)
    return _EXIT_OK


def _run_recompute(args: argparse.Namespace) -> int:
    try:
        report = recompute(database=args.database)
    except DatabaseFileMissingError as error:
        # The database has to exist and be seeded before there is anything to project.
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR
    except RecomputeError as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR

    for line in report.summary_lines():
        print(line)
    return _EXIT_OK


def _run_ingest_knowledge(args: argparse.Namespace, settings: Settings | None) -> int:
    from c360.knowledge.corpus import CorpusError  # noqa: PLC0415
    from c360.knowledge.embeddings import (  # noqa: PLC0415
        EmbeddingProvider,
        build_embedding_provider,
    )
    from c360.knowledge.extension import VecExtensionError  # noqa: PLC0415
    from c360.knowledge.ingest import IngestionError, ingest_knowledge  # noqa: PLC0415

    provider: EmbeddingProvider | None = None
    if args.provider == "bedrock":
        if settings is None:
            print(
                "error: --provider bedrock needs valid configuration (AWS region, embed model).",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        provider = build_embedding_provider(settings)

    try:
        report = ingest_knowledge(
            database=args.database,
            provider=provider,
            dimensions=args.dimensions,
            target_tokens=args.target_tokens,
            overlap_ratio=args.overlap_ratio,
        )
    except (CorpusError, IngestionError, VecExtensionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_ERROR

    for line in report.summary_lines():
        print(line)
    return _EXIT_OK


def _render_run(record: object, fmt: str) -> str:
    """Render a run record as Markdown or JSON for the CLI."""
    import json  # noqa: PLC0415

    from c360.eval.report import render_json, render_markdown  # noqa: PLC0415
    from c360.eval.results import RunRecord  # noqa: PLC0415

    assert isinstance(record, RunRecord)  # noqa: S101
    if fmt == "json":
        return json.dumps(render_json(record), indent=2)
    return render_markdown(record)


def _eval_run(args: argparse.Namespace, settings: Settings) -> int:
    import asyncio  # noqa: PLC0415

    from c360.eval.runner import EvalMode, run_evaluation  # noqa: PLC0415
    from c360.eval.store import EvalStore  # noqa: PLC0415

    store = EvalStore(settings.eval_db_path)
    record = asyncio.run(run_evaluation(settings, EvalMode(args.mode)))
    store.save_run(record)
    if args.set_baseline and record.passed:
        store.set_baseline(record.run_id)
    print(_render_run(record, args.format))
    return _EXIT_OK if record.passed else _EXIT_ERROR


def _eval_report(args: argparse.Namespace, settings: Settings) -> int:
    from c360.eval.store import EvalStore  # noqa: PLC0415

    store = EvalStore(settings.eval_db_path)
    record = store.get_run(args.run) if args.run else store.latest_run()
    if record is None:
        print("error: no such run.", file=sys.stderr)
        return _EXIT_ERROR
    print(_render_run(record, args.format))
    return _EXIT_OK


def _eval_compare(args: argparse.Namespace, settings: Settings) -> int:
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415
    from dataclasses import asdict  # noqa: PLC0415

    from c360.eval.report import (  # noqa: PLC0415
        diff_runs,
        regression_check,
        render_comparison_markdown,
    )
    from c360.eval.runner import EvalMode, run_evaluation  # noqa: PLC0415
    from c360.eval.store import EvalStore  # noqa: PLC0415

    store = EvalStore(settings.eval_db_path)
    baseline = store.baseline_run()
    if baseline is None:
        print("error: no baseline set; run 'eval run --set-baseline' first.", file=sys.stderr)
        return _EXIT_ERROR
    challenger = asyncio.run(run_evaluation(settings, EvalMode(args.mode)))
    store.save_run(challenger)
    tolerance = settings.eval_regression_tolerance_pts
    deltas = diff_runs(baseline, challenger, tolerance_pts=tolerance)
    promotable, _blocking = regression_check(baseline, challenger, tolerance_pts=tolerance)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "baseline": baseline.run_id,
                    "challenger": challenger.run_id,
                    "promotable": promotable,
                    "deltas": [asdict(d) for d in deltas],
                },
                indent=2,
            )
        )
    else:
        print(render_comparison_markdown(baseline, challenger, deltas, promotable=promotable))
    return _EXIT_OK


def _eval_promote(args: argparse.Namespace, settings: Settings) -> int:
    import asyncio  # noqa: PLC0415

    from c360.eval.report import regression_check  # noqa: PLC0415
    from c360.eval.runner import EvalMode, run_evaluation  # noqa: PLC0415
    from c360.eval.store import EvalStore  # noqa: PLC0415

    store = EvalStore(settings.eval_db_path)
    baseline = store.baseline_run()
    challenger = asyncio.run(run_evaluation(settings, EvalMode(args.mode)))
    store.save_run(challenger)
    if baseline is None:
        if challenger.passed:
            store.set_baseline(challenger.run_id)
            print(f"promoted {challenger.run_id} as the initial baseline.")
            return _EXIT_OK
        print("error: challenger failed its hard gates; not promoted.", file=sys.stderr)
        return _EXIT_ERROR
    promotable, blocking = regression_check(
        baseline, challenger, tolerance_pts=settings.eval_regression_tolerance_pts
    )
    if not promotable:
        print("promotion blocked -- regressions:", file=sys.stderr)
        for delta in blocking:
            print(f"  - {delta.name} (dim {delta.dimension})", file=sys.stderr)
        return _EXIT_ERROR
    store.set_baseline(challenger.run_id)
    print(f"promoted {challenger.run_id} over {baseline.run_id}.")
    return _EXIT_OK


#: Eval subcommand dispatch table, so the top-level ``_run_eval`` stays a simple lookup.
_EVAL_COMMANDS: Final[dict[str, Callable[[argparse.Namespace, Settings], int]]] = {
    "run": _eval_run,
    "report": _eval_report,
    "compare": _eval_compare,
    "promote": _eval_promote,
}


def _run_eval(args: argparse.Namespace, settings: Settings | None) -> int:
    """Dispatch ``c360 eval <run|report|compare|promote>`` (task 11.11).

    Evaluation needs valid configuration (a provider, thresholds, DB paths), so unlike seeding it
    cannot proceed on defaults alone -- an unloadable configuration is fatal here. The subcommands
    are individual functions dispatched through :data:`_EVAL_COMMANDS` for a simple lookup.
    """
    if settings is None:
        print("error: evaluation requires valid configuration.", file=sys.stderr)
        return _EXIT_ERROR
    handler = _EVAL_COMMANDS.get(args.eval_command)
    if handler is None:  # pragma: no cover - required=True guards the subcommand
        return _EXIT_ERROR
    return handler(args, settings)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    settings = _load_settings()
    parser = _build_parser(settings)
    args = parser.parse_args(argv)

    if args.command == "seed":
        return _run_seed(args)
    if args.command == "recompute":
        return _run_recompute(args)
    if args.command == "detect-signals":
        return _run_detect_signals(args, settings)
    if args.command == "run-reports":
        return _run_reports(args, settings)
    if args.command == "ingest-knowledge":
        return _run_ingest_knowledge(args, settings)
    if args.command == "eval":
        return _run_eval(args, settings)

    # Unreachable: `required=True` on the subparsers means argparse rejects an unknown command
    # before dispatch. Kept as a guard for a future subcommand added without a branch here.
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
