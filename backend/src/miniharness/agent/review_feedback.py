"""Keep failure evidence across model replies, tool calls and formal retests."""

from miniharness.verification.acceptance import INDEPENDENT_CHECK
from miniharness.verification.contracts import independent_aliases


def pending_validation(previous: dict, checked: dict[str, list[str]]) -> dict[str, list[str]]:
    # An empty list is a successful check. An absent key means it was not checked.
    pending = dict(previous.get("issues", {}))
    legacy = pending.pop("previous", [])
    if "issues" not in previous and previous.get("error"):
        legacy = previous["error"].splitlines()
    for message in legacy:
        step = "previous"
        if " — " in message:
            step = "fields"
        elif "模型回复为空" in message or "finish_reason=length" in message:
            step = "response"
        pending[step] = list(dict.fromkeys([*pending.get(step, []), message]))
    for step, messages in checked.items():
        if messages:
            pending[step] = list(dict.fromkeys(messages))
        else:
            pending.pop(step, None)
    return pending


def validation_text(issues: dict[str, list[str]]) -> str:
    return "\n".join(dict.fromkeys(message for messages in issues.values() for message in messages))


def verification_issues(previous: list[dict], gate: dict) -> list[dict]:
    """A read, a draft decision or a new exception cannot prove an earlier test passed."""
    revision = gate.get("task_revision")
    scope = (gate.get("scope") or {}).get("key")
    pending = {
        (issue["scope"], issue["check_id"], issue["case_id"]): issue
        for issue in previous
        if issue["task_revision"] == revision
    }
    checks = gate.get("checks", [])
    aliases = independent_aliases(checks)
    for check in checks:
        identifier = check["id"]
        passed = set(check.get("passed_case_ids", []))
        for key in list(pending):
            if key[:2] == (scope, identifier) and (check["status"] == "passed" or key[2] in passed):
                del pending[key]
        if check["status"] == "passed" or identifier in aliases:
            continue
        failures = [
            (item.get("id", ""), item.get("message") or check.get("error") or "用例未通过")
            for item in check.get("case_failures", [])
        ]
        collected = check.get("collection_failures", [])
        for index, error in enumerate(check.get("collection_errors", [])):
            case = collected[index].get("id", "") if index < len(collected) else ""
            failures.append((case, error))
        for item in collected[len(check.get("collection_errors", [])) :]:
            failures.append((item.get("id", ""), check.get("error") or "收集用例失败"))
        if not failures:
            failures = [("", check.get("error") or check["status"])]
        for case, message in failures:
            key = (scope, identifier, case)
            messages = pending.get(key, {}).get("messages", [])
            pending[key] = {
                "task_revision": revision,
                "scope": scope,
                "check_id": identifier,
                "case_id": case,
                "also_reported_as": sorted(aliases) if identifier == INDEPENDENT_CHECK else [],
                "messages": list(dict.fromkeys([*messages, message])),
                "verification_id": gate.get("id"),
                "snapshot": gate.get("snapshot"),
            }
    return list(pending.values())
