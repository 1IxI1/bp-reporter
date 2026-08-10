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
