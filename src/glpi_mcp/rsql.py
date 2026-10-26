"""
Building RSQL filters for the GLPI High-Level API.

Collection endpoints take a ``filter`` parameter in RSQL: ``field==value``
clauses joined by ``;`` (AND) or ``,`` (OR), with dot paths for nested
properties (``status.id==1``). Tools accept a plain dict and turn it into RSQL
here, so a model never has to get quoting and operator syntax right itself.
"""

import re
from collections.abc import Iterable
from typing import Any

# Suffix after "__" in a filter key -> RSQL operator.
OPERATORS = {
    "eq": "==",
    "ne": "!=",
    "in": "=in=",
    "out": "=out=",
    "lt": "=lt=",
    "le": "=le=",
    "gt": "=gt=",
    "ge": "=ge=",
    "like": "=like=",
    "ilike": "=ilike=",
    "isnull": "=isnull=",
    "isnotnull": "=isnotnull=",
    "empty": "=empty=",
    "notempty": "=notempty=",
}
# Operators that take no value.
UNARY = frozenset({"isnull", "isnotnull", "empty", "notempty"})
# Operators that take a list of values.
LIST_OPERATORS = frozenset({"in", "out"})
# A property path: letters, digits and underscores, dot-separated.
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
# Characters that end an unquoted RSQL value.
RESERVED = re.compile(r"[\s\"'();,=!~<>]")


def quote(value: Any) -> str:
    """Render one value for RSQL, quoting it if it contains reserved characters.

    Booleans become ``1``/``0``, as GLPI stores them; ``*`` in a string is the
    wildcard of ``=like=`` and ``=ilike=``.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    if text and not RESERVED.search(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _clause(key: str, value: Any) -> str:
    field, _, op = key.partition("__")
    op = op or "eq"
    if op not in OPERATORS:
        raise ValueError(
            f"Unknown filter operator {op!r} in {key!r}. Use one of: "
            f"{', '.join(OPERATORS)} (e.g. 'name__ilike')."
        )
    if not FIELD.match(field):
        raise ValueError(
            f"Invalid filter field {field!r}: use a property name or a dot path "
            "such as 'status.id'."
        )
    if op in UNARY:
        return f"{field}{OPERATORS[op]}"
    is_list = isinstance(value, list | tuple | set)
    if op == "eq" and value is None:
        return f"{field}=isnull="
    if op == "ne" and value is None:
        return f"{field}=isnotnull="
    if value is None:
        raise ValueError(
            f"Filter {key!r} needs a value; None is only valid with == and != "
            "(is null / is not null)."
        )
    if is_list and op in ("eq", "ne"):
        op = "in" if op == "eq" else "out"
    if op in LIST_OPERATORS:
        values = list(value) if is_list else [value]
        if not values:
            raise ValueError(f"Filter {key!r} needs at least one value.")
        return f"{field}{OPERATORS[op]}({','.join(quote(v) for v in values)})"
    if is_list:
        raise ValueError(f"Filter {key!r} takes one value, not a list.")
    return f"{field}{OPERATORS[op]}{quote(value)}"


def build_filter(filters: dict[str, Any] | None) -> str | None:
    """Turn ``{"field__op": value}`` pairs into an RSQL filter (AND-ed).

    Examples:
        ``{"status.id": [1, 2]}`` -> ``status.id=in=(1,2)``
        ``{"name__ilike": "*srv*", "is_deleted": False}``
        -> ``name=ilike=*srv*;is_deleted==0``

    Raises:
        ValueError: On an unknown operator, a malformed field, or a value of
            the wrong shape for its operator.
    """
    if not filters:
        return None
    return ";".join(_clause(key, value) for key, value in filters.items())


def has_top_level_or(rsql: str) -> bool:
    """Whether ``rsql`` has a ``,`` (OR) outside parentheses and quotes.

    Commas inside ``=in=(1,2)`` or a quoted value do not count: wrapping such a
    filter in parentheses is harmless but noisy.
    """
    depth = 0
    quote_char = None
    escaped = False
    for char in rsql:
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
        elif char in "\"'":
            quote_char = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            return True
    return False


def combine(parts: Iterable[str | None]) -> str | None:
    """AND together RSQL filters, keeping each one's own OR-groups intact."""
    present = [part.strip() for part in parts if part and part.strip()]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return ";".join(f"({part})" if has_top_level_or(part) else part for part in present)
