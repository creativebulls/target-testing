#!/usr/bin/env python3
"""Fetch Adobe Target activities and offers via MCP for ATGitOps Helper."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from activity_discovery import load_config  # noqa: E402
from create_activity import to_snake_case  # noqa: E402
from deploy_to_target_mcp import (  # noqa: E402
    McpClient,
    extract_named_items,
    extract_tool_result,
    resolve_access_token,
)

ACTIVITY_GET_TOOLS = {
    "ab": "get_ab_activity",
    "xt": "get_xt_activity",
    "abt": "get_abt_activity",
    "ap": "get_abt_activity",
}


from mcp_config import apply_mcp_connection_settings  # noqa: E402


def connect_client() -> McpClient:
    config = apply_mcp_connection_settings(load_config())
    mcp_url = config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp")
    token = resolve_access_token(config)
    client = McpClient(mcp_url, token)
    client.initialize()
    return client


def normalize_activity_type(activity_type: str | None) -> str:
    value = (activity_type or "xt").lower()
    if value in {"ab", "xt", "abt", "ap"}:
        return "abt" if value == "ap" else value
    return "xt"


def list_target_activities(
    *,
    limit: int = 200,
    offset: int | None = None,
    sort_by: str | None = None,
    state: str | None = None,
    activity_type: str | None = None,
    name_contains: str | None = None,
    starts_after: str | None = None,
    starts_before: str | None = None,
    modified_after: str | None = None,
    ends_after: str | None = None,
    ends_before: str | None = None,
    workspace: str | None = None,
    segment_id: str | None = None,
    profile_attribute_id: str | None = None,
    priority: int | None = None,
    mbox: str | None = None,
    offer_id: str | None = None,
    view_id: str | None = None,
) -> list[dict]:
    """List Adobe Target activities with server-side filtering + sorting.

    Every filter is passed straight through to the MCP `list_target_activities`
    tool — the Adobe Admin API does the filter and sort work, so the response
    is already the exact page the caller wants (up to `limit`, MCP-capped at
    200 per page).
    """
    client = connect_client()
    params: dict = {"limit": limit}
    if offset is not None:
        params["offset"] = offset
    if sort_by:
        params["sort_by"] = sort_by
    if state:
        params["state"] = state
    if activity_type:
        params["activity_type"] = normalize_activity_type(activity_type)
    if name_contains:
        params["name_contains"] = name_contains
    if starts_after:
        params["starts_after"] = starts_after
    if starts_before:
        params["starts_before"] = starts_before
    if modified_after:
        params["modified_after"] = modified_after
    if ends_after:
        params["ends_after"] = ends_after
    if ends_before:
        params["ends_before"] = ends_before
    if workspace:
        params["workspace"] = workspace
    if segment_id:
        params["segment_id"] = segment_id
    if profile_attribute_id:
        params["profile_attribute_id"] = profile_attribute_id
    if priority is not None:
        params["priority"] = int(priority)
    if mbox:
        params["mbox"] = mbox
    if offer_id:
        params["offer_id"] = offer_id
    if view_id:
        params["view_id"] = view_id

    response = client.call_tool("list_target_activities", params)
    result = extract_tool_result(response)
    return extract_named_items(result, "activities")


def list_target_workspaces() -> list[dict]:
    """Every Adobe Target workspace this user has access to.

    Composer's workspace picker uses this — workspaces determine where a
    newly-created activity lives in Target Admin. Not every tenant has
    multiple workspaces; single-tenant orgs return one 'default' workspace.
    """
    client = connect_client()
    response = client.call_tool("list_target_workspaces", {})
    result = extract_tool_result(response)
    return (
        extract_named_items(result, "workspaces")
        or extract_named_items(result, "items")
    )


def get_activity_tool_name(activity_type: str) -> str:
    normalized = normalize_activity_type(activity_type)
    return ACTIVITY_GET_TOOLS[normalized]


def get_target_activity(activity_id: int, activity_type: str) -> dict:
    client = connect_client()
    tool_name = get_activity_tool_name(activity_type)
    response = client.call_tool(tool_name, {"activity_id": activity_id})
    return extract_tool_result(response)


def get_target_offer(offer_id: int) -> dict:
    client = connect_client()
    response = client.call_tool("get_target_offer", {"offer_id": offer_id})
    return extract_tool_result(response)


def deactivate_target_activity(activity_id: int) -> dict:
    """Soft-delete in Target: Adobe MCP has no hard-delete tool, only state change."""
    client = connect_client()
    response = client.call_tool(
        "update_activity_state",
        {"activity_id": int(activity_id), "state": "deactivated"},
    )
    return {
        "activity_id": int(activity_id),
        "state": "deactivated",
        "result": extract_tool_result(response),
    }


def preview_target_activity(
    activity_id: int,
    activity_type: str | None = None,
    url: str | None = None,
) -> dict:
    """Generate QA preview URLs that bypass audience targeting.

    Wraps Adobe Target MCP's `preview_activity` tool. The tool returns one
    preview URL per experience so QA can view each variant directly without
    matching the activity's normal audience rules. `activity_type` is passed
    through when known — some tool versions require it, others ignore it.

    Form-based (non-VEC) activities require a `url` — the page URL where the
    mbox is deployed. Without it the tool returns an empty `preview_urls` and
    a `message` telling the caller to provide `url`. We pass through the
    caller-supplied page URL when available so QA links resolve on the first
    call. VEC activities are unaffected by an extra `url` param.

    Returns the raw MCP payload; the API layer is responsible for extracting
    a normalized list of `{experience, url}` pairs (schema has varied
    across MCP releases).
    """
    client = connect_client()
    params: dict = {"activity_id": int(activity_id)}
    if activity_type:
        params["activity_type"] = normalize_activity_type(activity_type)
    if url:
        params["url"] = url
    response = client.call_tool("preview_activity", params)
    return extract_tool_result(response)


def _extract_id(result: dict) -> int | None:
    for key in ("id", "activity_id", "offer_id"):
        value = result.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _even_traffic(count: int) -> list[int]:
    if count <= 0:
        return []
    if count == 1:
        return [100]
    base = 100 // count
    remainder = 100 - base * count
    return [base + (1 if i < remainder else 0) for i in range(count)]


def _normalize_experiences(payload: dict, *, activity_type: str, activity_name: str) -> list[dict]:
    """Normalize experiences list from payload (new) or legacy A/B fields."""
    split = activity_type in {"ab", "abt"}
    raw = payload.get("experiences")
    items: list[dict] = []

    if isinstance(raw, list) and raw:
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            letter = chr(65 + min(index, 25))
            if split or index > 0:
                default_name = (
                    f"Experience {letter} - {activity_name}"
                    if activity_name
                    else f"Experience {letter}"
                )
            else:
                default_name = (
                    f"Experience - {activity_name}" if activity_name else "Experience"
                )
            exp_name = str(item.get("experience_name") or default_name).strip()
            offer_name = str(item.get("offer_name") or exp_name).strip()
            try:
                traffic = int(item.get("traffic_percent", 0))
            except (TypeError, ValueError):
                traffic = 0
            items.append(
                {
                    "experience_name": exp_name,
                    "offer_name": offer_name,
                    "traffic_percent": max(0, min(100, traffic)),
                }
            )
    else:
        # Legacy 2-slot payload
        experience_a_name = str(
            payload.get("experience_a_name")
            or (f"Experience A - {activity_name}" if split else f"Experience - {activity_name}")
        ).strip()
        offer_name = str(payload.get("offer_name") or experience_a_name).strip()
        items.append(
            {
                "experience_name": experience_a_name,
                "offer_name": offer_name,
                "traffic_percent": 50 if split else 0,
            }
        )
        if split or payload.get("offer_b_name") or payload.get("experience_b_name"):
            experience_b_name = str(
                payload.get("experience_b_name") or f"Experience B - {activity_name}"
            ).strip()
            offer_b_name = str(
                payload.get("offer_b_name") or experience_b_name
            ).strip()
            try:
                traffic_a = int(payload.get("traffic_a_percent", 50))
            except (TypeError, ValueError):
                traffic_a = 50
            try:
                traffic_b = int(payload.get("traffic_b_percent", 100 - traffic_a))
            except (TypeError, ValueError):
                traffic_b = 100 - traffic_a
            items[0]["traffic_percent"] = max(0, min(100, traffic_a))
            items.append(
                {
                    "experience_name": experience_b_name,
                    "offer_name": offer_b_name,
                    "traffic_percent": max(0, min(100, traffic_b)),
                }
            )

    if not items:
        items = [
            {
                "experience_name": f"Experience - {activity_name}" if activity_name else "Experience",
                "offer_name": f"Experience - {activity_name}" if activity_name else "Experience",
                "traffic_percent": 100 if split else 0,
            }
        ]

    # AB/ABT require at least 2 experiences
    if split and len(items) < 2:
        letter = chr(65 + len(items))
        items.append(
            {
                "experience_name": f"Experience {letter} - {activity_name}"
                if activity_name
                else f"Experience {letter}",
                "offer_name": f"Experience {letter} - {activity_name}"
                if activity_name
                else f"Experience {letter}",
                "traffic_percent": 0,
            }
        )

    if split:
        total = sum(int(item["traffic_percent"]) for item in items)
        if total != 100:
            even = _even_traffic(len(items))
            for index, item in enumerate(items):
                item["traffic_percent"] = even[index]
    else:
        for item in items:
            item["traffic_percent"] = 0

    return items


def _build_create_activity_params(
    *,
    activity_type: str,
    activity_name: str,
    mbox_name: str,
    experiences: list[dict],
    description: str,
    starts_at: str,
    ends_at: str,
    workspace_id: str | None = None,
    include_offer_ids: bool = True,
    include_visitor_percentage: bool = True,
) -> dict:
    """Build MCP create_*_activity params for the live schema.

    Current Adobe Target MCP validation for A/B requires `variants`
    (Pydantic create_ab_activityArguments). XT still accepts experiences/locations.
    """
    variants: list[dict] = []
    for index, item in enumerate(experiences):
        letter = chr(65 + min(index, 25))
        entry: dict = {
            "name": item.get("experience_name") or f"Experience {letter}",
        }
        if (
            include_visitor_percentage
            and activity_type in {"ab", "abt"}
        ):
            entry["visitorPercentage"] = int(item.get("traffic_percent") or 0)
        offer_id = item.get("offer_id")
        if include_offer_ids and offer_id:
            entry["offerId"] = int(offer_id)
        variants.append(entry)

    params: dict = {
        "name": activity_name,
        # saved = draft/inactive-friendly; composer then deactivates when allowed
        "state": "saved",
        "locations": [{"name": mbox_name}],
        "experiences": variants,
        "variants": variants,
    }

    if description:
        params["description"] = description
    if starts_at:
        params["starts_at"] = starts_at
    if ends_at:
        params["ends_at"] = ends_at
    if workspace_id:
        params["workspace_id"] = str(workspace_id)

    return params


def update_traffic_split(
    activity_id: int,
    *,
    activity_type: str,
    splits: dict,
) -> dict:
    """Update AB/ABT traffic allocation via MCP update_traffic_split."""
    client = connect_client()
    normalized_type = normalize_activity_type(activity_type)
    if normalized_type not in {"ab", "abt"}:
        raise ValueError("Traffic split is only supported for AB / ABT activities.")
    if not isinstance(splits, dict) or not splits:
        raise ValueError("splits must map experience names to percentages.")
    total = 0
    clean: dict[str, int] = {}
    for name, pct in splits.items():
        key = str(name).strip()
        value = int(pct)
        if not key:
            continue
        clean[key] = value
        total += value
    if total != 100:
        raise ValueError(f"Traffic split percentages must sum to 100 (got {total}).")
    response = client.call_tool(
        "update_traffic_split",
        {
            "activity_id": int(activity_id),
            "activity_type": normalized_type,
            "splits": clean,
        },
    )
    return extract_tool_result(response)


def _create_offer(client, *, name: str, html_content: str) -> tuple[int, dict]:
    offer_response = client.call_tool(
        "create_target_offer",
        {
            "name": name,
            "content": html_content,
        },
    )
    offer_result = extract_tool_result(offer_response)
    if isinstance(offer_result, dict) and offer_result.get("raw"):
        raise RuntimeError(
            f"Target create offer failed: {offer_result.get('raw')}"
        )
    offer_id = _extract_id(offer_result)
    if not offer_id:
        raise RuntimeError(
            f"Target create offer returned no offer id: {json.dumps(offer_result)[:500]}"
        )
    return offer_id, offer_result


def create_draft_activity(payload: dict) -> dict:
    """Create inactive Target activity + HTML offer(s) for the composer flow."""
    client = connect_client()
    activity_type = normalize_activity_type(payload.get("activity_type", "xt"))
    tool_name = f"create_{activity_type}_activity"
    mbox_name = (payload.get("mbox_name") or "target-global-mbox").strip()
    page_url = (payload.get("page_url") or payload.get("activity_location") or "").strip()
    activity_name = (payload.get("activity_name") or "").strip()
    split = activity_type in {"ab", "abt"}
    html_content = payload.get("html_content") or "<section><h2>New offer</h2></section>"

    if not activity_name:
        raise ValueError("activity_name is required")

    experiences = _normalize_experiences(
        payload, activity_type=activity_type, activity_name=activity_name
    )

    # Create one offer per experience before activity so variants can reference them.
    offer_results: list[dict] = []
    for item in experiences:
        offer_id, offer_result = _create_offer(
            client, name=item["offer_name"], html_content=html_content
        )
        item["offer_id"] = offer_id
        offer_results.append(offer_result)

    create_kwargs = dict(
        activity_type=activity_type,
        activity_name=activity_name,
        mbox_name=mbox_name,
        experiences=experiences,
        description=(payload.get("activity_description") or "").strip(),
        starts_at=(payload.get("activity_start_date") or "").strip(),
        ends_at=(payload.get("activity_end_date") or "").strip(),
        workspace_id=(str(payload.get("workspace_id") or "").strip() or None),
    )
    create_params = _build_create_activity_params(**create_kwargs)

    activity_response = client.call_tool(tool_name, create_params)
    activity_result = extract_tool_result(activity_response)

    # Retry once without offerId / visitorPercentage if schema rejects unknown fields.
    if isinstance(activity_result, dict) and activity_result.get("raw"):
        raw = str(activity_result.get("raw"))
        if (
            "offerId" in raw
            or "visitorPercentage" in raw
            or "validation error" in raw.lower()
        ):
            create_params = _build_create_activity_params(
                **create_kwargs,
                include_offer_ids=False,
                include_visitor_percentage="visitorPercentage" not in raw,
            )
            if "visitorPercentage" in raw:
                for item in create_params.get("experiences", []):
                    item.pop("visitorPercentage", None)
                for item in create_params.get("variants", []):
                    item.pop("visitorPercentage", None)
            activity_response = client.call_tool(tool_name, create_params)
            activity_result = extract_tool_result(activity_response)

    if isinstance(activity_result, dict) and activity_result.get("raw"):
        raise RuntimeError(
            f"Target create activity failed: {activity_result.get('raw')}"
        )

    activity_id = _extract_id(activity_result)
    if not activity_id:
        raise RuntimeError(
            f"Target create returned no activity id: {json.dumps(activity_result)[:500]}"
        )

    # Keep draft inactive for the composer flow.
    try:
        client.call_tool(
            "update_activity_state",
            {"activity_id": activity_id, "state": "deactivated"},
        )
    except Exception:
        pass

    # Attach offers to experiences (best-effort by configured names + common fallbacks).
    for index, item in enumerate(experiences):
        experience_name = item["experience_name"]
        oid = int(item["offer_id"])
        letter = chr(65 + min(index, 25))
        candidates = [experience_name, f"Experience {letter}", f"variant_{letter.lower()}"]
        if index == 0:
            candidates.extend(["Experience A", "Control", "variant_a"])
        if index == 1:
            candidates.extend(["Experience B", "variant_b"])
        for candidate in candidates:
            try:
                client.call_tool(
                    "update_variant_offer",
                    {
                        "activity_id": activity_id,
                        "experience_name": candidate,
                        "offer_id": oid,
                    },
                )
                break
            except Exception:
                continue

    traffic_split_result = None
    if split:
        try:
            traffic_split_result = update_traffic_split(
                activity_id,
                activity_type=activity_type,
                splits={
                    item["experience_name"]: int(item["traffic_percent"])
                    for item in experiences
                },
            )
        except Exception as error:
            traffic_split_result = {"error": str(error)}

    offer_id = int(experiences[0]["offer_id"])
    offer_b_id = int(experiences[1]["offer_id"]) if len(experiences) > 1 else None

    return {
        "activity_id": activity_id,
        "offer_id": offer_id,
        "offer_b_id": offer_b_id,
        "activity_name": activity_name,
        "offer_name": experiences[0]["offer_name"],
        "offer_b_name": experiences[1]["offer_name"] if len(experiences) > 1 else "",
        "experience_a_name": experiences[0]["experience_name"],
        "experience_b_name": experiences[1]["experience_name"] if len(experiences) > 1 else "",
        "traffic_a_percent": int(experiences[0]["traffic_percent"]) if split else 100,
        "traffic_b_percent": (
            int(experiences[1]["traffic_percent"]) if split and len(experiences) > 1 else 0
        ),
        "experiences": [
            {
                "experience_name": item["experience_name"],
                "offer_name": item["offer_name"],
                "offer_id": int(item["offer_id"]),
                "traffic_percent": int(item["traffic_percent"]) if split else 0,
            }
            for item in experiences
        ],
        "activity_type": activity_type,
        "state": "deactivated",
        "mbox_name": mbox_name,
        "page_url": page_url,
        "activity": activity_result,
        "offer": offer_results[0] if offer_results else None,
        "offer_b": offer_results[1] if len(offer_results) > 1 else None,
        "traffic_split": traffic_split_result,
    }


def list_target_audiences(
    *,
    limit: int = 100,
    offset: int = 0,
    name: str | None = None,
    audience_type: str | None = "reusable",
) -> list[dict]:
    """List audiences. Defaults to reusable so results include name + id.

    Anonymous Target audiences often have no display name in MCP list/get.
    """
    client = connect_client()
    params: dict = {"limit": limit, "offset": offset}
    if name:
        params["name"] = name
    if audience_type:
        params["audience_type"] = audience_type
    response = client.call_tool("list_target_audiences", params)
    result = extract_tool_result(response)
    return extract_named_items(result, "audiences")


def list_target_mboxes(*, limit: int = 100, offset: int = 0, name: str | None = None) -> list[dict]:
    client = connect_client()
    params: dict = {"limit": limit, "offset": offset}
    if name:
        params["name"] = name
    response = client.call_tool("list_target_mboxes", params)
    result = extract_tool_result(response)
    return extract_named_items(result, "mboxes")


def get_target_mbox(mbox_name: str) -> dict:
    """Fetch full details of a single mbox (locations, params seen, etc.)."""
    client = connect_client()
    response = client.call_tool(
        "get_target_mbox", {"mbox_name": mbox_name}
    )
    return extract_tool_result(response)


def list_target_mbox_profile_attributes(
    *, limit: int = 200, offset: int = 0
) -> list[dict]:
    """Every visitor-profile attribute Adobe Target has recorded for this
    tenant. These are the `profile.*` variables usable in Target activity
    scripts and audience rules."""
    client = connect_client()
    response = client.call_tool(
        "list_target_mbox_profile_attributes",
        {"limit": limit, "offset": offset},
    )
    result = extract_tool_result(response)
    return extract_named_items(result, "attributes") or extract_named_items(
        result, "mbox_profile_attributes"
    )


def list_target_response_tokens() -> list[dict]:
    """Every response token registered on this tenant. Response tokens are the
    strings Target injects into `adobe.target.getOffer` response metadata so
    on-page code can read activity/experience info at delivery time."""
    client = connect_client()
    response = client.call_tool("list_target_response_tokens", {})
    result = extract_tool_result(response)
    return (
        extract_named_items(result, "response_tokens")
        or extract_named_items(result, "responseTokens")
        or extract_named_items(result, "items")
    )


def create_target_response_token(
    name: str, description: str | None = None
) -> dict:
    """Register a new response token. `name` is the key that will appear in
    `response.tnt.responseTokens[<name>]` at delivery time."""
    client = connect_client()
    params: dict = {"name": name}
    if description:
        params["description"] = description
    response = client.call_tool("create_target_response_token", params)
    return extract_tool_result(response)


def update_activity_schedule(
    activity_id: int,
    *,
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> dict:
    client = connect_client()
    params: dict = {"activity_id": int(activity_id)}
    if starts_at:
        params["starts_at"] = starts_at
    if ends_at:
        params["ends_at"] = ends_at
    response = client.call_tool("update_activity_schedule", params)
    return extract_tool_result(response)


def update_target_activity(activity_id: int, activity_patch: dict) -> dict:
    client = connect_client()
    response = client.call_tool(
        "update_activity",
        {"activity_id": int(activity_id), "activity": activity_patch},
    )
    return extract_tool_result(response)


def update_target_offer_content(
    offer_id: int,
    *,
    name: str | None = None,
    content: str | None = None,
) -> dict:
    client = connect_client()
    params: dict = {"offer_id": int(offer_id)}
    if name:
        params["name"] = name
    if content is not None:
        params["content"] = content
    response = client.call_tool("update_target_offer", params)
    return extract_tool_result(response)


def _walk_values(node: object) -> list[object]:
    items: list[object] = []
    if isinstance(node, dict):
        items.extend(node.values())
        for value in node.values():
            items.extend(_walk_values(value))
    elif isinstance(node, list):
        for value in node:
            items.extend(_walk_values(value))
    return items


def extract_offer_candidates(activity_detail: dict) -> list[dict]:
    candidates: list[dict] = []
    seen: set[int] = set()

    def add_candidate(offer_id: object, name: str | None = None, variant: str | None = None) -> None:
        if offer_id is None:
            return
        try:
            numeric_id = int(offer_id)
        except (TypeError, ValueError):
            return
        if numeric_id in seen:
            return
        seen.add(numeric_id)
        candidates.append(
            {
                "offer_id": numeric_id,
                "offer_name": name,
                "variant": variant,
            }
        )

    for key in ("experiences", "options", "variants"):
        entries = activity_detail.get(key)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            variant = (
                entry.get("name")
                or entry.get("variant")
                or entry.get("experienceLocalId")
                or f"variant_{chr(97 + index)}"
            )
            offer_id = (
                entry.get("offerId")
                or entry.get("offer_id")
                or entry.get("defaultOfferId")
            )
            offer_name = entry.get("offerName") or entry.get("offer_name")
            add_candidate(offer_id, offer_name, str(variant))

            offer = entry.get("offer")
            if isinstance(offer, dict):
                add_candidate(offer.get("id"), offer.get("name"), str(variant))

    for value in _walk_values(activity_detail):
        if not isinstance(value, dict):
            continue
        if "offerId" in value:
            add_candidate(value.get("offerId"), value.get("name"))
        if "offer_id" in value:
            add_candidate(value.get("offer_id"), value.get("name"))

    return candidates


def strip_name_prefix(name: str) -> str:
    return re.sub(r"^\[GitHub\]\[[^\]]+\]\s*", "", name).strip()


def build_folder_name(activity_name: str, activity_type: str) -> str:
    base = to_snake_case(strip_name_prefix(activity_name))
    suffix = "_ab_test" if normalize_activity_type(activity_type) == "ab" else "_xt_test"
    if not base.endswith("_xt_test") and not base.endswith("_ab_test"):
        base = f"{base}{suffix}"
    return base


def map_target_state(state: str | None) -> str:
    mapping = {
        "approved": "active",
        "deactivated": "inactive",
        "paused": "paused",
        "saved": "saved",
    }
    return mapping.get((state or "saved").lower(), "saved")


def map_repo_activity_type(activity_type: str) -> str:
    normalized = normalize_activity_type(activity_type)
    return {"ab": "AB", "xt": "XT", "abt": "ABT"}.get(normalized, "XT")


def extract_location(activity_detail: dict) -> str:
    for key in ("locations", "mboxes", "mboxNames"):
        value = activity_detail.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                return str(first.get("name") or first.get("mbox") or "home")
            return str(first)
        if isinstance(value, str) and value:
            return value
    return "home"


def import_activity_to_repo(
    activity_id: int,
    activity_type: str,
    *,
    offer_id: int | None = None,
    folder_name: str | None = None,
) -> dict:
    detail = get_target_activity(activity_id, activity_type)
    activity_name = detail.get("name") or f"Activity {activity_id}"
    clean_name = strip_name_prefix(activity_name)
    repo_type = map_repo_activity_type(activity_type)
    folder = folder_name or build_folder_name(clean_name, activity_type)
    folder_path = ROOT / folder

    if folder_path.exists():
        raise FileExistsError(f"Folder already exists: {folder}")

    offer_candidates = extract_offer_candidates(detail)
    selected_offer_id = offer_id or (offer_candidates[0]["offer_id"] if offer_candidates else None)
    if not selected_offer_id:
        raise ValueError(
            "Could not find an offer linked to this activity. Provide offer_id manually."
        )

    offer = get_target_offer(selected_offer_id)
    offer_content = offer.get("content") or "<section><h2>Imported offer</h2></section>"
    offer_name = offer.get("name") or f"{clean_name} - Variant A"

    variant_name = "variant_a"
    if offer_candidates:
        for candidate in offer_candidates:
            if candidate["offer_id"] == selected_offer_id and candidate.get("variant"):
                variant_name = str(candidate["variant"]).replace(" ", "_").lower()
                if not variant_name.startswith("variant_"):
                    variant_name = f"variant_{variant_name}"
                break

    html_file = f"{folder}_exp_a.html"
    template_info_path = ROOT / "_activity_template" / "activity-info.json"
    activity_info = json.loads(template_info_path.read_text(encoding="utf-8"))

    activity_info["activity_id"] = activity_id
    activity_info["activity_name"] = clean_name
    activity_info["activity_description"] = detail.get("description") or (
        f"Imported from Adobe Target activity {activity_id}"
    )
    activity_info["activity_status"] = map_target_state(detail.get("state"))
    activity_info["activity_type"] = repo_type
    activity_info["activity_location"] = extract_location(detail)
    if starts_at := detail.get("startsAt") or detail.get("starts_at"):
        activity_info["activity_start_date"] = str(starts_at)[:10]
    if ends_at := detail.get("endsAt") or detail.get("ends_at"):
        activity_info["activity_end_date"] = str(ends_at)[:10]

    activity_info["variants"] = [
        {
            "variant": variant_name,
            "html_file": html_file,
            "offer_name": strip_name_prefix(offer_name),
            "offer_id": selected_offer_id,
            "mode": "create_or_update",
        }
    ]

    folder_path.mkdir(parents=True)
    with (folder_path / "activity-info.json").open("w", encoding="utf-8") as handle:
        json.dump(activity_info, handle, indent=2)
        handle.write("\n")

    content = offer_content if offer_content.endswith("\n") else f"{offer_content}\n"
    with (folder_path / html_file).open("w", encoding="utf-8") as handle:
        handle.write(content)

    return {
        "folder": folder,
        "activity_id": activity_id,
        "offer_id": selected_offer_id,
        "activity_name": clean_name,
        "imported": True,
    }


def get_activity_bundle(activity_id: int, activity_type: str) -> dict:
    detail = get_target_activity(activity_id, activity_type)
    offers = extract_offer_candidates(detail)
    offer_details = []
    for offer in offers[:5]:
        try:
            offer_details.append(get_target_offer(offer["offer_id"]))
        except RuntimeError:
            offer_details.append({"id": offer["offer_id"], "content": None})

    return {
        "activity": detail,
        "offer_candidates": offers,
        "offers": offer_details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Adobe Target resources via MCP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=200)
    list_parser.add_argument("--offset", type=int, default=None)
    list_parser.add_argument(
        "--sort-by",
        default=None,
        help="Prefix with '-' for descending (e.g. '-modifiedAt').",
    )
    list_parser.add_argument(
        "--state",
        default=None,
        help="approved | deactivated | paused | saved",
    )
    list_parser.add_argument("--name-contains")
    list_parser.add_argument("--activity-type")
    list_parser.add_argument("--starts-after")
    list_parser.add_argument("--starts-before")
    list_parser.add_argument("--modified-after")
    list_parser.add_argument("--ends-after")
    list_parser.add_argument("--ends-before")
    list_parser.add_argument("--workspace")
    list_parser.add_argument("--segment-id")
    list_parser.add_argument("--profile-attribute-id")
    list_parser.add_argument("--priority", type=int, default=None)
    list_parser.add_argument("--mbox")
    list_parser.add_argument("--offer-id")
    list_parser.add_argument("--view-id")

    subparsers.add_parser("list-workspaces")

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("activity_id", type=int)
    get_parser.add_argument("--activity-type", default="xt")

    get_activity_parser = subparsers.add_parser("get-activity")
    get_activity_parser.add_argument("activity_id", type=int)
    get_activity_parser.add_argument("--activity-type", default="xt")

    get_offer_parser = subparsers.add_parser("get-offer")
    get_offer_parser.add_argument("offer_id", type=int)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("activity_id", type=int)
    import_parser.add_argument("--activity-type", default="xt")
    import_parser.add_argument("--offer-id", type=int)
    import_parser.add_argument("--folder-name")

    deactivate_parser = subparsers.add_parser("deactivate")
    deactivate_parser.add_argument("activity_id", type=int)

    preview_parser = subparsers.add_parser(
        "preview",
        help="Generate QA preview URLs for each experience of an activity.",
    )
    preview_parser.add_argument("activity_id", type=int)
    preview_parser.add_argument("--activity-type", default=None)
    preview_parser.add_argument(
        "--url",
        default=None,
        help="Page URL where the mbox is deployed. Required by MCP for "
        "form-based (non-VEC) activities.",
    )

    draft_parser = subparsers.add_parser("create-draft")
    draft_parser.add_argument("--payload-json", required=True)

    audiences_parser = subparsers.add_parser("list-audiences")
    audiences_parser.add_argument("--limit", type=int, default=100)
    audiences_parser.add_argument("--offset", type=int, default=0)
    audiences_parser.add_argument("--name", default=None)
    audiences_parser.add_argument(
        "--audience-type",
        default="reusable",
        help="reusable | anonymous | property (default: reusable, includes names)",
    )

    mboxes_parser = subparsers.add_parser("list-mboxes")
    mboxes_parser.add_argument("--limit", type=int, default=100)
    mboxes_parser.add_argument("--offset", type=int, default=0)
    mboxes_parser.add_argument("--name")

    get_mbox_parser = subparsers.add_parser("get-mbox")
    get_mbox_parser.add_argument("mbox_name")

    profile_attrs_parser = subparsers.add_parser("list-mbox-profile-attributes")
    profile_attrs_parser.add_argument("--limit", type=int, default=200)
    profile_attrs_parser.add_argument("--offset", type=int, default=0)

    subparsers.add_parser("list-response-tokens")

    create_token_parser = subparsers.add_parser("create-response-token")
    create_token_parser.add_argument("name")
    create_token_parser.add_argument("--description", default=None)

    schedule_parser = subparsers.add_parser("update-schedule")
    schedule_parser.add_argument("activity_id", type=int)
    schedule_parser.add_argument("--starts-at")
    schedule_parser.add_argument("--ends-at")

    update_parser = subparsers.add_parser("update-activity")
    update_parser.add_argument("activity_id", type=int)
    update_parser.add_argument("--payload-json", required=True)

    offer_update_parser = subparsers.add_parser("update-offer")
    offer_update_parser.add_argument("offer_id", type=int)
    offer_update_parser.add_argument("--name")
    offer_update_parser.add_argument("--content-json")

    traffic_parser = subparsers.add_parser("update-traffic-split")
    traffic_parser.add_argument("activity_id", type=int)
    traffic_parser.add_argument("--activity-type", default="ab")
    traffic_parser.add_argument(
        "--splits-json",
        required=True,
        help='JSON object mapping experience name → percent, e.g. {"Experience A":50,"Experience B":50}',
    )

    args = parser.parse_args()

    try:
        if args.command == "list":
            payload = list_target_activities(
                limit=args.limit,
                offset=args.offset,
                sort_by=args.sort_by,
                state=args.state,
                name_contains=args.name_contains,
                activity_type=args.activity_type,
                starts_after=args.starts_after,
                starts_before=args.starts_before,
                modified_after=args.modified_after,
                ends_after=args.ends_after,
                ends_before=args.ends_before,
                workspace=args.workspace,
                segment_id=args.segment_id,
                profile_attribute_id=args.profile_attribute_id,
                priority=args.priority,
                mbox=args.mbox,
                offer_id=args.offer_id,
                view_id=args.view_id,
            )
        elif args.command == "list-workspaces":
            payload = list_target_workspaces()
        elif args.command == "get":
            payload = get_activity_bundle(args.activity_id, args.activity_type)
        elif args.command == "get-activity":
            payload = get_target_activity(args.activity_id, args.activity_type)
        elif args.command == "get-offer":
            payload = get_target_offer(args.offer_id)
        elif args.command == "deactivate":
            payload = deactivate_target_activity(args.activity_id)
        elif args.command == "preview":
            payload = preview_target_activity(
                args.activity_id,
                activity_type=args.activity_type,
                url=args.url,
            )
        elif args.command == "create-draft":
            payload = create_draft_activity(json.loads(args.payload_json))
        elif args.command == "list-audiences":
            payload = list_target_audiences(
                limit=args.limit,
                offset=args.offset,
                name=args.name,
                audience_type=args.audience_type or None,
            )
        elif args.command == "list-mboxes":
            payload = list_target_mboxes(
                limit=args.limit,
                offset=args.offset,
                name=args.name,
            )
        elif args.command == "get-mbox":
            payload = get_target_mbox(args.mbox_name)
        elif args.command == "list-mbox-profile-attributes":
            payload = list_target_mbox_profile_attributes(
                limit=args.limit, offset=args.offset
            )
        elif args.command == "list-response-tokens":
            payload = list_target_response_tokens()
        elif args.command == "create-response-token":
            payload = create_target_response_token(
                args.name, description=args.description
            )
        elif args.command == "update-schedule":
            payload = update_activity_schedule(
                args.activity_id,
                starts_at=args.starts_at,
                ends_at=args.ends_at,
            )
        elif args.command == "update-activity":
            payload = update_target_activity(
                args.activity_id,
                json.loads(args.payload_json),
            )
        elif args.command == "update-offer":
            content = None
            if args.content_json:
                content = json.loads(args.content_json)
                if isinstance(content, dict):
                    content = content.get("content", "")
            payload = update_target_offer_content(
                args.offer_id,
                name=args.name,
                content=content,
            )
        elif args.command == "update-traffic-split":
            payload = update_traffic_split(
                args.activity_id,
                activity_type=args.activity_type,
                splits=json.loads(args.splits_json),
            )
        else:
            payload = import_activity_to_repo(
                args.activity_id,
                args.activity_type,
                offer_id=args.offer_id,
                folder_name=args.folder_name,
            )
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
