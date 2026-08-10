from decimal import Decimal

from app.models import decode_measure, parse_measure_group
from tests.conftest import measure_group


def test_decode_measure_honors_unit() -> None:
    assert decode_measure(1460, -1) == Decimal("146.0")
    assert decode_measure(9, 1) == Decimal(90)


def test_parse_bp_group_and_stable_dedupe_key() -> None:
    parsed = parse_measure_group(measure_group(123, 1_786_000_000, 146, 90, 71, unit=-1), "42")

    assert parsed.complete
    assert parsed.systolic == Decimal("146.0")
    assert parsed.diastolic == Decimal("90.0")
    assert parsed.pulse == Decimal("71.0")
    assert parsed.dedupe_key == "42:grpid:123"
    assert parsed.device_id == "wpm05-device"
    assert parsed.model_id == 45
