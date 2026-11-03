#!/usr/bin/env python3
"""Create an activity and kick off ImportInPlaceFromPublic for each row of a CSV.

For every row that does not yet have an ``activity_id``:

1. Insert a row into ``activity`` (via ``psql``, using the PG* env vars) and
   capture the returned ``activity_id``.
2. Write that id back into the CSV so a re-run skips the row.
3. Run ``./start-background-process ImportInPlaceFromPublic ...`` with the
   row's collection, site and visibility.
"""

import argparse
import csv
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("import_batch")

DEFAULT_USER = "mwruthe"
# Keep the "./" so subprocess runs the local script instead of searching PATH.
# START_PROCESS = "./start-background-process"
START_PROCESS = "/home/posda/posdatools/Posda/bin/start-background-process"
REQUIRED_COLUMNS = ("activity_name", "collection", "site", "visibility", "activity_id")


class ImportBatchError(RuntimeError):
    """Raised when a database or background-process step fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_file", type=Path, help="CSV of imports to run")
    parser.add_argument("--user", default=DEFAULT_USER, help="invoking/notify user")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen without touching the DB or starting jobs",
    )
    return parser.parse_args()


def read_rows(csv_file: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_file.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ImportBatchError(f"{csv_file} is empty")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ImportBatchError(f"{csv_file} is missing columns: {missing}")
        return list(reader.fieldnames), list(reader)


def write_rows(
    csv_file: Path, fieldnames: list[str], rows: list[dict[str, str]]
) -> None:
    """Rewrite the CSV atomically so a crash mid-run never leaves it truncated."""
    fd, tmp_name = tempfile.mkstemp(dir=csv_file.parent, suffix=".tmp")
    with os.fdopen(fd, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_name, csv_file)


def create_activity(activity_name: str, user: str) -> int:
    """Insert an activity row and return its id.

    Values are passed as psql variables and interpolated with :'var' so they are
    quoted server-side rather than spliced into the SQL string.
    """
    sql = (
        "insert into activity (brief_description, when_created, who_created) "
        "values (:'activity_name', now(), :'user') returning activity_id;"
    )
    command = [
        "psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--quiet",
        "--set=ON_ERROR_STOP=1",
        f"--set=activity_name={activity_name}",
        f"--set=user={user}",
    ]
    # SQL goes in on stdin: psql only expands :'var' there, not in --command.
    result = subprocess.run(command, input=sql, capture_output=True, text=True)
    if result.returncode != 0:
        raise ImportBatchError(
            f"psql failed for {activity_name!r}: {result.stderr.strip()}"
        )
    output = result.stdout.strip()
    try:
        return int(output)
    except ValueError as exc:
        raise ImportBatchError(f"unexpected psql output {output!r}") from exc


def build_import_command(row: dict[str, str], activity_id: int, user: str) -> list[str]:
    return [
        START_PROCESS,
        "ImportInPlaceFromPublic",
        "--invoking-user",
        user,
        "--notify",
        user,
        "--activity_id",
        str(activity_id),
        "--collection_name",
        row["collection"],
        "--site_name",
        row["site"],
        "--visibility",
        row["visibility"],
        "--queue",
        "special",
    ]


def start_import(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise ImportBatchError(f"could not run {START_PROCESS}: {exc}") from exc
    if result.stdout.strip():
        log.info("start-background-process: %s", result.stdout.strip())
    if result.returncode != 0:
        raise ImportBatchError(
            f"{START_PROCESS} exited {result.returncode}: {result.stderr.strip()}"
        )


def process_row(
    row: dict[str, str],
    user: str,
    dry_run: bool,
    persist: Callable[[], None],
) -> None:
    name = row["activity_name"]
    if row["activity_id"].strip():
        log.info("skip %r: already has activity_id %s", name, row["activity_id"])
        return

    if dry_run:
        log.info("[dry-run] would create activity %r", name)
        log.info(
            "[dry-run] would run: %s", " ".join(build_import_command(row, 0, user))
        )
        return

    activity_id = create_activity(name, user)
    log.info("created activity %s for %r", activity_id, name)
    row["activity_id"] = str(activity_id)
    persist()

    command = build_import_command(row, activity_id, user)
    log.info("running: %s", " ".join(command))
    start_import(command)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    if not args.dry_run and not Path(START_PROCESS).exists():
        log.error("%s not found in %s", START_PROCESS, Path.cwd())
        return 1

    fieldnames, rows = read_rows(args.csv_file)

    def persist() -> None:
        write_rows(args.csv_file, fieldnames, rows)

    for row in rows:
        process_row(row, args.user, args.dry_run, persist)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImportBatchError as exc:
        log.error("%s", exc)
        sys.exit(1)
