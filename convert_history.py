#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

CANONICAL_HEADER = b"Date,Heart rate,Systole,Diastole\r\n"
SOURCE_COLUMNS = ("date", "systolic", "diastolic", "pulse")


@dataclass(frozen=True, slots=True)
class HistoryRow:
    date: str
    systolic: Decimal
    diastolic: Decimal
    pulse: Decimal

    @property
    def dedupe_key(self) -> tuple[str, str, str, str]:
        return (
            self.date,
            format_decimal(self.systolic),
            format_decimal(self.diastolic),
            format_decimal(self.pulse),
        )

    def withings_fields(self) -> list[str]:
        return [
            self.date,
            format_decimal(self.pulse),
            format_decimal(self.systolic),
            format_decimal(self.diastolic),
        ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert normalized BP history into Withings CSV batches. "
            "The source must have date,systolic,diastolic,pulse columns."
        )
    )
    parser.add_argument("input", type=Path, help="normalized source CSV")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--template", type=Path, help="official downloaded Withings template")
    parser.add_argument(
        "--existing",
        type=Path,
        action="append",
        default=[],
        help="existing Withings-format CSV; may be repeated",
    )
    parser.add_argument("--timezone", default="UTC", help="timezone for naive source dates")
    parser.add_argument("--input-delimiter", default=",")
    parser.add_argument("--batch-size", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.batch_size <= 300:
        raise SystemExit("--batch-size must be between 1 and 300")
    if len(args.input_delimiter) != 1:
        raise SystemExit("--input-delimiter must be one character")

    timezone = ZoneInfo(args.timezone)
    header, newline = read_template_header(args.template)
    existing = load_existing(args.existing)
    rows, issues = load_source(args.input, args.input_delimiter, timezone, existing)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for index, start in enumerate(range(0, len(rows), args.batch_size), start=1):
        output = args.output_dir / f"withings-bp-{index:03d}.csv"
        write_batch(output, header, newline, rows[start : start + args.batch_size])
        files.append(output.name)

    report = {
        "source": str(args.input),
        "accepted": len(rows),
        "skipped": len(issues),
        "existing_keys": len(existing),
        "files": files,
        "issues": issues,
    }
    report_path = args.output_dir / "conversion-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: report[key] for key in ("accepted", "skipped", "files")}))
    return 0


def read_template_header(template: Path | None) -> tuple[bytes, str]:
    if template is None:
        return CANONICAL_HEADER, "\r\n"
    content = template.read_bytes()
    if not content:
        raise ValueError("template is empty")
    first_line = content.splitlines(keepends=True)[0]
    if first_line.endswith(b"\r\n"):
        newline = "\r\n"
    elif first_line.endswith(b"\n"):
        newline = "\n"
    else:
        newline = "\r\n"
        first_line += b"\r\n"
    decoded = first_line.decode("utf-8-sig").rstrip("\r\n")
    fields = next(csv.reader([decoded], delimiter=","))
    if len(fields) != 4:
        raise ValueError("Withings BP template must contain exactly four comma-separated columns")
    return first_line, newline


def load_existing(paths: list[Path]) -> set[tuple[str, str, str, str]]:
    keys: set[tuple[str, str, str, str]] = set()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, delimiter=",")
            next(reader, None)
            for fields in reader:
                if len(fields) < 4:
                    continue
                try:
                    date = normalize_withings_date(fields[0])
                    pulse = parse_number(fields[1], "pulse")
                    systolic = parse_number(fields[2], "systolic")
                    diastolic = parse_number(fields[3], "diastolic")
                except ValueError:
                    continue
                keys.add(
                    (
                        date,
                        format_decimal(systolic),
                        format_decimal(diastolic),
                        format_decimal(pulse),
                    )
                )
    return keys


def load_source(
    path: Path,
    delimiter: str,
    timezone: ZoneInfo,
    existing: set[tuple[str, str, str, str]],
) -> tuple[list[HistoryRow], list[dict[str, object]]]:
    rows: list[HistoryRow] = []
    issues: list[dict[str, object]] = []
    seen = set(existing)
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter=delimiter)
        normalized_names = {str(name).strip().lower() for name in (reader.fieldnames or [])}
        missing = set(SOURCE_COLUMNS) - normalized_names
        if missing:
            raise ValueError(f"source is missing columns: {', '.join(sorted(missing))}")
        name_map = {str(name).strip().lower(): str(name) for name in reader.fieldnames or []}
        for line_number, raw in enumerate(reader, start=2):
            try:
                row = HistoryRow(
                    date=parse_source_date(raw[name_map["date"]], timezone),
                    systolic=parse_number(raw[name_map["systolic"]], "systolic"),
                    diastolic=parse_number(raw[name_map["diastolic"]], "diastolic"),
                    pulse=parse_number(raw[name_map["pulse"]], "pulse"),
                )
                validate_ranges(row)
            except (KeyError, TypeError, ValueError) as error:
                issues.append({"line": line_number, "reason": str(error)})
                continue
            if row.dedupe_key in seen:
                issues.append({"line": line_number, "reason": "duplicate or already existing"})
                continue
            seen.add(row.dedupe_key)
            rows.append(row)
    rows.sort(key=lambda row: row.date)
    return rows, issues


def parse_source_date(value: object, timezone: ZoneInfo) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("date is empty")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError("date must be ISO 8601 or yyyy-mm-dd hh:mm:ss") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    else:
        parsed = parsed.astimezone(timezone)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def normalize_withings_date(value: object) -> str:
    text = str(value).strip()
    try:
        return (
            datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=UTC)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except ValueError as error:
        raise ValueError("invalid Withings date") from error


def parse_number(value: object, name: str) -> Decimal:
    text = str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"{name} is not a number") from error
    if not number.is_finite():
        raise ValueError(f"{name} is not finite")
    return number


def validate_ranges(row: HistoryRow) -> None:
    if not Decimal(40) <= row.systolic <= Decimal(300):
        raise ValueError("systolic is outside 40..300")
    if not Decimal(20) <= row.diastolic <= Decimal(200):
        raise ValueError("diastolic is outside 20..200")
    if not Decimal(20) <= row.pulse <= Decimal(250):
        raise ValueError("pulse is outside 20..250")
    if row.systolic <= row.diastolic:
        raise ValueError("systolic must be greater than diastolic")


def write_batch(path: Path, header: bytes, newline: str, rows: list[HistoryRow]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=",", lineterminator=newline)
    writer.writerows(row.withings_fields() for row in rows)
    path.write_bytes(header + buffer.getvalue().encode("utf-8"))


def format_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
