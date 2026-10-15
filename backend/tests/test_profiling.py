"""Table-driven type inference + profiling."""
from __future__ import annotations

import pandas as pd
import pytest

import profiling
from profiling import BOOL, CAT, DATE, NUM, TEXT

CASES = [
    ("plain ints", [1, 2, 3, 4, 5, 6, 7, 12], NUM),
    ("floats", [1.5, 2.25, 3.125, 9.5, 11.75, 4.0], NUM),
    ("currency strings", ["$1,240", "$980", "$12.10", "$4,000", "$50"], NUM),
    ("percent strings", ["12%", "45%", "7%", "88%", "3%"], NUM),
    ("iso dates", ["2024-01-01", "2024-02-14", "2024-03-05", "2024-04-09"], DATE),
    ("slash dates", ["2024/01/01", "2024/02/14", "2024/03/05", "2024/04/09"], DATE),
    ("booleans", [True, False, True, False], BOOL),
    ("yes/no", ["yes", "no", "yes", "no", "yes"], BOOL),
    ("0/1 ints", [0, 1, 1, 0, 1], BOOL),
    ("low-cardinality strings", ["north", "south", "east", "west", "north", "south"], CAT),
    ("long free text", ["the quick brown fox jumped over the lazy dog again and again %d" % i
                        for i in range(30)], TEXT),
]


@pytest.mark.parametrize("label,values,expected", CASES, ids=[c[0] for c in CASES])
def test_infer_type(label, values, expected):
    assert profiling.infer_type(pd.Series(values)) == expected, label


def test_coerce_returns_types_for_every_column():
    df = pd.DataFrame({"when": ["2024-01-01", "2024-01-02"], "amt": ["$5", "$7"],
                       "who": ["a", "b"]})
    out, types = profiling.coerce(df)
    assert set(types) == set(out.columns)
    assert types["when"] == DATE and types["amt"] == NUM and types["who"] == CAT


def test_profile_sheet_shape():
    df = pd.DataFrame({"cat": list("aabbcc"), "num": [1, 2, 3, 4, 5, 6]})
    types = {"cat": CAT, "num": NUM}
    prof = profiling.profile_sheet("S", df, types)
    assert prof["name"] == "S" and prof["rows"] == 6
    by = {c["name"]: c for c in prof["columns"]}
    assert by["num"]["min"] == 1 and by["num"]["max"] == 6
    assert by["cat"]["unique"] == 3 and by["cat"]["top"][0]["count"] == 2


def test_rows_payload_pagination():
    df = pd.DataFrame({"n": range(100)})
    payload = profiling.rows_payload(df, offset=10, limit=5)
    assert payload["total"] == 100 and payload["offset"] == 10
    assert [r[0] for r in payload["rows"]] == [10, 11, 12, 13, 14]


def test_read_any_rejects_unknown_extension():
    with pytest.raises(profiling.ParseError):
        profiling.read_any("thing.bin", b"abc")
