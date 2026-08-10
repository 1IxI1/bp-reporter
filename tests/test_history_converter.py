from __future__ import annotations

import csv
import json
from pathlib import Path

from convert_history import main


def test_converter_preserves_template_header_splits_and_deduplicates(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    with source.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["date", "systolic", "diastolic", "pulse"])
        for index in range(302):
            writer.writerow([f"2025-04-{index % 28 + 1:02d} 10:{index % 60:02d}:00", 140, 90, 70])
        writer.writerow(["bad date", 140, 90, 70])

    template = tmp_path / "template.csv"
    header = "\ufeffДата,Пульс,Систола,Диастола\n".encode()
    template.write_bytes(header)
    existing = tmp_path / "existing.csv"
    existing.write_text(
        "Date,Heart rate,Systole,Diastole\n2025-04-01 10:00:00,70,140,90\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert (
        main(
            [
                str(source),
                str(output_dir),
                "--template",
                str(template),
                "--existing",
                str(existing),
                "--timezone",
                "UTC",
            ]
        )
        == 0
    )

    outputs = sorted(output_dir.glob("withings-bp-*.csv"))
    assert len(outputs) == 2
    assert outputs[0].read_bytes().startswith(header)
    with outputs[0].open("r", encoding="utf-8-sig", newline="") as file:
        assert sum(1 for _ in file) == 301  # header plus 300 measurements
    report = json.loads((output_dir / "conversion-report.json").read_text())
    assert report["accepted"] == 301
    assert report["skipped"] == 2


def test_telegram_jsonl_uses_valid_text_date_and_falls_back(tmp_path: Path) -> None:
    source = tmp_path / "bp.jsonl"
    records = [
        {
            "_": "Message",
            "id": 16,
            "date": "2026-03-25T18:28:53+00:00",
            "message": "21 марта 09:51\n\n#1: 138/80\n#2: 135/79\n\n#: 136/80",
        },
        {
            "_": "Message",
            "id": 69,
            "date": "2026-04-11T20:12:10+00:00",
            "message": "11 ареля 23:12\n152 на 66\n143 на 60",
        },
        {
            "_": "Message",
            "id": 151,
            "date": "2026-07-11T22:18:40+00:00",
            "message": "12 июня 01:15\n142 на 68\n139 на 64\n160 на 64\n158 на 64",
        },
    ]
    source.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    assert main([str(source), str(output_dir), "--timezone", "Europe/Minsk"]) == 0

    with (output_dir / "withings-bp-001.csv").open("r", encoding="utf-8-sig", newline="") as output:
        rows = list(csv.reader(output))
    assert rows[1] == ["2026-03-21 09:51:00", "", "138", "80"]
    assert rows[2] == ["2026-03-21 09:51:01", "", "135", "79"]
    assert rows[3][0] == "2026-04-11 23:12:10"
    assert rows[5][0] == "2026-07-12 01:18:40"
    assert len(rows) == 9  # header plus eight raw measurements

    report = json.loads((output_dir / "conversion-report.json").read_text())
    assert report["accepted"] == 8
    assert report["messages_with_measurements"] == 3
    assert [item["message_id"] for item in report["date_fallbacks"]] == [69, 151]
