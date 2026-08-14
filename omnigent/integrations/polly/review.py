from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, TypeAlias

JsonObject: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]  # model JSON boundary

VERDICTS = ("Sound", "Mostly sound", "Unclear", "Risky", "Needs changes")
PRIORITIES = ("High", "Medium", "Low")
_REQUIRED = {"review_id", "summary", "approach_verdict", "has_blocking_issues", "inline_comments"}
_OPTIONAL = {
    "status_update",
    "resolved_previous_findings",
    "still_open_previous_findings",
    "workspace_status",
}
_COMMENT_KEYS = {"path", "line", "priority", "title", "body"}
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NON_WHITESPACE = r"\S"
_PATH_PATTERN = r"^(?!/|\./|\.\./|a/|b/)(?=.*\S)[^\r\n]+$"
_VALID_PATH = re.compile(_PATH_PATTERN)


class ReviewValidationError(ValueError):
    pass


def review_schema(review_id: str) -> JsonObject:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_REQUIRED),
        "properties": {
            "review_id": {"type": "string", "const": review_id},
            "summary": {"type": "string", "minLength": 1, "pattern": _NON_WHITESPACE},
            "approach_verdict": {"type": "string", "enum": list(VERDICTS)},
            "has_blocking_issues": {"type": "boolean"},
            "status_update": {"type": "string"},
            "resolved_previous_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "still_open_previous_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "workspace_status": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "checked_out_sha": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
            "inline_comments": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(_COMMENT_KEYS),
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": _PATH_PATTERN,
                        },
                        "line": {"type": "integer", "minimum": 1},
                        "priority": {"type": "string", "enum": list(PRIORITIES)},
                        "title": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": _NON_WHITESPACE,
                        },
                        "body": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": _NON_WHITESPACE,
                        },
                    },
                },
            },
        },
    }


def extract_review(text: str, review_id: str) -> JsonObject:
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReviewValidationError("review must be one JSON object") from exc
    if not isinstance(raw, dict):
        raise ReviewValidationError("review must be a JSON object")
    unknown = set(raw) - _REQUIRED - _OPTIONAL
    missing = _REQUIRED - set(raw)
    if unknown:
        raise ReviewValidationError(f"unexpected review fields: {sorted(unknown)}")
    if missing:
        raise ReviewValidationError(f"missing review fields: {sorted(missing)}")
    if raw["review_id"] != review_id:
        raise ReviewValidationError("Review-ID does not match")
    if not isinstance(raw["summary"], str) or not raw["summary"].strip():
        raise ReviewValidationError("summary must be non-empty")
    if raw["approach_verdict"] not in VERDICTS:
        raise ReviewValidationError("invalid approach verdict")
    if not isinstance(raw["has_blocking_issues"], bool):
        raise ReviewValidationError("has_blocking_issues must be boolean")
    if not isinstance(raw["inline_comments"], list) or len(raw["inline_comments"]) > 5:
        raise ReviewValidationError("inline_comments must contain at most five entries")
    for comment in raw["inline_comments"]:
        _validate_comment(comment)
    for field in ("resolved_previous_findings", "still_open_previous_findings"):
        value = raw.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ReviewValidationError(f"{field} must be a string list")
    status = raw.get("status_update")
    if status is not None and not isinstance(status, str):
        raise ReviewValidationError("status_update must be a string")
    workspace = raw.get("workspace_status")
    if workspace is not None:
        if not isinstance(workspace, dict) or set(workspace) - {"checked_out_sha", "note"}:
            raise ReviewValidationError("workspace_status fields are invalid")
        if not all(isinstance(value, str) for value in workspace.values()):
            raise ReviewValidationError("workspace_status values must be strings")
    return raw


def _validate_comment(comment: object) -> None:
    if not isinstance(comment, dict) or set(comment) != _COMMENT_KEYS:
        raise ReviewValidationError("inline comment fields are invalid")
    path = comment["path"]
    if not isinstance(path, str) or _VALID_PATH.fullmatch(path) is None:
        raise ReviewValidationError("inline comment path must be repository-relative")
    if (
        not isinstance(comment["line"], int)
        or isinstance(comment["line"], bool)
        or comment["line"] < 1
    ):
        raise ReviewValidationError("inline comment line must be positive")
    if comment["priority"] not in PRIORITIES:
        raise ReviewValidationError("invalid inline comment priority")
    if not all(
        isinstance(comment[field], str) and comment[field].strip() for field in ("title", "body")
    ):
        raise ReviewValidationError("inline comment title and body are required")


def merge_reviews(reviews: Mapping[str, JsonObject], diff: str) -> JsonObject:
    if set(reviews) != {"claude_review", "codex_review"}:
        raise ReviewValidationError("exactly claude_review and codex_review are required")
    ordered = [("Claude", reviews["claude_review"]), ("Codex", reviews["codex_review"])]
    anchors = _diff_anchors(diff)
    comments: dict[tuple[str, int], JsonObject] = {}
    demoted: dict[tuple[str, int], JsonObject] = {}
    rank = {priority: index for index, priority in enumerate(PRIORITIES)}
    for _, review in ordered:
        for comment in review["inline_comments"]:
            key = (comment["path"], comment["line"])
            if key not in anchors:
                previous = demoted.get(key)
                if previous is None or rank[comment["priority"]] < rank[previous["priority"]]:
                    demoted[key] = comment
                continue
            previous = comments.get(key)
            if previous is None or rank[comment["priority"]] < rank[previous["priority"]]:
                comments[key] = comment

    summaries = [f"## {label}\n{review['summary'].strip()}" for label, review in ordered]
    if demoted:
        summaries.append(
            "## Findings without a valid diff anchor\n"
            + "\n".join(
                f"- **{item['priority']} · {item['title']}** "
                f"`{item['path']}:{item['line']}` — {item['body']}"
                for item in sorted(
                    demoted.values(),
                    key=lambda item: (rank[item["priority"]], item["path"], item["line"]),
                )
            )
        )
    resolved = set(ordered[0][1].get("resolved_previous_findings", []))
    resolved.intersection_update(ordered[1][1].get("resolved_previous_findings", []))
    still_open = list(
        dict.fromkeys(
            item
            for _, review in ordered
            for item in review.get("still_open_previous_findings", [])
        )
    )
    return {
        "summary": "\n\n".join(summaries),
        "approach_verdict": max(
            (review["approach_verdict"] for _, review in ordered), key=VERDICTS.index
        ),
        "has_blocking_issues": any(review["has_blocking_issues"] for _, review in ordered),
        "status_update": " ".join(
            value.strip()
            for _, review in ordered
            if isinstance((value := review.get("status_update")), str) and value.strip()
        ),
        "resolved_previous_findings": sorted(resolved),
        "still_open_previous_findings": still_open,
        "blocking_findings": sorted(
            (
                item
                for item in (*comments.values(), *demoted.values())
                if item["priority"] == "High"
            ),
            key=lambda item: (item["path"], item["line"]),
        ),
        "inline_comments": sorted(
            comments.values(),
            key=lambda item: (rank[item["priority"]], item["path"], item["line"]),
        )[:5],
    }


def _diff_anchors(diff: str) -> set[tuple[str, int]]:
    anchors: set[tuple[str, int]] = set()
    path: str | None = None
    new_line = 0
    old_remaining = 0
    new_remaining = 0
    for line in diff.splitlines():
        if old_remaining or new_remaining:
            if line.startswith("\\"):
                continue
            marker = line[:1]
            if marker == "+":
                anchors.add((path, new_line))  # type: ignore[arg-type]
                new_line += 1
                new_remaining -= 1
                continue
            if marker == "-":
                old_remaining -= 1
                continue
            if marker == " " or not line:
                anchors.add((path, new_line))  # type: ignore[arg-type]
                new_line += 1
                old_remaining -= 1
                new_remaining -= 1
                continue
            old_remaining = new_remaining = 0
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0]
            path = None if raw == "/dev/null" else raw.removeprefix("b/")
            continue
        match = _HUNK.match(line)
        if match and path:
            old_remaining = int(match.group(1) or 1)
            new_line = int(match.group(2))
            new_remaining = int(match.group(3) or 1)
    return anchors
