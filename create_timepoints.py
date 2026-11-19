#!/usr/bin/env python3
"""Kick off CreateTimepointFromImportEventId for each imported row of a CSV.

For every row whose ``imp_ok`` column is ``1``, run::

    start-background-process CreateTimepointFromImportEventId \\
        --invoking-user USER --activity_id ID --import_event_id ID \\
        --comment "" --notify USER --include_all_files 1

using the row's ``activity_id`` and ``import_event_id``. Rows with ``imp_ok``
not equal to ``1`` are ignored. Rows that already carry a ``timepoint_id`` are
skipped with a warning so a re-run does not create duplicate timepoints.

The CSV is never modified. A ``.tsv`` file is read as tab-separated.
"""

import argparse
import csv
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("create_timepoints")

DEFAULT_USER = "mwruthe"
START_PROCESS = "/home/posda/posdatools/Posda/bin/start-background-process"
REQUIRED_COLUMNS = ("activity_id", "import_event_id", "imp_ok", "timepoint_id")


class CreateTimepointError(RuntimeError):
    """Raised when reading the CSV or starting a background process fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_file", type=Path, help="CSV/TSV of imports")
    parser.add_argument("--user", default=DEFAULT_USER, help="invoking/notify user")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the commands that would run without starting any jobs",
    )
    return parser.parse_args()


def read_rows(csv_file: Path) -> list[dict[str, str]]:
    delimiter = "\t" if csv_file.suffix.lower() == ".tsv" else ","
    with csv_file.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise CreateTimepointError(f"{csv_file} is empty")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise CreateTimepointError(f"{csv_file} is missing columns: {missing}")
        return list(reader)


def parse_id(row: dict[str, str], column: str) -> int:
    raw = row[column].strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise CreateTimepointError(
            f"bad {column} {raw!r} for {row.get('activity_name', '?')!r}"
        ) from exc


def build_timepoint_command(
    activity_id: int, import_event_id: int, user: str
) -> list[str]:
    return [
        START_PROCESS,
        "CreateTimepointFromImportEventId",
        "--invoking-user",
        user,
        "--activity_id",
        str(activity_id),
        "--import_event_id",
        str(import_event_id),
        "--comment",
        "",
        "--notify",
        user,
        "--include_all_files",
        "1",
    ]


def start_process(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise CreateTimepointError(f"could not run {START_PROCESS}: {exc}") from exc
    if result.stdout.strip():
        log.info("start-background-process: %s", result.stdout.strip())
    if result.returncode != 0:
        raise CreateTimepointError(
            f"{START_PROCESS} exited {result.returncode}: {result.stderr.strip()}"
        )


def should_create_timepoint(row: dict[str, str]) -> bool:
    name = row.get("activity_name", "?")
    if row["imp_ok"].strip() != "1":
        log.debug("skip %r: imp_ok is %r", name, row["imp_ok"])
        return False
    if row["timepoint_id"].strip():
        log.warning("skip %r: already has timepoint_id %s", name, row["timepoint_id"])
        return False
    return True


def process_row(row: dict[str, str], user: str, dry_run: bool) -> None:
    if not should_create_timepoint(row):
        return
    activity_id = parse_id(row, "activity_id")
    import_event_id = parse_id(row, "import_event_id")
    command = build_timepoint_command(activity_id, import_event_id, user)
    # Quote the empty --comment value so the logged line is copy-pasteable.
    printable = " ".join(arg if arg else '""' for arg in command)
    if dry_run:
        log.info("[dry-run] would run: %s", printable)
        return
    log.info("running: %s", printable)
    start_process(command)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    if not args.dry_run and not Path(START_PROCESS).exists():
        log.error("%s not found", START_PROCESS)
        return 1

    for row in read_rows(args.csv_file):
        process_row(row, args.user, args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CreateTimepointError as exc:
        log.error("%s", exc)
        sys.exit(1)
