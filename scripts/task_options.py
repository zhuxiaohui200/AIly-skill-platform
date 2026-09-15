"""Task option helpers aligned with 51PM frontend (taskOptions.js)."""

from __future__ import annotations

import re
from typing import Any, Callable


def normalize_dept_name(name: Any) -> str:
    text = str(name or "").lower()
    return re.sub(r"[\s\-_\/（）()【】\[\]·]", "", text).strip()


def preferred_dept_from_user(user: dict[str, Any] | None) -> str:
    if not isinstance(user, dict):
        return ""
    fourth = str(
        user.get("department_name_fourth") or user.get("dept_name_fourth") or ""
    ).strip()
    if fourth:
        return fourth
    dept_name = str(user.get("dept_name") or user.get("department_name") or "").strip()
    if not dept_name:
        return ""
    parts = [p.strip() for p in dept_name.split("/") if p.strip()]
    return parts[-1] if parts else dept_name


def is_dept_roughly_matched(option_dept: str, preferred: str) -> bool:
    a = normalize_dept_name(option_dept)
    b = normalize_dept_name(preferred)
    if not a or not b:
        return False
    if a == b:
        return True
    return a in b or b in a


def extract_groups(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    return data if isinstance(data, list) else []


def flatten_tree(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten backend groups into parent nodes with children (frontend shape)."""
    nodes: list[dict[str, Any]] = []
    for group in groups:
        dept = str(group.get("dept_name") or "").strip() or "未分组"
        for parent in group.get("task_options") or []:
            if not isinstance(parent, dict):
                continue
            children_raw = parent.get("children") or []
            children = []
            for child in children_raw:
                if not isinstance(child, dict):
                    continue
                children.append(
                    {
                        "value": int(child["value"]),
                        "label": str(child.get("label") or "").strip(),
                        "groupLabel": dept,
                        "leaf": True,
                    }
                )
            nodes.append(
                {
                    "value": int(parent["value"]),
                    "label": str(parent.get("label") or "").strip(),
                    "groupLabel": dept,
                    "project_type": parent.get("project_type") or "",
                    "leaf": not bool(children),
                    "children": children,
                }
            )
    return nodes


def sort_tree_for_assignee(
    groups: list[dict[str, Any]], preferred_dept: str
) -> list[dict[str, Any]]:
    if not preferred_dept:
        return flatten_tree(groups)
    preferred: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for group in groups:
        dept = str(group.get("dept_name") or "").strip()
        bucket = preferred if is_dept_roughly_matched(dept, preferred_dept) else others
        bucket.append(group)
    return flatten_tree(preferred + others)


def list_choices(
    tree: list[dict[str, Any]], *, preferred_dept: str = ""
) -> list[dict[str, Any]]:
    """Selectable leaves with path labels for Agent / need_input."""
    choices: list[dict[str, Any]] = []
    for parent in tree:
        dept = str(parent.get("groupLabel") or "")
        parent_label = str(parent.get("label") or "")
        children = parent.get("children") or []
        preferred = bool(preferred_dept and is_dept_roughly_matched(dept, preferred_dept))
        if not children:
            choices.append(
                {
                    "id": int(parent["value"]),
                    "label": parent_label,
                    "path": parent_label,
                    "dept_name": dept,
                    "preferred": preferred,
                }
            )
            continue
        for child in children:
            child_label = str(child.get("label") or "")
            path = f"{parent_label}-{child_label}" if parent_label else child_label
            choices.append(
                {
                    "id": int(child["value"]),
                    "label": child_label,
                    "path": path,
                    "dept_name": dept,
                    "preferred": preferred,
                }
            )
    # preferred first
    choices.sort(key=lambda c: (0 if c.get("preferred") else 1, c.get("dept_name") or "", c.get("path") or ""))
    return choices


def _parse_token_list(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            parts.extend(_parse_token_list(item))
        return parts
    text = str(raw).strip()
    if not text:
        return []
    # support JSON-ish or common separators
    for sep in ("&", "，", ",", ";", "|", "\n"):
        if sep in text:
            return [p.strip() for p in text.split(sep) if p.strip()]
    return [text]


def resolve_option_ids(
    tree: list[dict[str, Any]],
    *,
    option_ids: Any = None,
    option_names: Any = None,
    preferred_dept: str = "",
) -> list[int]:
    """Resolve ids and/or labels (leaf / Parent-Child) to option_ids."""
    ids: list[int] = []
    seen: set[int] = set()

    def add(oid: int) -> None:
        if oid in seen:
            return
        seen.add(oid)
        ids.append(oid)

    # numeric ids first
    for token in _parse_token_list(option_ids):
        try:
            add(int(float(str(token).strip())))
        except (TypeError, ValueError):
            # allow mixed "平台研发-后端开发" in option_ids
            _match_names_into(tree, [str(token)], preferred_dept, add)

    names = _parse_token_list(option_names)
    if names:
        _match_names_into(tree, names, preferred_dept, add)
    return ids


def _match_names_into(
    tree: list[dict[str, Any]],
    names: list[str],
    preferred_dept: str,
    add: Callable[[int], None],
) -> None:
    # Expand "Parent-Child1、Child2" into multiple name tokens
    expanded: list[str] = []
    for name in names:
        text = str(name or "").strip()
        if not text:
            continue
        if "-" in text and "、" in text.split("-", 1)[1]:
            parent, children = text.split("-", 1)
            for child in children.split("、"):
                c = child.strip()
                if c:
                    expanded.append(f"{parent.strip()}-{c}")
        else:
            expanded.append(text)

    for name in expanded:
        oid = _match_one_name(tree, name, preferred_dept)
        if oid is not None:
            add(oid)


def _match_one_name(
    tree: list[dict[str, Any]], name: str, preferred_dept: str
) -> int | None:
    needle = str(name or "").strip()
    if not needle:
        return None
    needle_norm = normalize_dept_name(needle)

    candidates: list[tuple[int, int, str]] = []  # score, id, path

    for parent in tree:
        dept = str(parent.get("groupLabel") or "")
        preferred = 0 if (preferred_dept and is_dept_roughly_matched(dept, preferred_dept)) else 1
        parent_label = str(parent.get("label") or "")
        children = parent.get("children") or []

        if not children:
            path = parent_label
            if normalize_dept_name(path) == needle_norm or normalize_dept_name(parent_label) == needle_norm:
                candidates.append((preferred, int(parent["value"]), path))
            continue

        for child in children:
            child_label = str(child.get("label") or "")
            path = f"{parent_label}-{child_label}" if parent_label else child_label
            if normalize_dept_name(path) == needle_norm:
                candidates.append((preferred, int(child["value"]), path))
            elif normalize_dept_name(child_label) == needle_norm:
                candidates.append((preferred + 2, int(child["value"]), path))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[2]))
    return candidates[0][1]


def build_structured_label(ids: list[int], tree: list[dict[str, Any]]) -> str:
    id_set = {int(x) for x in ids}
    if not id_set:
        return ""
    segments: list[str] = []
    for parent in tree:
        parent_label = str(parent.get("label") or "").strip()
        parent_value = int(parent.get("value"))
        children = parent.get("children") or []
        selected = [
            str(c.get("label") or "").strip()
            for c in children
            if int(c.get("value")) in id_set and str(c.get("label") or "").strip()
        ]
        if not selected:
            if parent_value in id_set and parent_label:
                segments.append(parent_label)
            continue
        uniq = list(dict.fromkeys(selected))
        if parent_label:
            segments.append(f"{parent_label}-{('、'.join(uniq))}")
        else:
            segments.append("、".join(uniq))
    return "&".join(segments)


_AUTO_HOUR_RE = re.compile(r"\|\s*\d+(?:\.\d+)?H\s*$", re.I)
_AUTO_PAREN_RE = re.compile(r"（[^（）]*）\s*$")


def strip_auto_suffix(name: str, tree: list[dict[str, Any]] | None = None) -> str:
    result = str(name or "").strip()
    result = _AUTO_PAREN_RE.sub("", result)
    result = _AUTO_HOUR_RE.sub("", result).strip()
    if not tree:
        return result
    # strip trailing structured segments joined by &
    plain = set()
    children_by_parent: dict[str, set[str]] = {}
    for parent in tree:
        parent_label = str(parent.get("label") or "").strip()
        if not parent_label:
            continue
        children = parent.get("children") or []
        if not children:
            plain.add(parent_label)
            continue
        children_by_parent.setdefault(parent_label, set())
        for child in children:
            label = str(child.get("label") or "").strip()
            if label:
                children_by_parent[parent_label].add(label)

    def is_segment(segment: str) -> bool:
        text = segment.strip()
        if not text:
            return False
        if text in plain:
            return True
        if "-" not in text:
            return False
        parent_label, children_text = text.split("-", 1)
        parent_label = parent_label.strip()
        child_set = children_by_parent.get(parent_label)
        if not child_set:
            return False
        parts = [p.strip() for p in children_text.split("、") if p.strip()]
        return bool(parts) and all(p in child_set for p in parts)

    parts = [p.strip() for p in result.split("&") if p.strip()]
    while parts and is_segment(parts[-1]):
        parts.pop()
    return "&".join(parts).strip()


def compose_task_name(
    base_name: str,
    *,
    option_ids: list[int],
    tree: list[dict[str, Any]],
    hour: Any = None,
) -> str:
    clean = strip_auto_suffix(base_name, tree)
    suffix = build_structured_label(option_ids, tree)
    main = f"{clean}{suffix}" if suffix else clean
    try:
        hour_num = float(hour) if hour not in (None, "") else 0.0
    except (TypeError, ValueError):
        hour_num = 0.0
    if hour_num > 0:
        # keep integer-looking hours without trailing .0
        hour_text = str(int(hour_num)) if hour_num == int(hour_num) else str(hour_num)
        return f"{main} | {hour_text}H"
    return main


def fetch_user_info(api_request: Callable[..., Any], user_id: int) -> dict[str, Any]:
    payload = api_request("GET", "/manage_api/user/get_user_info", {"id": int(user_id)})
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {}


def fetch_task_option_groups(api_request: Callable[..., Any]) -> list[dict[str, Any]]:
    payload = api_request("GET", "/manage_api/index/get_task_options", {})
    return extract_groups(payload)
