import pytest

from glpi_mcp.rsql import build_filter
from glpi_mcp.rsql import combine
from glpi_mcp.rsql import quote


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (None, None),
        ({}, None),
        ({"name": "pc01"}, "name==pc01"),
        ({"status.id": [1, 2]}, "status.id=in=(1,2)"),
        ({"status.id__ne": [5, 6]}, "status.id=out=(5,6)"),
        ({"status.id__out": [5]}, "status.id=out=(5)"),
        ({"name__ilike": "*srv*"}, "name=ilike=*srv*"),
        ({"date__ge": "2026-01-31"}, "date=ge=2026-01-31"),
        ({"is_deleted": False}, "is_deleted==0"),
        ({"location": None}, "location=isnull="),
        ({"location__ne": None}, "location=isnotnull="),
        ({"comment__empty": True}, "comment=empty="),
        ({"a": 1, "b__lt": 2.5}, "a==1;b=lt=2.5"),
    ],
)
def test_build_filter(filters, expected):
    assert build_filter(filters) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("two words", '"two words"'),
        ("a;b", '"a;b"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("", '""'),
        (True, "1"),
        (7, "7"),
    ],
)
def test_quote(value, expected):
    assert quote(value) == expected


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"name__contains": "x"}, "Unknown filter operator"),
        ({"name;x": "y"}, "Invalid filter field"),
        ({"id__in": []}, "at least one value"),
        ({"id__lt": [1, 2]}, "one value"),
        ({"date__lt": None}, "needs a value"),
    ],
)
def test_build_filter_rejects(filters, message):
    with pytest.raises(ValueError, match=message):
        build_filter(filters)


def test_combine_keeps_or_groups():
    assert combine(["a==1", None, "b==2,c==3", " "]) == "a==1;(b==2,c==3)"
    assert combine(["b==2,c==3"]) == "b==2,c==3"
    assert combine([None, ""]) is None
    # Commas inside =in=(...) or a quoted value are not OR.
    assert combine(["id=in=(1,2)", 'name=="a,b"']) == 'id=in=(1,2);name=="a,b"'
