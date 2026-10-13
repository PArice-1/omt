from typing import Any


def count_failures(records: list[dict[str, str | bool]]) -> dict[str, int]:
    if len(records) == 0:
        return {}

    count: dict[str, int] = {}
    for record in records:
        if record["ok"] is False:
            if record["tool"] not in count:
                count[record["tool"]] = 1
                continue
            count[record["tool"]] += 1

    return count



