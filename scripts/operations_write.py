"""Interactive write operations (dry_run → confirm → POST)."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Callable

from operations_domains import _resolve_not_project_id_value

_MOMENT_MODULE_MAP = {
    "": "",
    "meet": "meet",
    "会议": "meet",
    "meeting": "meet",
    "risk": "risk",
    "风险": "risk",
    "problem": "problem",
    "问题": "problem",
}

_CONFIRM_TYPE_MAP = {
    "confirmtaskestimate": "ConfirmTaskEstimate",
    "confirmnottaskestimate": "ConfirmNotTaskEstimate",
    "project": "ConfirmTaskEstimate",
    "task": "ConfirmTaskEstimate",
    "项目": "ConfirmTaskEstimate",
    "项目工时": "ConfirmTaskEstimate",
    "not_project": "ConfirmNotTaskEstimate",
    "非项目": "ConfirmNotTaskEstimate",
    "非项目工时": "ConfirmNotTaskEstimate",
}

_MODULE_LABEL = {"meet": "会议", "risk": "风险", "problem": "问题"}
_CONFIRM_LABEL = {
    "ConfirmTaskEstimate": "项目工时",
    "ConfirmNotTaskEstimate": "非项目工时",
}

# 会议待办落库格式与前端 AddMomentForm.serializeTodos 一致：
# remark = [{"text":"...","userIds":[1],"userNames":["张三"]}, ...]
_AT_NAME_RE = re.compile(r"@([^\s@,，；;：:、]+)")
_TODO_LINE_PREFIX_RE = re.compile(
    r"^(?:待办事项|待办|后续动作|后续安排|行动项|Action\s*Items?)\s*[:：]?\s*",
    re.I,
)
_NUMBERED_PREFIX_RE = re.compile(r"^[\d]+[\.、\)]\s*|^[-•●▪]\s*|\[\s*[xX ]?\s*\]\s*")
_ASSIGNEE_PREFIX_RE = re.compile(r"^([^：:]{1,20})[：:]\s*(.+)$")
_MAX_TODOS = 30
_MAX_TODO_TEXT = 500

# 同一任务同一天（同一填写人）只能有一条工时；跨天可各有一条；同日已存在则必须改走 update
# 填报 date 还必须落在任务 start_date～end_date 内
# 已确认 / 非当日历史工时不可 update（对齐 51PM）
UNIQUE_TASK_ESTIMATE_OPS: dict[str, dict[str, Any]] = {
    "add_project_task_estimate": {
        "kind": "项目",
        "update_operation": "update_project_task_estimate",
        "list_paths": (
            "/manage_api/project_task_estimate/get_task_estimate_list",
            "/manage_api/project_task_estimate/get_estimate_list",
        ),
        "task_info_path": "/manage_api/project_task/get_task_info",
    },
    "add_not_project_estimate": {
        "kind": "非项目",
        "update_operation": "update_not_project_estimate",
        "list_paths": (
            "/manage_api/project_not_task_estimate/get_task_estimate_list",
        ),
        "task_info_path": "/manage_api/project_not_task/get_task_info",
    },
}

UPDATE_ESTIMATE_OPS: dict[str, dict[str, Any]] = {
    "update_project_task_estimate": {
        "kind": "项目",
        "list_paths": (
            "/manage_api/project_task_estimate/get_task_estimate_list",
            "/manage_api/project_task_estimate/get_estimate_list",
        ),
    },
    "update_not_project_estimate": {
        "kind": "非项目",
        "list_paths": (
            "/manage_api/project_not_task_estimate/get_task_estimate_list",
        ),
    },
}


def _parse_estimate_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _estimate_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    from skill_enhancements import extract_page_items

    items, _ = extract_page_items(payload)
    out = [item for item in items if isinstance(item, dict)]
    if out:
        return out
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("estimate_list", "estimates", "list", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("estimate_list", "estimates", "list"):
                value = nested.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_estimate_date(value: Any) -> str:
    """Normalize API/CLI date values to YYYY-MM-DD for comparison."""
    text = str(value or "").strip()
    if not text:
        return ""
    # "2026-09-03 00:00:00" / ISO → first 10 chars when looks like date
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _task_info_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return {}
    info = data.get("info")
    if isinstance(info, dict):
        return info
    return data


def fetch_task_date_range(
    *,
    task_info_path: str,
    task_id: int,
    token: str | None,
    api_request_fn,
) -> tuple[str, str] | None:
    """Return (start_date, end_date) YYYY-MM-DD for task, or None if unavailable."""
    import api_client

    try:
        payload = api_request_fn(
            "GET",
            task_info_path,
            {"id": task_id},
            token=token,
        )
    except (api_client.ClientError, OSError):
        return None
    info = _task_info_from_payload(payload)
    start = _normalize_estimate_date(
        info.get("start_date") or info.get("begin_date") or info.get("start_time")
    )
    end = _normalize_estimate_date(
        info.get("end_date") or info.get("finish_date") or info.get("end_time")
    )
    if not start and not end:
        return None
    return start, end


def enforce_estimate_date_in_task_range(
    operation: str,
    body: dict[str, Any],
    *,
    token: str | None,
    api_request_fn,
) -> None:
    """Spend date must fall within the task's start_date～end_date (when known)."""
    spec = UNIQUE_TASK_ESTIMATE_OPS.get(operation)
    if not spec:
        return
    task_id = _parse_estimate_int(body.get("task_id"))
    if task_id is None:
        return
    work_date = _normalize_estimate_date(body.get("date") or body.get("work_date"))
    if not work_date:
        work_date = date.today().isoformat()
    path = str(spec.get("task_info_path") or "")
    if not path:
        return
    span = fetch_task_date_range(
        task_info_path=path,
        task_id=task_id,
        token=token,
        api_request_fn=api_request_fn,
    )
    if not span:
        return
    start, end = span
    # Partial bounds: only enforce sides we know
    if start and work_date < start:
        _raise_estimate_date_out_of_range(
            operation=operation,
            task_id=task_id,
            work_date=work_date,
            start=start,
            end=end,
            kind=str(spec["kind"]),
        )
    if end and work_date > end:
        _raise_estimate_date_out_of_range(
            operation=operation,
            task_id=task_id,
            work_date=work_date,
            start=start,
            end=end,
            kind=str(spec["kind"]),
        )


def _raise_estimate_date_out_of_range(
    *,
    operation: str,
    task_id: int,
    work_date: str,
    start: str,
    end: str,
    kind: str,
) -> None:
    import api_client

    range_text = f"{start or '?'} ~ {end or '?'}"
    payload = {
        "error": "estimate_date_out_of_range",
        "detail": (
            f"该{kind}任务（task_id={task_id}）的时间范围是 {range_text}，"
            f"不能在 {work_date} 填写花费。请改用范围内的日期，或换在该日期仍有效的任务。"
        ),
        "operation": operation,
        "task_id": task_id,
        "date": work_date,
        "task_start_date": start or None,
        "task_end_date": end or None,
        "message": f"花费日期 {work_date} 不在任务时间范围 {range_text} 内",
        "agent_instruction": (
            "禁止强行 add。"
            "向用户说明：只能在任务起止日期内填工时；"
            "请换范围内的 date，或先确认任务周期后再填。"
        ),
        "next_step": "把任务时间范围告知用户，请用户改日期或换任务后重试",
    }
    raise api_client.ClientError(
        "estimate_date_out_of_range",
        json.dumps(payload, ensure_ascii=False),
    )


def _is_estimate_confirmed(status: Any) -> bool:
    """confirm_status: 0=待确认；非 0（常为确认人 user_id）=已确认。"""
    if status in (None, "", 0, "0", "待确认", "pending", "unconfirmed"):
        return False
    if status in (1, "1", "已确认", "confirmed"):
        return True
    try:
        return int(status) != 0
    except (TypeError, ValueError):
        text = str(status).strip().lower()
        return text not in ("0", "待确认", "pending", "unconfirmed")


def _estimate_record_id(item: dict[str, Any]) -> int | None:
    return _parse_estimate_int(
        item.get("id")
        or item.get("eid")
        or item.get("estimate_id")
        or item.get("work_hour_id")
    )


def _raise_estimate_immutable(
    *,
    error: str,
    operation: str,
    estimate_id: int,
    kind: str,
    work_date: str,
    confirm_status: Any,
    detail: str,
    message: str,
) -> None:
    import api_client

    payload = {
        "error": error,
        "detail": detail,
        "operation": operation,
        "estimate_id": estimate_id,
        "date": work_date or None,
        "confirm_status": confirm_status,
        "message": message,
        "agent_instruction": (
            "禁止调用 update 修改该工时。"
            "向用户说明：已确认或历史工时不可改；"
            "若需调整，只能改当日且仍待确认的记录，或联系管理员/走开放日流程。"
        ),
        "next_step": "告知用户该记录不可修改，不要再次尝试 update",
    }
    raise api_client.ClientError(error, json.dumps(payload, ensure_ascii=False))


def fetch_estimate_by_id(
    *,
    estimate_id: int,
    list_paths: tuple[str, ...] | list[str],
    task_id: int | None,
    work_date: str,
    token: str | None,
    api_request_fn,
) -> dict[str, Any] | None:
    """Load one estimate row for update checks (by task list and/or my panel list)."""
    import api_client

    if task_id is not None:
        for path in list_paths:
            try:
                payload = api_request_fn(
                    "GET",
                    path,
                    {"task_id": task_id, "page": 1, "limit": 100},
                    token=token,
                )
            except (api_client.ClientError, OSError):
                continue
            for item in _estimate_items_from_payload(payload):
                if _estimate_record_id(item) == estimate_id:
                    return item

    # Fallback: my panel list (id field is eid), scoped by date when known
    if work_date:
        start = end = work_date
    else:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=400)).isoformat()
    try:
        payload = api_request_fn(
            "GET",
            "/manage_api/main_panel/get_estimate_time_list",
            {"start_date": start, "end_date": end},
            token=token,
        )
    except (api_client.ClientError, OSError):
        return None
    items = _estimate_items_from_payload(payload)
    if not items and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            items = [x for x in data if isinstance(x, dict)]
    for item in items:
        if _estimate_record_id(item) == estimate_id:
            return item
    return None


def enforce_estimate_modifiable(
    operation: str,
    body: dict[str, Any],
    *,
    token: str | None,
    api_request_fn,
) -> None:
    """Block update of confirmed or historical (non-today) estimates."""
    spec = UPDATE_ESTIMATE_OPS.get(operation)
    if not spec:
        return
    estimate_id = _parse_estimate_int(body.get("id") or body.get("estimate_id"))
    if estimate_id is None:
        return
    task_id = _parse_estimate_int(body.get("task_id"))
    work_date = _normalize_estimate_date(body.get("date") or body.get("work_date"))
    existing = fetch_estimate_by_id(
        estimate_id=estimate_id,
        list_paths=spec["list_paths"],
        task_id=task_id,
        work_date=work_date,
        token=token,
        api_request_fn=api_request_fn,
    )
    if not existing:
        return
    kind = str(spec["kind"])
    confirm_status = existing.get("confirm_status")
    existing_date = _normalize_estimate_date(
        existing.get("date") or existing.get("work_date") or existing.get("estimate_date")
    ) or work_date
    today = date.today().isoformat()

    if _is_estimate_confirmed(confirm_status):
        _raise_estimate_immutable(
            error="estimate_confirmed_immutable",
            operation=operation,
            estimate_id=estimate_id,
            kind=kind,
            work_date=existing_date,
            confirm_status=confirm_status,
            detail=(
                f"该{kind}工时（id={estimate_id}"
                + (f"，日期 {existing_date}" if existing_date else "")
                + "）已确认，不可修改。"
            ),
            message="已确认的工时不可修改",
        )
    if existing_date and existing_date != today:
        _raise_estimate_immutable(
            error="estimate_historical_immutable",
            operation=operation,
            estimate_id=estimate_id,
            kind=kind,
            work_date=existing_date,
            confirm_status=confirm_status,
            detail=(
                f"该{kind}工时（id={estimate_id}，日期 {existing_date}）属于历史记录，"
                f"仅允许修改当天（{today}）且仍待确认的工时。"
            ),
            message="历史工时不可修改",
        )


def find_existing_task_estimate(
    *,
    list_paths: tuple[str, ...] | list[str],
    task_id: int,
    user_id: int | None,
    work_date: str,
    token: str | None,
    api_request_fn,
) -> dict[str, Any] | None:
    """Return estimate for this task+date (optionally scoped to user). Cross-day rows are ignored."""
    import api_client

    target_date = _normalize_estimate_date(work_date)
    if not target_date:
        return None

    last_error: Exception | None = None
    for path in list_paths:
        try:
            params: dict[str, Any] = {"task_id": task_id, "page": 1, "limit": 100}
            # Prefer server-side date filter when supported
            params["date"] = target_date
            params["start_date"] = target_date
            params["end_date"] = target_date
            payload = api_request_fn(
                "GET",
                path,
                params,
                token=token,
            )
        except (api_client.ClientError, OSError) as exc:
            last_error = exc
            continue
        items = _estimate_items_from_payload(payload)
        matches: list[dict[str, Any]] = []
        for item in items:
            item_task = _parse_estimate_int(item.get("task_id"))
            if item_task is not None and item_task != task_id:
                continue
            item_date = _normalize_estimate_date(
                item.get("date") or item.get("work_date") or item.get("estimate_date")
            )
            if item_date and item_date != target_date:
                continue
            # If list API ignored date filter and row has no date, skip (avoid false duplicate)
            if not item_date:
                continue
            item_user = _parse_estimate_int(
                item.get("user_id") or item.get("uid") or item.get("create_user_id")
            )
            if user_id is not None and item_user is not None and item_user != user_id:
                continue
            matches.append(item)
        if user_id is not None:
            same_user = [
                item
                for item in matches
                if _parse_estimate_int(
                    item.get("user_id") or item.get("uid") or item.get("create_user_id")
                )
                == user_id
            ]
            if same_user:
                matches = same_user
        return matches[0] if matches else None
    _ = last_error
    return None


def duplicate_estimate_payload(
    *,
    operation: str,
    task_id: int,
    work_date: str,
    existing: dict[str, Any],
    update_operation: str,
    kind: str,
) -> dict[str, Any]:
    existing_id = _parse_estimate_int(
        existing.get("id") or existing.get("estimate_id") or existing.get("work_hour_id")
    )
    day = _normalize_estimate_date(work_date) or _normalize_estimate_date(existing.get("date"))
    return {
        "error": "duplicate_estimate",
        "detail": (
            f"该{kind}任务（task_id={task_id}）在 {day} 已有工时记录，"
            f"同一任务同一天只能有一条。请改用 {update_operation} 更新当日记录，不要再次 add。"
            f"若要填其他日期，请带对应 date 再 add（跨天允许各一条）。"
        ),
        "operation": operation,
        "task_id": task_id,
        "date": day,
        "existing_estimate_id": existing_id,
        "existing": {
            "id": existing_id,
            "task_id": _parse_estimate_int(existing.get("task_id")) or task_id,
            "date": existing.get("date") or day,
            "consumed": existing.get("consumed"),
            "remark": existing.get("remark"),
            "user_id": existing.get("user_id") or existing.get("uid"),
        },
        "next_operation": update_operation,
        "message": f"同一任务在 {day} 已有工时，请改为更新当日记录而不是新增。",
        "agent_instruction": (
            f"禁止再次对同一天调用 {operation}。"
            f"向用户说明：任务可跨天填报，但同一天只能有一条；"
            f"{day} 已有记录（id={existing_id}）。"
            f"若要改当天工时：调用 {update_operation} --param id {existing_id}；"
            f"若要填其他日期：用 {operation} 并带上新的 --param date YYYY-MM-DD（不要误报会覆盖其他天）。"
        ),
        "example_command": (
            f"python3 scripts/api_client.py {update_operation} "
            f"--param id {existing_id} --param consumed <工时> --param remark <备注>"
        ),
    }


def enforce_unique_task_estimate(
    operation: str,
    body: dict[str, Any],
    *,
    token: str | None,
    api_request_fn,
    actor_user_id: int | None = None,
) -> None:
    spec = UNIQUE_TASK_ESTIMATE_OPS.get(operation)
    if not spec:
        return
    task_id = _parse_estimate_int(body.get("task_id"))
    if task_id is None:
        return
    work_date = _normalize_estimate_date(body.get("date") or body.get("work_date"))
    if not work_date:
        work_date = date.today().isoformat()
    user_id = _parse_estimate_int(body.get("user_id")) or actor_user_id
    existing = find_existing_task_estimate(
        list_paths=spec["list_paths"],
        task_id=task_id,
        user_id=user_id,
        work_date=work_date,
        token=token,
        api_request_fn=api_request_fn,
    )
    if not existing:
        return
    import api_client

    existing_id = _estimate_record_id(existing)
    confirm_status = existing.get("confirm_status")
    if existing_id is not None and _is_estimate_confirmed(confirm_status):
        _raise_estimate_immutable(
            error="estimate_confirmed_immutable",
            operation=operation,
            estimate_id=existing_id,
            kind=str(spec["kind"]),
            work_date=work_date,
            confirm_status=confirm_status,
            detail=(
                f"该{spec['kind']}任务（task_id={task_id}）在 {work_date} 已有工时且已确认"
                f"（id={existing_id}），不可再新增或修改。"
            ),
            message=f"{work_date} 的工时已确认，不可修改",
        )

    payload = duplicate_estimate_payload(
        operation=operation,
        task_id=task_id,
        work_date=work_date,
        existing=existing,
        update_operation=str(spec["update_operation"]),
        kind=str(spec["kind"]),
    )
    raise api_client.ClientError("duplicate_estimate", json.dumps(payload, ensure_ascii=False))

def resolve_write(mode: str, params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "confirm_work_hours": _resolve_confirm_work_hours,
        "add_project_moment": _resolve_add_project_moment,
        "add_project_task_estimate": _resolve_add_project_task_estimate,
        "confirm_user_work_hours": _resolve_confirm_user_work_hours,
        "approve_work_hour_batch": _resolve_approve_work_hour_batch,
        "add_not_project_estimate": _resolve_add_not_project_estimate,
        "update_project_task_estimate": _resolve_update_project_task_estimate,
        "update_project_moment": _resolve_update_project_moment,
        "add_bug": _resolve_add_bug,
        "apply_publish": _resolve_apply_publish,
        "reject_publish": _resolve_reject_publish,
        "task_action": _resolve_task_action,
        "save_project_overview_value": _resolve_save_project_overview_value,
        "add_not_project_demand": _resolve_add_not_project_demand,
        "add_not_project_task": _resolve_add_not_project_task,
        "finish_not_project_task": _resolve_finish_not_project_task,
        "update_not_project_estimate": _resolve_update_not_project_estimate,
        "add_apply_demand": _resolve_add_apply_demand,
        "add_demand_pool": _resolve_add_demand_pool,
        "add_feedback_demand_pool": _resolve_add_feedback_demand_pool,
        "approve_publish_apply": _resolve_approve_publish_apply,
    }
    handler = handlers.get(mode)
    if not handler:
        raise api_client.ClientError("validation_error", f"unknown write resolve_mode: {mode}")
    return handler(dict(params))


def _resolve_confirm_work_hours(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params, {"id": ("estimate_id", "work_hour_id")}
    )
    if out.get("id") in (None, ""):
        for key in ("estimate_id", "work_hour_id"):
            if out.get(key) not in (None, ""):
                out["id"] = int(out[key])
                break
    out.pop("estimate_id", None)
    out.pop("work_hour_id", None)
    raw_type = str(out.get("confirm_type", "")).strip()
    key = raw_type.replace(" ", "").lower()
    if key in _CONFIRM_TYPE_MAP:
        out["confirm_type"] = _CONFIRM_TYPE_MAP[key]
    elif raw_type in _CONFIRM_TYPE_MAP.values():
        out["confirm_type"] = raw_type
    elif raw_type:
        out["confirm_type"] = raw_type
    if out.get("id") not in (None, ""):
        out["id"] = int(out["id"])
    ids_raw = out.pop("ids", None)
    if ids_raw not in (None, ""):
        import api_client

        out["_batch_ids"] = api_client._parse_int_list(ids_raw)
    return out


def _parse_json_value(value: Any) -> Any:
    if value in (None, ""):
        return value
    if isinstance(value, (list, dict)):
        return value
    text = str(value).strip()
    if text.startswith("[") or text.startswith("{"):
        return json.loads(text)
    return value


def _resolve_confirm_user_work_hours(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = dict(params)
    user_name = out.pop("user_name", None)
    if user_name and not out.get("user_id"):
        out["user_id"] = int(api_client.resolve_user_id(str(user_name)))
    if not out.get("date"):
        out["date"] = date.today().isoformat()
    if out.get("user_id") not in (None, ""):
        out["user_id"] = int(out["user_id"])
    return out


def _resolve_approve_work_hour_batch(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = dict(params)
    user_name = out.pop("user_name", None)
    if user_name and not out.get("user_id"):
        out["user_id"] = int(api_client.resolve_user_id(str(user_name)))
    if out.get("user_id") not in (None, ""):
        out["user_id"] = int(out["user_id"])
    project_ids = out.pop("project_ids", None) or out.pop("id_arr_one", None)
    not_project_ids = out.pop("not_project_ids", None) or out.pop("id_arr_two", None)
    if project_ids not in (None, ""):
        out["id_arr_one"] = api_client._parse_int_list(project_ids)
    if not_project_ids not in (None, ""):
        out["id_arr_two"] = api_client._parse_int_list(not_project_ids)
    if out.get("module_sort") not in (None, ""):
        out["module_sort"] = int(out["module_sort"])
    return out


def _resolve_add_not_project_estimate(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {
            "date": ("work_date", "start_date"),
            "consumed": ("hours", "work_hours"),
            "remark": ("note", "description"),
            "task_id": ("id",),
        },
    )
    if not out.get("date"):
        out["date"] = date.today().isoformat()
    if out.get("consumed") not in (None, ""):
        out["consumed"] = float(out["consumed"])
    user_name = out.pop("user_name", None)
    if user_name and not out.get("user_id"):
        out["user_id"] = int(api_client.resolve_user_id(str(user_name)))
    if out.get("task_id") in (None, "") and out.get("id") not in (None, ""):
        out["task_id"] = int(out.pop("id"))
    elif out.get("task_id") not in (None, ""):
        out["task_id"] = int(out["task_id"])
    if out.get("user_id") not in (None, ""):
        out["user_id"] = int(out["user_id"])
    return out


def _resolve_update_project_task_estimate(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {
            "id": ("estimate_id", "work_hour_id"),
            "consumed": ("hours", "work_hours"),
            "remark": ("note", "description"),
            "date": ("work_date",),
        },
    )
    if out.get("id") in (None, ""):
        for key in ("estimate_id", "work_hour_id"):
            if out.get(key) not in (None, ""):
                out["id"] = int(out[key])
                break
    out.pop("estimate_id", None)
    out.pop("work_hour_id", None)
    if out.get("id") not in (None, ""):
        out["id"] = int(out["id"])
    if out.get("consumed") not in (None, ""):
        out["consumed"] = float(out["consumed"])
    return out


def _resolve_update_project_moment(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(params, {"id": ("moment_id",)})
    if out.get("id") in (None, "") and out.get("moment_id") not in (None, ""):
        out["id"] = int(out.pop("moment_id"))
    elif out.get("id") not in (None, ""):
        out["id"] = int(out["id"])
    # 会议待办：同 add，支持 todos / 多行 remark → JSON；相关人员写入 user_id
    module = str(out.get("module") or "").strip()
    module = _MOMENT_MODULE_MAP.get(module, _MOMENT_MODULE_MAP.get(module.lower(), module))
    if module:
        out["module"] = module
    if module == "meet" or out.get("todos") not in (None, "") or (
        isinstance(out.get("remark"), str)
        and out.get("remark", "").strip().startswith("[")
    ):
        # update 常不传 module；有 todos 或 JSON remark 时按会议待办处理
        _apply_meet_todos_to_body(out)
    else:
        user_ids = _collect_moment_user_ids(out, None)
        for key in (
            "user_names",
            "user_name",
            "attendees",
            "related_persons",
            "related_person",
            "related_users",
            "todos",
        ):
            out.pop(key, None)
        if user_ids:
            out["user_id"] = user_ids
        elif out.get("user_id") and not isinstance(out.get("user_id"), list):
            out["user_id"] = api_client._parse_int_list(out["user_id"])
    return out


def _resolve_add_bug(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {"project_name": ("name",), "sj_num": ("opportunity_no", "business_no")},
    )
    if not out.get("project_id"):
        out["project_id"] = int(api_client.resolve_project_id(out))
    for key in ("project_name", "name", "sj_num", "opportunity_no", "business_no"):
        out.pop(key, None)
    if not out.get("submit_time"):
        out["submit_time"] = date.today().isoformat()
    for name_key, id_key in (
        ("tester_name", "tester"),
        ("assigned_to_name", "assigned_to"),
        ("assignee_name", "assigned_to"),
    ):
        nm = out.pop(name_key, None)
        if nm and not out.get(id_key):
            out[id_key] = int(api_client.resolve_user_id(str(nm)))
    for key in ("project_id", "task_id", "bug_type", "bug_status", "bug_level", "priority", "tester", "assigned_to"):
        if out.get(key) not in (None, ""):
            out[key] = int(out[key])
    return out


def _resolve_apply_publish(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {"project_name": ("name",), "sj_num": ("opportunity_no", "business_no")},
    )
    if not out.get("project_id"):
        out["project_id"] = int(api_client.resolve_project_id(out))
    if not out.get("sj_num"):
        payload = api_client.api_request(
            "GET",
            "/manage_api/project/get_project_info",
            {"id": str(out["project_id"])},
        )
        data = api_client._business_data(payload)
        info = data.get("info") if isinstance(data, dict) else {}
        if isinstance(info, dict) and info.get("sj_num"):
            out["sj_num"] = str(info["sj_num"])
        if isinstance(info, dict) and info.get("name") and not out.get("project_name"):
            out["project_name"] = str(info["name"])
    for key in ("name", "opportunity_no", "business_no"):
        out.pop(key, None)
    pool = out.pop("demand_pool_id", None) or out.pop("demand_pool_ids", None)
    if pool not in (None, ""):
        out["demand_pool_id"] = api_client._parse_int_list(pool)
    if out.get("publish_dijiao_version") not in (None, ""):
        out["publish_dijiao_version"] = int(out["publish_dijiao_version"])
    if out.get("project_id") not in (None, ""):
        out["project_id"] = int(out["project_id"])
    return out


def _resolve_reject_publish(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if out.get("id") in (None, "") and out.get("publish_id") not in (None, ""):
        out["id"] = int(out.pop("publish_id"))
    elif out.get("id") not in (None, ""):
        out["id"] = int(out["id"])
    return out


def _resolve_task_action(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(params, {"id": ("task_id",)})
    if out.get("id") in (None, "") and out.get("task_id") not in (None, ""):
        out["id"] = int(out.pop("task_id"))
    elif out.get("id") not in (None, ""):
        out["id"] = int(out["id"])
    assigned = out.pop("assigned_to_names", None) or out.pop("assigned_to_name", None)
    if assigned and not out.get("assigned_to"):
        ids = []
        for part in str(assigned).replace("，", ",").split(","):
            name = part.strip()
            if name:
                ids.append(int(api_client.resolve_user_id(name)))
        out["assigned_to"] = ids
    elif out.get("assigned_to") and not isinstance(out.get("assigned_to"), list):
        out["assigned_to"] = api_client._parse_int_list(out["assigned_to"])
    return out


def _resolve_save_project_overview_value(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {"project_name": ("name",), "sj_num": ("opportunity_no", "business_no")},
    )
    if not out.get("project_id"):
        out["project_id"] = int(api_client.resolve_project_id(out))
    for key in ("project_name", "name", "sj_num", "opportunity_no", "business_no"):
        out.pop(key, None)
    rows = out.get("rows")
    out["rows"] = _parse_json_value(rows)
    if out.get("project_id") not in (None, ""):
        out["project_id"] = int(out["project_id"])
    return out


def _resolve_assigned_to_list(out: dict[str, Any], name_key: str = "assigned_to_names") -> dict[str, Any]:
    import api_client

    names = out.pop(name_key, None) or out.pop("assigned_to_name", None) or out.pop("user_names", None)
    if names and not out.get("assigned_to"):
        ids: list[int] = []
        for part in str(names).replace("，", ",").split(","):
            name = part.strip()
            if name:
                ids.append(int(api_client.resolve_user_id(name)))
        out["assigned_to"] = ids
    elif out.get("assigned_to") and not isinstance(out.get("assigned_to"), list):
        out["assigned_to"] = api_client._parse_int_list(out["assigned_to"])
    return out


def _resolve_project_apply_fields(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {"project_name": ("name",), "sj_num": ("opportunity_no", "business_no")},
    )
    if not out.get("project_id"):
        out["project_id"] = int(api_client.resolve_project_id(out))
    if not out.get("sj_num"):
        payload = api_client.api_request(
            "GET",
            "/manage_api/project/get_project_info",
            {"id": str(out["project_id"])},
        )
        data = api_client._business_data(payload)
        info = data.get("info") if isinstance(data, dict) else {}
        if isinstance(info, dict):
            if info.get("sj_num"):
                out["sj_num"] = str(info["sj_num"])
            if info.get("name") and not out.get("project_name"):
                out["project_name"] = str(info["name"])
    for key in ("name", "opportunity_no", "business_no"):
        out.pop(key, None)
    if out.get("project_id") not in (None, ""):
        out["project_id"] = int(out["project_id"])
    return out


def _resolve_demand_pool_params(params: list[Any], *, feedback: bool) -> list[dict[str, Any]]:
    import api_client

    resolved: list[dict[str, Any]] = []
    for raw in params:
        if not isinstance(raw, dict):
            raise api_client.ClientError("validation_error", "params must be a JSON array of objects")
        item = dict(raw)
        if feedback:
            item["demand_type"] = "反馈"
        executor_name = item.pop("executor_name", None)
        if executor_name and not item.get("executor"):
            uid = int(api_client.resolve_user_id(str(executor_name)))
            item["executor"] = uid
            item.setdefault("executor_name", str(executor_name))
        if item.get("executor") not in (None, ""):
            item["executor"] = int(item["executor"])
        if item.get("cost") not in (None, ""):
            item["cost"] = float(item["cost"])
        if item.get("option_ids") and not isinstance(item.get("option_ids"), list):
            item["option_ids"] = api_client._parse_int_list(item["option_ids"])
        resolved.append(item)
    return resolved


_TASK_STATUS_MAP = {
    "": "doing",
    "doing": "doing",
    "进行中": "doing",
    "in_progress": "doing",
    "in-progress": "doing",
    "wait": "wait",
    "待开始": "wait",
    "未开始": "wait",
    "pending": "wait",
    "done": "done",
    "已完成": "done",
    "完成": "done",
    "pause": "pause",
    "暂停": "pause",
    "closed": "closed",
    "已关闭": "closed",
    "cancel": "cancel",
    "已取消": "cancel",
}


def _normalize_task_status(value: Any, *, default: str = "doing") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    mapped = _TASK_STATUS_MAP.get(text) or _TASK_STATUS_MAP.get(text.lower())
    return mapped or text


# 与网页端 DemandOneTypeOptions / TaskSplitTypeOptions 一致：3 非产出 / 4 产出 / 0 同上
_ONE_TYPE_MAP: dict[str, int] = {
    "3": 3,
    "非产出": 3,
    "non_output": 3,
    "non-output": 3,
    "4": 4,
    "产出": 4,
    "output": 4,
    "0": 0,
    "同上": 0,
    "same": 0,
}


def _normalize_one_type(value: Any, *, default: int = 3) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        iv = int(value)
        return iv if iv in (0, 3, 4) else default
    text = str(value).strip()
    if not text:
        return default
    if text in _ONE_TYPE_MAP:
        return _ONE_TYPE_MAP[text]
    lower = text.lower()
    if lower in _ONE_TYPE_MAP:
        return _ONE_TYPE_MAP[lower]
    try:
        iv = int(float(text))
        return iv if iv in (0, 3, 4) else default
    except (TypeError, ValueError):
        return default


def _resolve_add_not_project_demand(params: dict[str, Any]) -> dict[str, Any]:
    out = _resolve_not_project_id_value(dict(params))
    out = _resolve_assigned_to_list(out)
    if out.get("assigned_to") and isinstance(out.get("assigned_to"), list) and len(out["assigned_to"]) == 1:
        out["assigned_to"] = [int(x) for x in out["assigned_to"]]
    elif out.get("assigned_to"):
        out["assigned_to"] = [int(x) for x in out["assigned_to"]]
    if out.get("project_id") not in (None, ""):
        out["project_id"] = int(out["project_id"])
    if out.get("plan_hour") not in (None, ""):
        out["plan_hour"] = float(out["plan_hour"])
    # 与网页端一致：新建默认进行中，否则状态为空无法正常推进/填花费
    out["status"] = _normalize_task_status(out.get("status"), default="doing")
    return out


def _resolve_add_not_project_task(params: dict[str, Any]) -> dict[str, Any]:
    import api_client
    from need_input import build_need_input_payload
    from task_options import (
        compose_task_name,
        fetch_task_option_groups,
        fetch_user_info,
        list_choices,
        preferred_dept_from_user,
        resolve_option_ids,
        sort_tree_for_assignee,
    )

    out = _resolve_not_project_id_value(dict(params))
    out = api_client._apply_field_aliases(out, {"pid": ("demand_id",)})
    assignee = out.pop("assignee_name", None) or out.pop("user_name", None)
    if assignee and not out.get("assigned_to"):
        out["assigned_to"] = int(api_client.resolve_user_id(str(assignee)))
    # 网页端新建默认指派当前登录人；Skill 由调用方注入 _actor_user_id
    actor_uid = out.pop("_actor_user_id", None)
    if out.get("assigned_to") in (None, "") and actor_uid not in (None, ""):
        out["assigned_to"] = int(actor_uid)
    if out.get("project_id") not in (None, ""):
        out["project_id"] = int(out["project_id"])
    if out.get("pid") not in (None, ""):
        out["pid"] = int(out["pid"])
    if out.get("assigned_to") not in (None, ""):
        out["assigned_to"] = int(out["assigned_to"])
    if out.get("plan_hour") not in (None, ""):
        out["plan_hour"] = float(out["plan_hour"])
    # 产出类型：可用 one_type / output_type / 产出|非产出；未传时与网页端一致默认非产出(3)
    if out.get("one_type") in (None, "") and out.get("output_type") not in (None, ""):
        out["one_type"] = out.pop("output_type")
    else:
        out.pop("output_type", None)
    out["one_type"] = _normalize_one_type(out.get("one_type"), default=3)
    # 与网页端一致：默认 doing
    out["status"] = _normalize_task_status(out.get("status"), default="doing")

    # —— 任务选项（与网页端一致：按指派人所属组优先，名称自动拼接）——
    leave_project = int(out.get("project_id") or 0) == 1496
    raw_option_ids = out.pop("option_ids", None)
    raw_option_names = (
        out.pop("task_options", None)
        or out.pop("option_names", None)
        or out.pop("task_option_names", None)
    )
    # allow task_options to carry numeric ids
    if raw_option_ids in (None, "") and raw_option_names not in (None, ""):
        # if all tokens look numeric, treat as ids
        tokens = [
            t.strip()
            for t in str(raw_option_names).replace("，", ",").split(",")
            if str(t).strip()
        ]
        if tokens and all(re.fullmatch(r"\d+", t) for t in tokens):
            raw_option_ids = raw_option_names
            raw_option_names = None

    if not leave_project:
        groups = fetch_task_option_groups(api_client.api_request)
        preferred_dept = ""
        if out.get("assigned_to"):
            preferred_dept = preferred_dept_from_user(
                fetch_user_info(api_client.api_request, int(out["assigned_to"]))
            )
        tree = sort_tree_for_assignee(groups, preferred_dept)
        option_ids = resolve_option_ids(
            tree,
            option_ids=raw_option_ids,
            option_names=raw_option_names,
            preferred_dept=preferred_dept,
        )
        if not option_ids:
            choices = list_choices(tree, preferred_dept=preferred_dept)
            preferred_choices = [c for c in choices if c.get("preferred")] or choices[:40]
            payload = build_need_input_payload(
                "add_not_project_task",
                WRITE_OPERATIONS["add_not_project_task"],
                missing_fields=["task_options"],
                message=(
                    "创建非项目任务需选择指派人所属组的「任务选项」；"
                    "选项会拼进任务名称（与网页端一致）。"
                ),
                provided={
                    k: out.get(k)
                    for k in (
                        "project_id",
                        "pid",
                        "name",
                        "assigned_to",
                        "start_date",
                        "end_date",
                        "plan_hour",
                    )
                },
            )
            payload["preferred_dept"] = preferred_dept or None
            payload["task_option_choices"] = preferred_choices
            payload["form"] = [
                {
                    "name": "task_options",
                    "label": "任务选项",
                    "type": "select",
                    "hint": (
                        f"优先选指派人组「{preferred_dept or '未知'}」下的选项；"
                        "可多选，传 path 或 id，例如 平台研发-后端开发 或 option_ids=12,34"
                    ),
                    "choices": [
                        {"value": c["path"], "label": f"{c['path']}（{c['dept_name']}）"}
                        for c in preferred_choices
                    ],
                }
            ]
            payload["next_step"] = (
                "把 task_option_choices 展示给用户选择后，带 "
                "--param task_options 平台研发-后端开发 "
                "或 --param option_ids 12,34 重试（可多选）"
            )
            raise api_client.ClientError(
                "need_input", json.dumps(payload, ensure_ascii=False)
            )
        out["option_ids"] = option_ids
        base_name = str(out.get("name") or "").strip()
        out["name"] = compose_task_name(
            base_name,
            option_ids=option_ids,
            tree=tree,
            hour=out.get("plan_hour"),
        )
    else:
        if raw_option_ids not in (None, ""):
            out["option_ids"] = api_client._parse_int_list(raw_option_ids)
        elif out.get("option_ids") and not isinstance(out.get("option_ids"), list):
            out["option_ids"] = api_client._parse_int_list(out["option_ids"])

    return out


def _resolve_finish_not_project_task(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if out.get("id") in (None, "") and out.get("task_id") not in (None, ""):
        out["id"] = int(out.pop("task_id"))
    elif out.get("id") not in (None, ""):
        out["id"] = int(out["id"])
    return out


def _resolve_update_not_project_estimate(params: dict[str, Any]) -> dict[str, Any]:
    out = _resolve_update_project_task_estimate(params)
    return out


def _resolve_add_apply_demand(params: dict[str, Any]) -> dict[str, Any]:
    out = _resolve_project_apply_fields(params)
    modules = out.pop("apply_demand_module", None) or out.pop("apply_demand_modules", None)
    if modules not in (None, ""):
        if isinstance(modules, list):
            out["apply_demand_module"] = modules
        else:
            out["apply_demand_module"] = [
                m.strip() for m in str(modules).replace("，", ",").split(",") if m.strip()
            ]
    return out


def _resolve_add_demand_pool(params: dict[str, Any], *, feedback: bool = False) -> dict[str, Any]:
    import api_client

    out = dict(params)
    a_id = out.pop("a_id", None) or out.pop("apply_demand_id", None)
    if a_id in (None, ""):
        raise api_client.ClientError("validation_error", "missing a_id / apply_demand_id")
    out["a_id"] = int(a_id)
    raw_params = out.pop("params", None)
    parsed = _parse_json_value(raw_params)
    if not isinstance(parsed, list) or not parsed:
        raise api_client.ClientError("validation_error", "params must be a non-empty JSON array")
    out["params"] = _resolve_demand_pool_params(parsed, feedback=feedback)
    return out


def _resolve_add_feedback_demand_pool(params: dict[str, Any]) -> dict[str, Any]:
    return _resolve_add_demand_pool(params, feedback=True)


def _resolve_approve_publish_apply(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = dict(params)
    if out.get("apply_publish_id") in (None, "") and out.get("id") not in (None, ""):
        out["apply_publish_id"] = int(out.pop("id"))
    elif out.get("apply_publish_id") not in (None, ""):
        out["apply_publish_id"] = int(out["apply_publish_id"])
    out.pop("publish_id", None)
    if not out.get("sj_num"):
        out = _resolve_project_apply_fields(out)
    else:
        out.pop("project_id", None)
        out.pop("project_name", None)
    if out.get("publish_submit_type") and not isinstance(out.get("publish_submit_type"), list):
        out["publish_submit_type"] = [
            x.strip() for x in str(out["publish_submit_type"]).replace("，", ",").split(",") if x.strip()
        ]
    if out.get("publish_dijiao_version") not in (None, ""):
        out["publish_dijiao_version"] = int(out["publish_dijiao_version"])
    if out.get("project_type") not in (None, ""):
        out["project_type"] = int(out["project_type"])
    if out.get("contain_logo") not in (None, ""):
        out["contain_logo"] = int(out["contain_logo"])
    if out.get("linshi_status") not in (None, ""):
        out["linshi_status"] = int(out["linshi_status"])
    pool = out.pop("selected_demand_pool_ids", None) or out.pop("demand_pool_id", None)
    if pool not in (None, ""):
        out["selected_demand_pool_ids"] = api_client._parse_int_list(pool)
    return out


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _split_person_names(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_split_person_names(item))
        return parts
    text = str(value).strip().lstrip("@").strip()
    if not text:
        return []
    out: list[str] = []
    for part in re.split(r"[,，;；、\n]+", text):
        name = part.strip().lstrip("@").strip()
        if name and name not in out:
            out.append(name)
    return out


def _resolve_person_ids(names: list[str]) -> tuple[list[int], list[str], list[str]]:
    """Return (ids, resolved_names_aligned_to_ids, unmatched_names)."""
    import api_client

    ids: list[int] = []
    resolved: list[str] = []
    unmatched: list[str] = []
    seen: set[int] = set()
    for name in names:
        clean = str(name or "").strip().lstrip("@").strip()
        if not clean:
            continue
        try:
            uid = int(api_client.resolve_user_id(clean))
        except api_client.ClientError as e:
            if e.error == "auth_required":
                raise
            unmatched.append(clean)
            continue
        except (TypeError, ValueError, OSError):
            unmatched.append(clean)
            continue
        if uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
        resolved.append(clean)
    return ids, resolved, unmatched


def _parse_todo_line(line: str) -> dict[str, Any] | None:
    text = str(line or "").strip()
    text = _NUMBERED_PREFIX_RE.sub("", text).strip()
    text = _TODO_LINE_PREFIX_RE.sub("", text).strip()
    if not text or text in {"待办", "待办事项", "后续动作", "后续安排", "行动项", "无", "无。"}:
        return None
    assignees = [n.strip().lstrip("@") for n in _AT_NAME_RE.findall(text) if n.strip()]
    body = _AT_NAME_RE.sub("", text)
    body = re.sub(r"（来自[^）]*）|\(来自[^)]*\)", "", body)
    body = re.sub(r"\s+", " ", body).strip(" ：:.-")
    prefix = _ASSIGNEE_PREFIX_RE.match(body)
    if prefix:
        head, rest = prefix.group(1).strip(), prefix.group(2).strip()
        # 「张三、李四：做XX」→ assignees + text
        head_names = _split_person_names(head)
        if head_names and len(head) <= 40 and rest:
            for n in head_names:
                if n not in assignees:
                    assignees.append(n)
            body = rest
    if not body:
        return None
    return {"text": body[:_MAX_TODO_TEXT], "assignees": assignees}


def _todos_from_plain_remark(remark: str) -> list[dict[str, Any]]:
    text = str(remark or "").strip()
    if not text:
        return []
    lines = re.split(r"\n+", text)
    # 单行但含 1、2、 编号时也拆开
    if len(lines) == 1 and re.search(r"(?:^|[\s])\d+[\.、\)]\s*\S", text):
        lines = re.split(r"(?=(?:^|[\s])\d+[\.、\)]\s*)", text)
    todos: list[dict[str, Any]] = []
    for raw in lines:
        item = _parse_todo_line(raw)
        if not item:
            continue
        todos.append(item)
        if len(todos) >= _MAX_TODOS:
            break
    return todos


def _normalize_meet_todos(raw: Any) -> list[dict[str, Any]]:
    """Normalize to frontend shape: [{text, userIds, userNames}]."""
    parsed = _parse_jsonish(raw)
    if isinstance(parsed, str):
        # 非 JSON：按多行/编号拆成多条待办
        draft = _todos_from_plain_remark(parsed)
    elif isinstance(parsed, list):
        draft = []
        for row in parsed[:_MAX_TODOS]:
            if isinstance(row, str):
                item = _parse_todo_line(row)
                if item:
                    draft.append(item)
                continue
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or row.get("content") or row.get("todo") or "").strip()
            assignees = (
                row.get("assignees")
                or row.get("user_names")
                or row.get("userNames")
                or row.get("owners")
                or []
            )
            user_ids_raw = row.get("userIds") or row.get("user_ids") or row.get("uids") or []
            names = _split_person_names(assignees)
            ids_from_row: list[int] = []
            if isinstance(user_ids_raw, list):
                for x in user_ids_raw:
                    try:
                        ids_from_row.append(int(x))
                    except (TypeError, ValueError):
                        pass
            elif user_ids_raw not in (None, ""):
                try:
                    ids_from_row = [int(x) for x in str(user_ids_raw).split(",") if str(x).strip()]
                except (TypeError, ValueError):
                    ids_from_row = []
            if not text and not names and not ids_from_row:
                continue
            draft.append({"text": text[:_MAX_TODO_TEXT], "assignees": names, "userIds": ids_from_row})
    else:
        return []

    out: list[dict[str, Any]] = []
    for item in draft:
        text = str(item.get("text") or "").strip()
        names = _split_person_names(item.get("assignees") or item.get("user_names") or item.get("userNames"))
        preset_ids = item.get("userIds") or item.get("user_ids") or []
        ids: list[int] = []
        if isinstance(preset_ids, list):
            for x in preset_ids:
                try:
                    ids.append(int(x))
                except (TypeError, ValueError):
                    pass
        resolved_ids, resolved_names, unmatched = _resolve_person_ids(names)
        # 合并预设 id 与按名解析 id
        merged_ids: list[int] = []
        seen: set[int] = set()
        id_to_name: dict[int, str] = {}
        for uid, name in zip(resolved_ids, resolved_names):
            id_to_name[uid] = name
        for uid in ids + resolved_ids:
            if uid in seen:
                continue
            seen.add(uid)
            merged_ids.append(uid)
        display_names = [id_to_name.get(uid, "") for uid in merged_ids]
        if not display_names and names and not merged_ids:
            display_names = list(names)
        if unmatched and not merged_ids:
            # 人名全未匹配时仍保留 text，userIds 空（与前端 AI 回填一致）
            pass
        if not text and not merged_ids:
            continue
        out.append(
            {
                "text": text,
                "userIds": merged_ids,
                "userNames": display_names,
            }
        )
    return out


def _serialize_meet_todos(todos: list[dict[str, Any]]) -> str:
    if not todos:
        return ""
    return json.dumps(todos, ensure_ascii=False)


def _collect_moment_user_ids(params: dict[str, Any], todo_items: list[dict[str, Any]] | None = None) -> list[int]:
    import api_client

    ids: list[int] = []
    seen: set[int] = set()

    def _add(uid: int) -> None:
        if uid in seen:
            return
        seen.add(uid)
        ids.append(uid)

    raw_ids = params.get("user_id")
    if raw_ids not in (None, "") and not isinstance(raw_ids, str):
        try:
            for uid in api_client._parse_int_list(raw_ids):
                _add(int(uid))
        except Exception:
            pass
    elif isinstance(raw_ids, str) and raw_ids.strip():
        # 可能是 "1,2" 或误传成姓名
        if re.fullmatch(r"[\d,\s]+", raw_ids.strip()):
            for uid in api_client._parse_int_list(raw_ids):
                _add(int(uid))

    name_blob = (
        params.get("user_names")
        or params.get("user_name")
        or params.get("attendees")
        or params.get("related_persons")
        or params.get("related_person")
        or params.get("related_users")
    )
    for uid in _resolve_person_ids(_split_person_names(name_blob))[0]:
        _add(uid)

    for todo in todo_items or []:
        for uid in todo.get("userIds") or []:
            try:
                _add(int(uid))
            except (TypeError, ValueError):
                pass
    return ids


def _apply_meet_todos_to_body(out: dict[str, Any]) -> list[str]:
    """For meet module: split todos into JSON remark; merge related persons into user_id."""
    warnings: list[str] = []
    raw_todos = out.pop("todos", None)
    raw_remark = out.get("remark")

    todo_items: list[dict[str, Any]] = []
    if raw_todos not in (None, ""):
        todo_items = _normalize_meet_todos(raw_todos)
    elif raw_remark not in (None, ""):
        parsed = _parse_jsonish(raw_remark)
        if isinstance(parsed, list):
            todo_items = _normalize_meet_todos(parsed)
        else:
            # 纯文本 remark：拆成多条（单条纯文本也包成一条，便于挂相关人）
            todo_items = _normalize_meet_todos(str(raw_remark))

    if todo_items:
        out["remark"] = _serialize_meet_todos(todo_items)
    elif raw_remark in (None, ""):
        out["remark"] = ""

    user_ids = _collect_moment_user_ids(out, todo_items)
    for key in (
        "user_names",
        "user_name",
        "attendees",
        "related_persons",
        "related_person",
        "related_users",
        "todos",
    ):
        out.pop(key, None)
    out["user_id"] = user_ids
    if not user_ids:
        warnings.append("未解析到参会/相关人员 user_id，请传 user_name/attendees 或在 todos[].assignees 写责任人")
    if todo_items and all(not t.get("userIds") for t in todo_items):
        warnings.append("待办项未匹配到责任人账号，请核对 todos[].assignees / user_names 昵称")
    return warnings


def _resolve_add_project_moment(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {
            "project_name": ("name",),
            "sj_num": ("opportunity_no", "business_no"),
        },
    )
    if not out.get("project_id"):
        out["project_id"] = int(api_client.resolve_project_id(out))
    for key in ("project_name", "name", "sj_num", "opportunity_no", "business_no"):
        out.pop(key, None)
    if "module" in out:
        mod = str(out["module"]).strip()
        out["module"] = _MOMENT_MODULE_MAP.get(mod, _MOMENT_MODULE_MAP.get(mod.lower(), mod))

    module = str(out.get("module") or "")
    if module == "meet":
        _apply_meet_todos_to_body(out)
    else:
        # 风险/问题：remark 为解决方案纯文本；相关人员 → user_id
        user_ids = _collect_moment_user_ids(out, None)
        for key in (
            "user_names",
            "user_name",
            "attendees",
            "related_persons",
            "related_person",
            "related_users",
            "todos",
        ):
            out.pop(key, None)
        out["user_id"] = user_ids

    if out.get("project_id") not in (None, ""):
        out["project_id"] = int(out["project_id"])
    return out


def _resolve_add_project_task_estimate(params: dict[str, Any]) -> dict[str, Any]:
    import api_client

    out = api_client._apply_field_aliases(
        params,
        {
            "date": ("work_date", "start_date"),
            "consumed": ("hours", "work_hours"),
            "remark": ("note", "description"),
            "task_id": ("id",),
        },
    )
    if not out.get("date"):
        out["date"] = date.today().isoformat()
    if out.get("consumed") not in (None, ""):
        out["consumed"] = float(out["consumed"])
    user_name = out.pop("user_name", None)
    if user_name and not out.get("user_id"):
        out["user_id"] = int(api_client.resolve_user_id(str(user_name)))
    if out.get("task_id") in (None, "") and out.get("id") not in (None, ""):
        out["task_id"] = int(out.pop("id"))
    elif out.get("task_id") not in (None, ""):
        out["task_id"] = int(out["task_id"])
        out.pop("id", None)
    if out.get("user_id") not in (None, ""):
        out["user_id"] = int(out["user_id"])
    return out


def _preview_confirm_work_hours(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "estimate_id": body.get("id"),
        "confirm_type": body.get("confirm_type"),
        "confirm_type_label": _CONFIRM_LABEL.get(str(body.get("confirm_type")), ""),
    }


def _warnings_add_moment(body: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    module = body.get("module")
    if module in ("risk", "problem") and not body.get("risk_level"):
        warnings.append("风险/问题类动态建议填写 risk_level（风险等级/影响程度）")
    if not body.get("user_id"):
        warnings.append("未指定 user_id/user_name，提交可能失败")
    if not body.get("type"):
        warnings.append("未指定 type（子类型），提交可能失败")
    if module == "meet":
        remark = body.get("remark")
        todos = _parse_jsonish(remark) if remark else []
        if isinstance(todos, list) and todos:
            if any(isinstance(t, dict) and not t.get("userIds") for t in todos):
                warnings.append("部分待办未挂相关人员，建议在 todos[].assignees 或 user_name 中补充")
        elif remark:
            warnings.append(
                "会议待办建议传 todos 数组（多条拆开），Skill 会序列化为 JSON remark；"
                "纯文本 remark 已尽量按行拆分"
            )
    return warnings


def _preview_add_moment(body: dict[str, Any]) -> dict[str, Any]:
    module = body.get("module", "")
    preview: dict[str, Any] = {
        "project_id": body.get("project_id"),
        "module": module,
        "module_label": _MODULE_LABEL.get(str(module), module),
        "type": body.get("type"),
        "content": body.get("content"),
        "remark": body.get("remark"),
        "risk_level": body.get("risk_level"),
        "user_id": body.get("user_id"),
    }
    if module == "meet":
        todos = _parse_jsonish(body.get("remark"))
        if isinstance(todos, list):
            preview["todo_count"] = len(todos)
            preview["todos"] = todos
    return preview


def _preview_add_project_estimate(body: dict[str, Any]) -> dict[str, Any]:
    preview = _preview_add_estimate(body)
    preview["work_hour_kind"] = "项目"
    preview["work_hour_kind_note"] = (
        "本操作为项目任务工时。若用户说的是非项目任务，请改用 add_not_project_estimate"
    )
    return preview


def _preview_add_not_project_estimate(body: dict[str, Any]) -> dict[str, Any]:
    preview = _preview_add_estimate(body)
    preview["work_hour_kind"] = "非项目"
    preview["work_hour_kind_note"] = (
        "本操作为非项目任务工时。若用户说的是项目任务，请改用 add_project_task_estimate"
    )
    return preview


def _warnings_add_project_estimate(body: dict[str, Any]) -> list[str]:
    return [
        "请确认是【项目任务】工时；非项目任务必须改用 add_not_project_estimate，禁止写入本接口"
    ]


def _warnings_add_not_project_estimate(body: dict[str, Any]) -> list[str]:
    return [
        "请确认是【非项目任务】工时；项目任务必须改用 add_project_task_estimate，禁止写入本接口"
    ]


def _preview_add_estimate(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": body.get("task_id"),
        "date": body.get("date"),
        "consumed": body.get("consumed"),
        "remark": body.get("remark"),
        "user_id": body.get("user_id"),
    }


_WRITE_CONTROL_PARAMS = [
    "dry_run",
    "confirm",
    "confirm_token",
    "force",
    "skip_confirm",
]

WRITE_OPERATIONS: dict[str, dict[str, Any]] = {
    "confirm_my_work_hours": {
        "description": "【写】确认工时花费（项目/非项目）",
        "method": "POST",
        "path": "/manage_api/main_panel/confirm_anything",
        "write": True,
        "group": "写操作",
        "params": [
            "id",
            "ids",
            "estimate_id",
            "work_hour_id",
            "confirm_type",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["id", "ids", "estimate_id", "work_hour_id"],
        "required": ["confirm_type"],
        "write_resolve_mode": "confirm_work_hours",
        "body_params": ["id", "confirm_type"],
        "body_required": ["id", "confirm_type"],
        "preview_summary": _preview_confirm_work_hours,
    },
    "add_project_moment": {
        "description": "【写】新增项目动态（会议/风险/问题）",
        "method": "POST",
        "path": "/manage_api/project_moment/add",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "project_name",
            "name",
            "sj_num",
            "opportunity_no",
            "business_no",
            "module",
            "meeting",
            "risk",
            "problem",
            "type",
            "content",
            "remark",
            "todos",
            "risk_level",
            "status",
            "done_time",
            "user_id",
            "user_name",
            "user_names",
            "attendees",
            "related_person",
            "related_persons",
            "related_users",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_any_project": True,
        "required": ["module", "content"],
        "write_resolve_mode": "add_project_moment",
        "body_params": [
            "project_id",
            "module",
            "type",
            "content",
            "remark",
            "risk_level",
            "status",
            "done_time",
            "user_id",
        ],
        "body_required": ["project_id", "module", "type", "content", "user_id"],
        "body_required_preview": ["project_id", "module", "content"],
        "preview_summary": _preview_add_moment,
        "preview_warnings": _warnings_add_moment,
    },
    "add_project_task_estimate": {
        "description": "【写】登记项目任务工时",
        "method": "POST",
        "path": "/manage_api/project_task_estimate/add",
        "write": True,
        "group": "写操作",
        "params": [
            "task_id",
            "id",
            "date",
            "work_date",
            "consumed",
            "hours",
            "work_hours",
            "remark",
            "note",
            "description",
            "user_id",
            "user_name",
            "risk_desc",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["task_id", "id"],
        "required": ["consumed", "remark"],
        "write_resolve_mode": "add_project_task_estimate",
        "body_params": [
            "task_id",
            "date",
            "consumed",
            "remark",
            "user_id",
            "risk_desc",
        ],
        "body_required": ["task_id", "date", "consumed", "remark"],
        "preview_summary": _preview_add_project_estimate,
        "preview_warnings": _warnings_add_project_estimate,
    },
    "confirm_user_work_hours": {
        "description": "【写】代确认用户某日工时",
        "method": "POST",
        "path": "/manage_api/data_export/estimate_hour_confirm_by_others",
        "write": True,
        "group": "写操作",
        "params": ["date", "user_id", "user_name", "remark", "dry_run", "confirm", "confirm_token", "force"],
        "required": ["remark"],
        "required_one_of": ["user_id", "user_name"],
        "write_resolve_mode": "confirm_user_work_hours",
        "body_params": ["date", "user_id", "remark"],
        "body_required": ["date", "user_id", "remark"],
        "preview_summary": lambda b: {"date": b.get("date"), "user_id": b.get("user_id"), "remark": b.get("remark")},
    },
    "approve_work_hour_batch": {
        "description": "【写】批量审核工时花费",
        "method": "POST",
        "path": "/manage_api/data_export/process_check_by_ids",
        "write": True,
        "group": "写操作",
        "params": [
            "module_name",
            "module_sort",
            "user_id",
            "user_name",
            "project_ids",
            "not_project_ids",
            "id_arr_one",
            "id_arr_two",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required": ["module_name", "module_sort"],
        "required_one_of": ["user_id", "user_name"],
        "write_resolve_mode": "approve_work_hour_batch",
        "body_params": ["module_name", "module_sort", "user_id", "id_arr_one", "id_arr_two"],
        "body_required": ["module_name", "module_sort", "user_id"],
        "preview_summary": lambda b: {
            "module_name": b.get("module_name"),
            "module_sort": b.get("module_sort"),
            "user_id": b.get("user_id"),
            "project_count": len(b.get("id_arr_one") or []),
            "not_project_count": len(b.get("id_arr_two") or []),
        },
        "preview_warnings": lambda b: (
            ["project_ids 与 not_project_ids 至少填一组"]
            if not (b.get("id_arr_one") or b.get("id_arr_two"))
            else []
        ),
    },
    "add_not_project_estimate": {
        "description": "【写】登记非项目任务工时",
        "method": "POST",
        "path": "/manage_api/project_not_task_estimate/add",
        "write": True,
        "group": "写操作",
        "params": [
            "task_id",
            "id",
            "date",
            "work_date",
            "consumed",
            "hours",
            "remark",
            "user_id",
            "user_name",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["task_id", "id"],
        "required": ["consumed", "remark"],
        "write_resolve_mode": "add_not_project_estimate",
        "body_params": ["task_id", "date", "consumed", "remark", "user_id"],
        "body_required": ["task_id", "date", "consumed", "remark"],
        "preview_summary": _preview_add_not_project_estimate,
        "preview_warnings": _warnings_add_not_project_estimate,
    },
    "update_project_task_estimate": {
        "description": "【写】更新项目任务工时",
        "method": "PUT",
        "path": "/manage_api/project_task_estimate/update",
        "write": True,
        "group": "写操作",
        "params": [
            "id",
            "estimate_id",
            "work_hour_id",
            "date",
            "consumed",
            "remark",
            "risk_desc",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["id", "estimate_id", "work_hour_id"],
        "required": ["consumed", "remark"],
        "write_resolve_mode": "update_project_task_estimate",
        "body_params": ["id", "date", "consumed", "remark", "risk_desc"],
        "body_required": ["id", "consumed", "remark"],
        "preview_summary": lambda b: {
            "estimate_id": b.get("id"),
            "date": b.get("date"),
            "consumed": b.get("consumed"),
            "remark": b.get("remark"),
        },
    },
    "update_project_moment": {
        "description": "【写】更新项目动态",
        "method": "PUT",
        "path": "/manage_api/project_moment/update",
        "write": True,
        "group": "写操作",
        "params": [
            "id",
            "moment_id",
            "module",
            "type",
            "content",
            "remark",
            "todos",
            "risk_level",
            "status",
            "done_time",
            "user_id",
            "user_name",
            "user_names",
            "attendees",
            "related_person",
            "related_persons",
            "related_users",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["id", "moment_id"],
        "required": ["content", "type"],
        "write_resolve_mode": "update_project_moment",
        "body_params": ["id", "type", "content", "remark", "risk_level", "status", "done_time", "user_id"],
        "body_required": ["id", "type", "content", "user_id"],
        "body_required_preview": ["id", "content"],
        "preview_summary": _preview_add_moment,
        "preview_warnings": _warnings_add_moment,
    },
    "add_bug": {
        "description": "【写】登记 BUG",
        "method": "POST",
        "path": "/manage_api/bug/add",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "project_name",
            "sj_num",
            "task_id",
            "submit_time",
            "bug_type",
            "bug_status",
            "bug_level",
            "priority",
            "tester",
            "tester_name",
            "assigned_to",
            "assigned_to_name",
            "assignee_name",
            "content",
            "remark",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_any_project": True,
        "required": ["bug_type", "bug_status", "bug_level", "priority", "content"],
        "write_resolve_mode": "add_bug",
        "body_params": [
            "project_id",
            "task_id",
            "submit_time",
            "bug_type",
            "bug_status",
            "bug_level",
            "priority",
            "tester",
            "assigned_to",
            "content",
            "remark",
        ],
        "body_required": [
            "project_id",
            "submit_time",
            "bug_type",
            "bug_status",
            "bug_level",
            "priority",
            "tester",
            "assigned_to",
            "content",
        ],
        "body_required_preview": ["project_id", "content", "bug_type"],
        "preview_summary": lambda b: {
            "project_id": b.get("project_id"),
            "content": b.get("content"),
            "bug_type": b.get("bug_type"),
            "assigned_to": b.get("assigned_to"),
        },
        "preview_warnings": lambda b: [
            w
            for w, missing in (
                ("未指定 tester/tester_name", not b.get("tester")),
                ("未指定 assigned_to/assigned_to_name", not b.get("assigned_to")),
            )
            if missing
        ],
    },
    "apply_publish": {
        "description": "【写】申请项目递交",
        "method": "POST",
        "path": "/manage_api/produce_demand/apply_publish",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "project_name",
            "sj_num",
            "apply_publish_content",
            "apply_publish_time",
            "demand_pool_id",
            "demand_pool_ids",
            "publish_dijiao_version",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_any_project": True,
        "required": ["apply_publish_content", "apply_publish_time"],
        "write_resolve_mode": "apply_publish",
        "body_params": [
            "sj_num",
            "project_name",
            "project_id",
            "apply_publish_content",
            "apply_publish_time",
            "demand_pool_id",
            "publish_dijiao_version",
        ],
        "body_required": [
            "sj_num",
            "project_name",
            "project_id",
            "apply_publish_content",
            "apply_publish_time",
        ],
        "preview_summary": lambda b: {
            "sj_num": b.get("sj_num"),
            "project_name": b.get("project_name"),
            "apply_publish_time": b.get("apply_publish_time"),
            "content": b.get("apply_publish_content"),
        },
    },
    "reject_publish_apply": {
        "description": "【写】驳回递交申请",
        "method": "POST",
        "path": "/manage_api/produce_demand/reject_publish",
        "write": True,
        "group": "写操作",
        "params": ["id", "publish_id", "reject_reason", "dry_run", "confirm", "confirm_token", "force"],
        "required_one_of": ["id", "publish_id"],
        "required": ["reject_reason"],
        "write_resolve_mode": "reject_publish",
        "body_params": ["id", "reject_reason"],
        "body_required": ["id", "reject_reason"],
        "preview_summary": lambda b: {"apply_id": b.get("id"), "reject_reason": b.get("reject_reason")},
    },
    "start_project_task": {
        "description": "【写】开始项目任务",
        "method": "POST",
        "path": "/manage_api/project_task/start",
        "write": True,
        "group": "写操作",
        "params": ["id", "task_id", "remark", "assigned_to", "assigned_to_name", "dry_run", "confirm", "confirm_token", "force"],
        "required_one_of": ["id", "task_id"],
        "required": ["remark"],
        "write_resolve_mode": "task_action",
        "body_params": ["id", "remark", "assigned_to"],
        "body_required": ["id", "remark"],
        "preview_summary": lambda b: {"task_id": b.get("id"), "remark": b.get("remark")},
    },
    "finish_project_task": {
        "description": "【写】完成项目任务",
        "method": "POST",
        "path": "/manage_api/project_task/finish",
        "write": True,
        "group": "写操作",
        "params": ["id", "task_id", "remark", "dry_run", "confirm", "confirm_token", "force"],
        "required_one_of": ["id", "task_id"],
        "required": ["remark"],
        "write_resolve_mode": "task_action",
        "body_params": ["id", "remark"],
        "body_required": ["id", "remark"],
        "preview_summary": lambda b: {"task_id": b.get("id"), "remark": b.get("remark")},
    },
    "save_project_overview_value": {
        "description": "【写】保存项目价值（概览）",
        "method": "POST",
        "path": "/manage_api/project_overview/save_value",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "project_name",
            "sj_num",
            "rows",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_any_project": True,
        "required": ["rows"],
        "write_resolve_mode": "save_project_overview_value",
        "body_params": ["project_id", "rows"],
        "body_required": ["project_id", "rows"],
        "preview_summary": lambda b: {
            "project_id": b.get("project_id"),
            "row_count": len(b.get("rows") or []) if isinstance(b.get("rows"), list) else 0,
        },
        "preview_warnings": lambda b: (
            ["rows 需为 JSON 数组；可先 get_project_overview_value 查看现有内容"]
            if not isinstance(b.get("rows"), list)
            else []
        ),
    },
    "add_not_project_demand": {
        "description": "【写】新增非项目需求",
        "method": "POST",
        "path": "/manage_api/project_not_task/add",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "not_project_id",
            "not_project_name",
            "name",
            "protype",
            "status",
            "plan_hour",
            "assigned_to",
            "assigned_to_names",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required": ["name"],
        "required_one_of": ["project_id", "not_project_id", "not_project_name", "name"],
        "write_resolve_mode": "add_not_project_demand",
        "body_params": ["project_id", "name", "desc", "status", "plan_hour", "assigned_to", "protype", "one_type", "pid"],
        "body_required": ["project_id", "name"],
        "body_required_preview": ["project_id", "name"],
        "preview_summary": lambda b: {
            "not_project_id": b.get("project_id"),
            "name": b.get("name"),
            "assigned_to": b.get("assigned_to"),
        },
    },
    "add_not_project_task": {
        "description": "【写】新增非项目任务（须选任务选项并按指派人组优先；未传指派人默认当前用户；未传产出类型默认非产出）",
        "method": "POST",
        "path": "/manage_api/project_not_task/add_task",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "not_project_id",
            "not_project_name",
            "pid",
            "demand_id",
            "name",
            "desc",
            "start_date",
            "end_date",
            "plan_hour",
            "assigned_to",
            "assignee_name",
            "user_name",
            "one_type",
            "output_type",
            "estimate_type",
            "option_ids",
            "task_options",
            "option_names",
            "status",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required": ["name", "start_date", "end_date", "plan_hour"],
        "required_one_of": ["project_id", "not_project_id", "not_project_name"],
        "write_resolve_mode": "add_not_project_task",
        "body_params": [
            "project_id",
            "pid",
            "name",
            "desc",
            "status",
            "assigned_to",
            "start_date",
            "end_date",
            "plan_hour",
            "one_type",
            "estimate_type",
            "option_ids",
        ],
        "body_required": ["project_id", "pid", "name", "start_date", "end_date", "plan_hour"],
        "preview_summary": lambda b: {
            "not_project_id": b.get("project_id"),
            "demand_id": b.get("pid"),
            "name": b.get("name"),
            "plan_hour": b.get("plan_hour"),
            "status": b.get("status"),
            "assigned_to": b.get("assigned_to"),
            "one_type": b.get("one_type"),
            "one_type_label": {3: "非产出", 4: "产出", 0: "同上"}.get(
                int(b["one_type"]) if b.get("one_type") not in (None, "") else -1,
                b.get("one_type"),
            ),
            "option_ids": b.get("option_ids"),
        },
        "preview_warnings": lambda b: [
            msg
            for msg, cond in (
                (
                    "未指定指派人，请传 assignee_name/user_name，或确认登录用户可作为默认指派人",
                    not b.get("assigned_to"),
                ),
                (
                    "未选择任务选项 option_ids/task_options（请假类非项目除外）",
                    not b.get("option_ids") and int(b.get("project_id") or 0) != 1496,
                ),
            )
            if cond
        ],
    },
    "finish_not_project_task": {
        "description": "【写】完成非项目任务",
        "method": "POST",
        "path": "/manage_api/project_not_task/finish_task",
        "write": True,
        "group": "写操作",
        "params": ["id", "task_id", "dry_run", "confirm", "confirm_token", "force"],
        "required_one_of": ["id", "task_id"],
        "write_resolve_mode": "finish_not_project_task",
        "body_params": ["id"],
        "body_required": ["id"],
        "preview_summary": lambda b: {"task_id": b.get("id")},
    },
    "update_not_project_estimate": {
        "description": "【写】更新非项目任务工时",
        "method": "PUT",
        "path": "/manage_api/project_not_task_estimate/update",
        "write": True,
        "group": "写操作",
        "params": [
            "id",
            "estimate_id",
            "date",
            "consumed",
            "remark",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["id", "estimate_id"],
        "required": ["consumed", "remark"],
        "write_resolve_mode": "update_not_project_estimate",
        "body_params": ["id", "date", "consumed", "remark"],
        "body_required": ["id", "consumed", "remark"],
        "preview_summary": lambda b: {
            "estimate_id": b.get("id"),
            "consumed": b.get("consumed"),
            "remark": b.get("remark"),
        },
    },
    "add_apply_demand": {
        "description": "【写】新增申请制作需求",
        "method": "POST",
        "path": "/manage_api/produce_demand/add_apply_demand",
        "write": True,
        "group": "写操作",
        "params": [
            "project_id",
            "project_name",
            "sj_num",
            "apply_demand_module",
            "apply_demand_modules",
            "apply_demand_desc",
            "apply_material_address",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_any_project": True,
        "required": ["apply_demand_module", "apply_demand_desc"],
        "write_resolve_mode": "add_apply_demand",
        "body_params": [
            "sj_num",
            "project_name",
            "project_id",
            "apply_demand_module",
            "apply_demand_desc",
            "apply_material_address",
            "demand_url",
        ],
        "body_required": ["sj_num", "project_name", "project_id", "apply_demand_module", "apply_demand_desc"],
        "preview_summary": lambda b: {
            "sj_num": b.get("sj_num"),
            "project_name": b.get("project_name"),
            "modules": b.get("apply_demand_module"),
            "desc": b.get("apply_demand_desc"),
        },
        "preview_warnings": lambda b: (
            ["可先调用 get_apply_demand_consts 查看模块/类型字典"] if not b.get("apply_demand_module") else []
        ),
    },
    "add_demand_pool": {
        "description": "【写】拆解新增制作需求池",
        "method": "POST",
        "path": "/manage_api/produce_demand/add_demand_pool",
        "write": True,
        "group": "写操作",
        "params": [
            "a_id",
            "apply_demand_id",
            "params",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["a_id", "apply_demand_id"],
        "required": ["params"],
        "write_resolve_mode": "add_demand_pool",
        "body_params": ["a_id", "params"],
        "body_required": ["a_id", "params"],
        "preview_summary": lambda b: {
            "apply_demand_id": b.get("a_id"),
            "item_count": len(b.get("params") or []),
        },
        "preview_warnings": lambda b: (
            ["params 需为 JSON 数组，每项含 demand_type/demand_module/cost/executor_name 等"]
            if not isinstance(b.get("params"), list)
            else []
        ),
    },
    "add_feedback_demand_pool": {
        "description": "【写】新增反馈类制作需求池（demand_type=反馈）",
        "method": "POST",
        "path": "/manage_api/produce_demand/add_demand_pool",
        "write": True,
        "group": "写操作",
        "params": [
            "a_id",
            "apply_demand_id",
            "params",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["a_id", "apply_demand_id"],
        "required": ["params"],
        "write_resolve_mode": "add_feedback_demand_pool",
        "body_params": ["a_id", "params"],
        "body_required": ["a_id", "params"],
        "preview_summary": lambda b: {
            "apply_demand_id": b.get("a_id"),
            "feedback_item_count": len(b.get("params") or []),
        },
    },
    "approve_publish_apply": {
        "description": "【写】通过递交申请并排期（创建正式递交，状态→已排期）",
        "method": "POST",
        "path": "/manage_api/project_publish/add",
        "write": True,
        "group": "写操作",
        "params": [
            "apply_publish_id",
            "id",
            "publish_id",
            "sj_num",
            "project_name",
            "publish_content",
            "pm_name",
            "project_develop",
            "scene_group",
            "publish_submit_type",
            "publish_dijiao_version",
            "real_publish_people",
            "selected_demand_pool_ids",
            "demand_pool_id",
            "dry_run",
            "confirm",
            "confirm_token",
            "force",
        ],
        "required_one_of": ["apply_publish_id", "id"],
        "required": ["publish_content"],
        "write_resolve_mode": "approve_publish_apply",
        "body_params": [
            "apply_publish_id",
            "sj_num",
            "publish_content",
            "pm_name",
            "project_develop",
            "scene_group",
            "project_area",
            "project_type",
            "publish_submit_type",
            "publish_dijiao_version",
            "real_publish_people",
            "contain_logo",
            "basic_version",
            "wdp_api_version",
            "industry_plugins",
            "engine_version",
            "linshi_status",
            "linshi_duty",
            "linshi_reason",
            "regional_organization",
            "publish_url",
            "selected_demand_pool_ids",
        ],
        "body_required": ["apply_publish_id", "sj_num", "publish_content"],
        "body_required_preview": ["apply_publish_id", "publish_content"],
        "preview_summary": lambda b: {
            "apply_publish_id": b.get("apply_publish_id"),
            "sj_num": b.get("sj_num"),
            "publish_content": b.get("publish_content"),
        },
        "preview_warnings": lambda b: [
            w
            for w, missing in (
                ("未指定 sj_num/project_name，确认前需补全", not b.get("sj_num")),
                ("可先 get_publish_normal_const 查递交版本/交付形式字典", not b.get("publish_dijiao_version")),
            )
            if missing
        ],
    },
}

WRITE_RESOLVE_MODES = frozenset(
    {
        "confirm_work_hours",
        "add_project_moment",
        "add_project_task_estimate",
        "confirm_user_work_hours",
        "approve_work_hour_batch",
        "add_not_project_estimate",
        "update_project_task_estimate",
        "update_project_moment",
        "add_bug",
        "apply_publish",
        "reject_publish",
        "task_action",
        "save_project_overview_value",
        "add_not_project_demand",
        "add_not_project_task",
        "finish_not_project_task",
        "update_not_project_estimate",
        "add_apply_demand",
        "add_demand_pool",
        "add_feedback_demand_pool",
        "approve_publish_apply",
    }
)
