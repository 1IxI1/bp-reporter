#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

CANONICAL_HEADER = b"Date,Heart rate,Systole,Diastole\r\n"
SOURCE_COLUMNS = ("date", "systolic", "diastolic", "pulse")
RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июн": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
TEXT_DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([а-яё]+)\s+(\d{1,2}):(\d{2})(?:\s|$)", re.IGNORECASE)
NUMBERED_BP_RE = re.compile(r"^\s*#(\d+)\s*:?\s*(\d{2,3})\s*/\s*(\d{2,3})(?:\D|$)")
PLAIN_BP_RE = re.compile(r"^\s*(?:правая\s+)?(\d{2,3})\s+на\s+(\d{2,3})(?:\D|$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class HistoryRow:
    date: str
    systolic: Decimal
    diastolic: Decimal
    pulse: Decimal | None

    @property
    def dedupe_key(self) -> tuple[str, str, str, str]:
        return (
            self.date,
            format_decimal(self.systolic),
            format_decimal(self.diastolic),
            format_optional_decimal(self.pulse),
        )

    def withings_fields(self) -> list[str]:
        return [
            self.date,
            format_optional_decimal(self.pulse),
            format_decimal(self.systolic),
            format_decimal(self.diastolic),
        ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert normalized CSV or a Telegram JSONL channel export into "
            "Withings blood-pressure CSV batches."
        )
    )
    parser.add_argument("input", type=Path, help="normalized CSV or Telegram JSONL")
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
    parser.add_argument(
        "--input-format",
        choices=("auto", "normalized", "telegram-jsonl"),
        default="auto",
    )
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
    input_format = args.input_format
    if input_format == "auto":
        input_format = "telegram-jsonl" if args.input.suffix.lower() == ".jsonl" else "normalized"
    if input_format == "telegram-jsonl":
        rows, issues, details = load_telegram_jsonl(args.input, timezone, existing)
    else:
        rows, issues = load_source(args.input, args.input_delimiter, timezone, existing)
        details = {"messages_with_measurements": None, "date_fallbacks": []}

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
        **details,
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
                    pulse = parse_optional_number(fields[1], "pulse")
                    systolic = parse_number(fields[2], "systolic")
                    diastolic = parse_number(fields[3], "diastolic")
                except ValueError:
                    continue
                keys.add(
                    (
                        date,
                        format_decimal(systolic),
                        format_decimal(diastolic),
                        format_optional_decimal(pulse),
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


def load_telegram_jsonl(
    path: Path,
    timezone: ZoneInfo,
    existing: set[tuple[str, str, str, str]],
) -> tuple[list[HistoryRow], list[dict[str, object]], dict[str, object]]:
    rows: list[HistoryRow] = []
    issues: list[dict[str, object]] = []
    date_fallbacks: list[dict[str, object]] = []
    seen = set(existing)
    messages_with_measurements = 0

    with path.open("r", encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                issues.append({"line": line_number, "reason": "invalid JSON"})
                continue
            if not isinstance(record, dict) or record.get("_") != "Message":
                continue
            text = record.get("message")
            sent_at = record.get("date")
            message_id = record.get("id")
            if not isinstance(text, str) or not isinstance(sent_at, str):
                issues.append(
                    {
                        "line": line_number,
                        "message_id": message_id,
                        "reason": "missing text or date",
                    }
                )
                continue
            measurements = extract_message_measurements(text)
            if not measurements:
                # Non-medical service and comment messages are expected in a channel export.
                continue
            try:
                fallback = datetime.fromisoformat(sent_at)
                if fallback.tzinfo is None:
                    raise ValueError("Telegram date has no timezone")
                fallback = fallback.astimezone(timezone)
            except ValueError as error:
                issues.append(
                    {
                        "line": line_number,
                        "message_id": message_id,
                        "reason": f"invalid Telegram date: {error}",
                    }
                )
                continue

            measured_at, fallback_reason = parse_message_datetime(text, fallback, timezone)
            if fallback_reason:
                date_fallbacks.append(
                    {
                        "line": line_number,
                        "message_id": message_id,
                        "reason": fallback_reason,
                        "used": measured_at.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            messages_with_measurements += 1
            for index, (systolic, diastolic) in enumerate(measurements):
                row = HistoryRow(
                    date=(measured_at + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S"),
                    systolic=Decimal(systolic),
                    diastolic=Decimal(diastolic),
                    pulse=None,
                )
                try:
                    validate_ranges(row)
                except ValueError as error:
                    issues.append(
                        {
                            "line": line_number,
                            "message_id": message_id,
                            "measurement": index + 1,
                            "reason": str(error),
                        }
                    )
                    continue
                if row.dedupe_key in seen:
                    issues.append(
                        {
                            "line": line_number,
                            "message_id": message_id,
                            "measurement": index + 1,
                            "reason": "duplicate or already existing",
                        }
                    )
                    continue
                seen.add(row.dedupe_key)
                rows.append(row)

    rows.sort(key=lambda row: row.date)
    return (
        rows,
        issues,
        {
            "messages_with_measurements": messages_with_measurements,
            "date_fallbacks": date_fallbacks,
        },
    )


def parse_message_datetime(
    text: str, fallback: datetime, timezone: ZoneInfo
) -> tuple[datetime, str | None]:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = TEXT_DATE_RE.match(first_line)
    if not match:
        return fallback, "text date is missing or unrecognized"
    day, month_name, hour, minute = match.groups()
    month = RUSSIAN_MONTHS.get(month_name.lower())
    if month is None:
        return fallback, f"unknown Russian month: {month_name}"
    candidates: list[datetime] = []
    for year in (fallback.year - 1, fallback.year, fallback.year + 1):
        try:
            candidates.append(
                datetime(year, month, int(day), int(hour), int(minute), tzinfo=timezone)
            )
        except ValueError:
            return fallback, "text date is invalid"
    candidate = min(candidates, key=lambda value: abs(value - fallback))
    if abs(candidate - fallback) > timedelta(days=14):
        return fallback, "text date differs from Telegram date by more than 14 days"
    return candidate, None


def extract_message_measurements(text: str) -> list[tuple[int, int]]:
    measurements: list[tuple[int, int]] = []
    for line in text.splitlines():
        numbered = NUMBERED_BP_RE.match(line)
        if numbered:
            measurements.append((int(numbered.group(2)), int(numbered.group(3))))
            continue
        plain = PLAIN_BP_RE.match(line)
        if plain:
            measurements.append((int(plain.group(1)), int(plain.group(2))))
    return measurements


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


def parse_optional_number(value: object, name: str) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    return parse_number(value, name)


def validate_ranges(row: HistoryRow) -> None:
    if not Decimal(40) <= row.systolic <= Decimal(300):
        raise ValueError("systolic is outside 40..300")
    if not Decimal(20) <= row.diastolic <= Decimal(200):
        raise ValueError("diastolic is outside 20..200")
    if row.pulse is not None and not Decimal(20) <= row.pulse <= Decimal(250):
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


def format_optional_decimal(value: Decimal | None) -> str:
    return format_decimal(value) if value is not None else ""


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
