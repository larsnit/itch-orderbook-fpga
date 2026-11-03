"""Compare a DUT's output stream with the model's, item by item.

Stops at the first mismatch and shows it with the items around it. Items that are dicts
(field values from structs.unpack) are compared on the expected item's fields only, so
fields the model doesn't know (t_arrival) are ignored.
"""

from typing import Callable

CONTEXT = 3  # items shown before and after a mismatch


def compare(expected: list, actual: list, name: str, describe: Callable[[object], str] = repr) -> None:
    for index, (want, got) in enumerate(zip(expected, actual)):
        if not matches(want, got):
            raise AssertionError(report(expected, actual, index, name, describe))
    if len(expected) != len(actual):
        index = min(len(expected), len(actual))
        raise AssertionError(
            f"{name}: expected {len(expected)} items, got {len(actual)}\n"
            + report(expected, actual, index, name, describe)
        )


def matches(want: object, got: object) -> bool:
    if isinstance(want, dict) and isinstance(got, dict):
        return all(got.get(field) == value for field, value in want.items())
    return want == got


def differences(want: dict, got: dict) -> str:
    return ", ".join(f"{field}: expected {value}, got {got.get(field)}" for field, value in want.items() if got.get(field) != value)


def report(expected: list, actual: list, index: int, name: str, describe: Callable[[object], str]) -> str:
    lines = [f"{name}: first mismatch at item {index}"]
    if index < len(expected) and index < len(actual) and isinstance(expected[index], dict):
        lines.append("  " + differences(expected[index], actual[index]))
    for i in range(max(0, index - CONTEXT), index + CONTEXT + 1):
        marker = ">>" if i == index else "  "
        want = describe(expected[i]) if i < len(expected) else "(none)"
        got = describe(actual[i]) if i < len(actual) else "(none)"
        if i < len(expected) or i < len(actual):
            lines.append(f"{marker} {i:6}  expected {want}")
            lines.append(f"{marker} {'':6}  got      {got}")
    return "\n".join(lines)
