"""自动推荐与主动召回分别限额，自动推荐不能占用主动召回的预留空间。"""

import json

from miniharness.context.manager import token_count


def select_memories(
    entries: list[str], recalled_ids: list[str], budget: int, auto_share: float
) -> list[str]:
    automatic, recalled, seen = [], [], set()
    active_ids = set(recalled_ids)
    for value in entries:
        try:
            parsed = json.loads(value)
            identifier = str(parsed.get("id", "")) if isinstance(parsed, dict) else ""
        except (TypeError, ValueError):
            identifier = ""
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        (recalled if identifier in active_ids else automatic).append(value)
    auto_budget = int(max(0, budget) * min(max(auto_share, 0), 1))
    selected = []
    for group, limit in ((recalled, max(0, budget) - auto_budget), (automatic, auto_budget)):
        kept = []
        for value in group:
            if token_count([*kept, value]) <= limit:
                kept.append(value)
        selected.extend(kept)
    while selected and token_count(selected) > budget:
        selected.pop()
    return selected
