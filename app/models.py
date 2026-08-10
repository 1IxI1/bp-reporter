from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

BP_DIASTOLIC = 9
BP_SYSTOLIC = 10
BP_PULSE = 11


@dataclass(frozen=True, slots=True)
class ParsedMeasurement:
    dedupe_key: str
    userid: str
    grpid: str | None
    measured_at: int
    created_at_withings: int | None
    modified_at_withings: int | None
    systolic: Decimal | None
    diastolic: Decimal | None
    pulse: Decimal | None
    device_id: str | None
    model_id: int | None
    model: str | None
    raw_json: str

    @property
    def complete(self) -> bool:
        return self.systolic is not None and self.diastolic is not None and self.pulse is not None


@dataclass(frozen=True, slots=True)
class FetchResult:
    groups: list[dict[str, Any]]
    updatetime: int | None


def decode_measure(value: float | str, unit: int | str) -> Decimal:
    return Decimal(str(value)) * (Decimal(10) ** int(unit))


def parse_measure_group(group: dict[str, Any], userid: str) -> ParsedMeasurement:
    decoded: dict[int, Decimal] = {}
    for measure in group.get("measures") or []:
        try:
            measure_type = int(measure["type"])
            if measure_type in (BP_DIASTOLIC, BP_SYSTOLIC, BP_PULSE):
                decoded[measure_type] = decode_measure(measure["value"], measure["unit"])
        except (KeyError, TypeError, ValueError):
            continue

    grpid_value = group.get("grpid")
    grpid = str(grpid_value) if grpid_value is not None else None
    measured_at = int(group["date"])
    raw_json = json.dumps(group, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if grpid:
        dedupe_key = f"{userid}:grpid:{grpid}"
    else:
        fallback = "|".join(
            (
                userid,
                str(measured_at),
                str(decoded.get(BP_SYSTOLIC, "")),
                str(decoded.get(BP_DIASTOLIC, "")),
                str(decoded.get(BP_PULSE, "")),
            )
        )
        dedupe_key = f"{userid}:fallback:{hashlib.sha256(fallback.encode()).hexdigest()}"

    device_id = group.get("deviceid") or group.get("hash_deviceid")
    return ParsedMeasurement(
        dedupe_key=dedupe_key,
        userid=userid,
        grpid=grpid,
        measured_at=measured_at,
        created_at_withings=_optional_int(group.get("created")),
        modified_at_withings=_optional_int(group.get("modified")),
        systolic=decoded.get(BP_SYSTOLIC),
        diastolic=decoded.get(BP_DIASTOLIC),
        pulse=decoded.get(BP_PULSE),
        device_id=str(device_id) if device_id is not None else None,
        model_id=_optional_int(group.get("model_id")),
        model=str(group["model"]) if group.get("model") is not None else None,
        raw_json=raw_json,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
