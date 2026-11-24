#!/usr/bin/env python3
import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg2

CSV_PATH = Path("full.csv")
COUNT_FIELDS = ("patients", "studies", "series", "files")

COUNT_QUERY = """
    select count(distinct fp.patient_id) as patient_count,
        count(distinct fst.study_instance_uid) as study_count,
        count(distinct fs.series_instance_uid) as series_count,
        count(distinct fi.file_id) as sop_count
    from file_import fi
    left join file_patient fp  on fp.file_id  = fi.file_id
    left join file_study   fst on fst.file_id = fi.file_id
    left join file_series  fs  on fs.file_id  = fi.file_id
    where fi.import_event_id = %s
"""


@dataclass(frozen=True)
class Counts:
    patients: int
    studies: int
    series: int
    files: int

    def differences(self, other: "Counts") -> list[str]:
        """Describe each field where self (expected) differs from other (actual)."""
        diffs = []
        for field in COUNT_FIELDS:
            expected = getattr(self, field)
            actual = getattr(other, field)
            if expected != actual:
                diffs.append(f"{field} expected={expected} actual={actual}")
        return diffs


def expected_counts_from_row(row: dict[str, str]) -> Counts:
    return Counts(
        patients=int(row["exp_patients"]),
        studies=int(row["exp_studies"]),
        series=int(row["exp_series"]),
        files=int(row["exp_files"]),
    )


def fetch_import_counts(cursor, import_event_id: str) -> Counts:
    cursor.execute(COUNT_QUERY, (import_event_id,))
    patients, studies, series, files = cursor.fetchone()
    return Counts(patients=patients, studies=studies, series=series, files=files)


def record_import_counts(row: dict[str, str], actual: Counts, ok: bool) -> None:
    """Write the actual counts and match flag back into the row's imp_* columns."""
    row["imp_patients"] = str(actual.patients)
    row["imp_studies"] = str(actual.studies)
    row["imp_series"] = str(actual.series)
    row["imp_files"] = str(actual.files)
    row["imp_ok"] = "1" if ok else "0"


def read_rows(csv_file: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_file.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_file} is empty")
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


def describe_row(row: dict[str, str]) -> str:
    return f"seq={row['seq']} activity_id={row['activity_id']} {row['activity_name']}"


def check_row(cursor, row: dict[str, str]) -> str:
    import_event_id = row["import_event_id"].strip()
    if not import_event_id:
        return f"SKIP     {describe_row(row)} (no import_event_id)"

    expected = expected_counts_from_row(row)
    actual = fetch_import_counts(cursor, import_event_id)
    diffs = expected.differences(actual)
    record_import_counts(row, actual, ok=not diffs)
    if not diffs:
        return f"MATCH    {describe_row(row)}"
    return f"MISMATCH {describe_row(row)}: {'; '.join(diffs)}"


def main() -> None:
    conn = psycopg2.connect(database="posda_files")
    cursor = conn.cursor()
    fieldnames, rows = read_rows(CSV_PATH)
    try:
        for row in rows:
            print(check_row(cursor, row), flush=True)
            # Large imports take minutes to count; save after each row so an
            # interrupted run keeps everything checked so far.
            write_rows(CSV_PATH, fieldnames, rows)
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
