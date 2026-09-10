#!/usr/bin/env python3
"""Deploy activity folders to Adobe Target via MCP on merge to main."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

from activity_discovery import (
    ROOT,
    get_deployable_activity_changes,
    load_config,
)
DEFAULT_ADOBE_SCOPES = (
    "openid,AdobeID,target_sdk,additional_info.roles,"
    "read_organizations,additional_info.projectedProductContext"
)


def fetch_access_token_from_client_credentials(config: dict) -> str:
    client_id = os.environ.get("ADOBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ADOBE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise ValueError(
            "ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET are required to refresh token"
        )

    auth_config = config.get("auth", {})
    token_url = auth_config.get(
        "ims_token_url", "https://ims-na1.adobelogin.com/ims/token/v3"
    )
    scopes = auth_config.get("scopes", DEFAULT_ADOBE_SCOPES)

    print("Fetching Adobe access token using client credentials...", file=sys.stderr)
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scopes,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()

    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("Adobe IMS response did not include access_token")

    expires_in = payload.get("expires_in", "unknown")
    print(f"Adobe access token fetched successfully (expires_in={expires_in}).", file=sys.stderr)
    return access_token


def resolve_access_token(config: dict, force_refresh: bool = False) -> str:
    if force_refresh:
        return fetch_access_token_from_client_credentials(config)

    access_token = os.environ.get("ADOBE_ACCESS_TOKEN", "").strip()
    if access_token:
        print("Using ADOBE_ACCESS_TOKEN from environment.", file=sys.stderr)
        return access_token

    return fetch_access_token_from_client_credentials(config)


class McpClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id: str | None = None
        self.request_id = 0

    def set_token(self, token: str) -> None:
        self.token = token
        self.headers["Authorization"] = f"Bearer {token}"
        self.session_id = None
        self.request_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }

        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        with httpx.Client(timeout=120.0) as client:
            response = client.post(self.url, headers=headers, json=payload)
            response.raise_for_status()

            if "mcp-session-id" in response.headers:
                self.session_id = response.headers["mcp-session-id"]

            return parse_response(response)

    def initialize(self) -> dict:
        result = self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "analog-devices-github-deploy",
                    "version": "1.0.0",
                },
            },
        )
        self.call("notifications/initialized", {})
        return result

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments})


def parse_response(response: httpx.Response) -> dict:
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise RuntimeError("SSE response did not contain JSON data")

    if not response.text.strip():
        return {}

    return response.json()


def extract_tool_result(response: dict) -> dict:
    if "error" in response:
        raise RuntimeError(f"MCP error: {response['error']}")

    result = response.get("result", {})
    content = result.get("content", [])

    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

    return result


class DeployValidationError(RuntimeError):
    """Raised when deploy pre-checks fail."""


def extract_named_items(result: dict, collection_key: str) -> list[dict]:
    if isinstance(result, list):
        return result

    if collection_key in result and isinstance(result[collection_key], list):
        return result[collection_key]

    for value in result.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if "name" in value[0]:
                return value

    return []


def find_existing_activity_by_name(client: McpClient, activity_name: str) -> dict | None:
    response = client.call_tool("list_target_activities", {"limit": 200})
    result = extract_tool_result(response)
    activities = extract_named_items(result, "activities")

    for activity in activities:
        if activity.get("name") == activity_name:
            return activity

    return None


def find_existing_offer_by_name(client: McpClient, offer_name: str) -> dict | None:
    response = client.call_tool(
        "list_target_offers",
        {"name": offer_name, "type": "content", "limit": 200},
    )
    result = extract_tool_result(response)
    offers = extract_named_items(result, "offers")

    for offer in offers:
        if offer.get("name") == offer_name:
            return offer

    return None


ACTIVITY_GET_TOOLS = {
    "ab": "get_ab_activity",
    "xt": "get_xt_activity",
    "abt": "get_abt_activity",
}


def extract_offer_ids_from_activity_detail(detail: dict) -> list[int]:
    offer_ids: list[int] = []

    def add_offer_id(value: object) -> None:
        if value is None:
            return
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return
        if numeric > 0 and numeric not in offer_ids:
            offer_ids.append(numeric)

    for key in ("experiences", "options", "variants"):
        entries = detail.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            add_offer_id(
                entry.get("offerId")
                or entry.get("offer_id")
                or entry.get("defaultOfferId")
            )
            offer = entry.get("offer")
            if isinstance(offer, dict):
                add_offer_id(offer.get("id"))

    return offer_ids


def resolve_missing_target_ids(
    client: McpClient,
    activity_info: dict,
    offers: list[dict],
    config: dict,
) -> None:
    type_map = config.get("activity_type_map", {})

    if not activity_info.get("activity_id"):
        activity_name = str(activity_info.get("activity_name", ""))
        existing = find_existing_activity_by_name(client, activity_name)
        if not existing:
            search_term = activity_name.split("]")[-1].strip() if "]" in activity_name else activity_name
            response = client.call_tool(
                "list_target_activities",
                {"limit": 200, "name_contains": search_term[:60]},
            )
            result = extract_tool_result(response)
            activities = extract_named_items(result, "activities")
            lowered = search_term.lower()
            for activity in activities:
                target_name = str(activity.get("name", "")).lower()
                if lowered in target_name or target_name in lowered:
                    existing = activity
                    break

        if existing and existing.get("id"):
            activity_info["activity_id"] = int(existing["id"])
            print(
                "INFO: Resolved activity_id="
                f"{activity_info['activity_id']} from Target for update deploy."
            )

    activity_id = activity_info.get("activity_id")
    offer_ids_from_activity: list[int] = []
    if activity_id:
        activity_type = map_activity_type(
            activity_info.get("activity_type", "XT"),
            type_map,
        )
        get_tool = ACTIVITY_GET_TOOLS.get(activity_type, "get_xt_activity")
        try:
            response = client.call_tool(get_tool, {"activity_id": activity_id})
            detail = extract_tool_result(response)
            offer_ids_from_activity = extract_offer_ids_from_activity_detail(detail)
        except Exception as error:
            print(f"WARNING: Could not fetch activity detail for offer IDs: {error}")

    for offer in offers:
        if offer.get("offer_id"):
            continue

        offer_name = str(offer.get("offer_name", ""))
        if offer_name:
            existing_offer = find_existing_offer_by_name(client, offer_name)
            if existing_offer and existing_offer.get("id"):
                offer["offer_id"] = int(existing_offer["id"])
                print(
                    f"INFO: Resolved offer_id={offer['offer_id']} "
                    f"for '{offer_name}' from Target."
                )
                continue

        if offer_ids_from_activity:
            offer["offer_id"] = offer_ids_from_activity[0]
            print(
                f"INFO: Resolved offer_id={offer['offer_id']} "
                "from get_activity response."
            )


def assert_activity_name_available(
    client: McpClient, activity_name: str, config: dict
) -> None:
    if not config.get("validation", {}).get("check_duplicate_activity_name", True):
        return

    existing = find_existing_activity_by_name(client, activity_name)
    if existing:
        raise DeployValidationError(
            f"Activity '{activity_name}' already exists in Adobe Target "
            f"(id={existing.get('id')}). Use a different activity_name or set activity_id."
        )


def assert_offer_name_available(
    client: McpClient, offer_name: str, config: dict, offer_id: int | None
) -> None:
    if offer_id:
        return

    if not config.get("validation", {}).get("check_duplicate_offer_name", True):
        return

    existing = find_existing_offer_by_name(client, offer_name)
    if existing:
        raise DeployValidationError(
            f"Offer '{offer_name}' already exists in Adobe Target "
            f"(id={existing.get('id')}). Use a different offer_name or set offer_id."
        )


def load_activity_info(folder: Path, info_file: str) -> dict:
    with (folder / info_file).open(encoding="utf-8") as handle:
        return json.load(handle)


def read_offer_html(folder: Path, html_file: str) -> str:
    return (folder / html_file).read_text(encoding="utf-8")


def map_activity_state(status: str, status_map: dict) -> str:
    return status_map.get(status.lower(), "saved")


def map_activity_type(activity_type: str, type_map: dict) -> str:
    return type_map.get(activity_type.upper(), activity_type.lower())


def infer_variant_from_filename(filename: str) -> str | None:
    match = re.search(r"_exp_([a-z0-9_]+)$", Path(filename).stem, re.IGNORECASE)
    if not match:
        return None
    suffix = match.group(1).lower()
    return suffix if suffix.startswith("variant_") else f"variant_{suffix}"


def build_offer_name(activity_info: dict, variant: str) -> str:
    activity_name = activity_info.get("activity_name", "Offer")
    label = variant.replace("variant_", "Variant ").replace("_", " ").title()
    return f"{activity_name} - {label}"


def get_deploy_username(config: dict) -> str:
    return (
        os.environ.get("DEPLOY_USERNAME", "").strip()
        or os.environ.get("GITHUB_ACTOR", "").strip()
        or config.get("deploy_username", "").strip()
    )


def get_name_prefix(config: dict) -> str:
    base_prefix = config.get("deploy_name_prefix", "[GitHub]")
    username = get_deploy_username(config)
    if username:
        return f"{base_prefix}[{username}]"
    return base_prefix


def with_name_prefix(name: str, prefix: str) -> str:
    cleaned = name.strip()
    if not prefix:
        return cleaned
    if cleaned.startswith(prefix):
        return cleaned
    return f"{prefix} {cleaned}"


def apply_deploy_name_prefix(
    activity_info: dict, offers: list[dict], config: dict
) -> tuple[dict, list[dict]]:
    prefix = get_name_prefix(config)
    prefixed_activity = dict(activity_info)
    prefixed_activity["activity_name"] = with_name_prefix(
        activity_info.get("activity_name", "Activity"), prefix
    )

    prefixed_offers = []
    for offer in offers:
        prefixed_offer = dict(offer)
        prefixed_offer["offer_name"] = with_name_prefix(
            offer.get("offer_name", "Offer"), prefix
        )
        prefixed_offers.append(prefixed_offer)

    return prefixed_activity, prefixed_offers


def resolve_actions(activity_info: dict, config: dict) -> dict:
    defaults = config.get("default_actions", {})
    overrides = activity_info.get("actions", {})
    return {**defaults, **overrides}


def resolve_offers(folder: Path, activity_info: dict, html_pattern: str) -> list[dict]:
    if variants := activity_info.get("variants"):
        return variants

    offers: list[dict] = []
    default_variant = activity_info.get("activity_variant")

    for html_path in sorted(folder.glob(html_pattern)):
        inferred_variant = infer_variant_from_filename(html_path.name)
        variant = inferred_variant or default_variant
        if not variant:
            print(
                f"Warning: could not determine variant for {html_path.name}, skipping."
            )
            continue

        offers.append(
            {
                "variant": variant,
                "html_file": html_path.name,
                "offer_name": build_offer_name(activity_info, variant),
                "offer_id": None,
                "mode": "create_or_update",
            }
        )

    return offers


def build_activity_locations(activity_info: dict) -> list[dict]:
    # Prefer explicit mbox; activity_location is often a page URL for preview.
    mbox = (activity_info.get("mbox_name") or "").strip()
    if mbox:
        return [{"name": mbox}]
    location = (activity_info.get("activity_location") or "target-global-mbox").strip()
    if location.startswith("http://") or location.startswith("https://"):
        return [{"name": "target-global-mbox"}]
    return [{"name": location or "target-global-mbox"}]


def build_activity_experiences(activity_info: dict) -> list[dict]:
    experiences: list[dict] = []
    activity_type = str(activity_info.get("activity_type") or "XT").upper()
    split = activity_type in {"AB", "ABT"}

    for index, variant in enumerate(activity_info.get("variants", [])):
        name = (
            (variant.get("experience_name") or "").strip()
            or (variant.get("offer_name") or "").strip()
            or variant.get("variant")
            or f"Experience {chr(65 + min(index, 25))}"
        )
        entry: dict = {"name": name}
        if split:
            try:
                entry["visitorPercentage"] = int(variant.get("traffic_percent") or 0)
            except (TypeError, ValueError):
                entry["visitorPercentage"] = 0
        experiences.append(entry)

    if experiences:
        if split:
            total = sum(int(item.get("visitorPercentage") or 0) for item in experiences)
            if total != 100 and experiences:
                # Even split fallback when percents are missing/invalid.
                count = len(experiences)
                base = 100 // count
                remainder = 100 - base * count
                for index, item in enumerate(experiences):
                    item["visitorPercentage"] = base + (1 if index < remainder else 0)
        return experiences

    variant_name = activity_info.get("activity_variant", "Experience A")
    return [{"name": variant_name}]


def create_target_activity(
    client: McpClient, activity_info: dict, type_map: dict
) -> dict:
    activity_type = map_activity_type(activity_info.get("activity_type", "XT"), type_map)
    tool_name = f"create_{activity_type}_activity"
    activity_name = activity_info["activity_name"]
    experiences = build_activity_experiences(activity_info)

    params = {
        "name": activity_name,
        "state": "saved",
        "experiences": experiences,
        "variants": experiences,
        "locations": build_activity_locations(activity_info),
    }

    if starts_at := activity_info.get("activity_start_date"):
        params["starts_at"] = starts_at
    if ends_at := activity_info.get("activity_end_date"):
        params["ends_at"] = ends_at
    if description := activity_info.get("activity_description"):
        params["description"] = description
    # Optional workspace picker — only forward when set locally. Absent means
    # "use the tenant's default workspace" (Adobe MCP tool default behavior).
    if workspace_id := str(activity_info.get("workspace_id") or "").strip():
        params["workspace_id"] = workspace_id

    print(f"Creating activity in Adobe Target via {tool_name}...")
    response = client.call_tool(tool_name, params)
    result = extract_tool_result(response)

    # Retry without visitorPercentage / variants alias if schema rejects fields.
    if isinstance(result, dict) and result.get("raw"):
        raw = str(result.get("raw"))
        if "visitorPercentage" in raw or "validation error" in raw.lower():
            retry_experiences = [
                {k: v for k, v in item.items() if k != "visitorPercentage"}
                for item in experiences
            ]
            retry_params = {
                **params,
                "experiences": retry_experiences,
                "variants": retry_experiences,
            }
            if "variants" in raw.lower() and "extra" in raw.lower():
                retry_params.pop("variants", None)
            print("Retrying activity create with adjusted experience fields...")
            response = client.call_tool(tool_name, retry_params)
            result = extract_tool_result(response)

    activity_id = result.get("id")
    if activity_id:
        print(
            f"SUCCESS: Activity '{activity_name}' created in Adobe Target "
            f"(id={activity_id}, type={activity_type.upper()})."
        )
    else:
        print(
            f"WARNING: Activity '{activity_name}' create call completed, "
            "but no activity id was returned by Adobe Target."
        )

    return result


def deploy_offer(client: McpClient, offer_config: dict, html_content: str) -> dict:
    offer_name = offer_config["offer_name"]
    offer_id = offer_config.get("offer_id")
    mode = offer_config.get("mode", "create_or_update")

    if mode == "update" or (mode == "create_or_update" and offer_id):
        if not offer_id:
            raise ValueError(f"offer_id is required to update offer '{offer_name}'")

        print(f"Updating offer in Adobe Target (id={offer_id})...")
        response = client.call_tool(
            "update_target_offer",
            {
                "offer_id": offer_id,
                "name": offer_name,
                "content": html_content,
            },
        )
        result = extract_tool_result(response)
        print(
            f"SUCCESS: Offer '{offer_name}' updated in Adobe Target (id={offer_id})."
        )
        return result

    print(f"Creating offer in Adobe Target...")
    response = client.call_tool(
        "create_target_offer",
        {"name": offer_name, "content": html_content},
    )
    result = extract_tool_result(response)
    created_offer_id = result.get("id")
    if created_offer_id:
        print(
            f"SUCCESS: Offer '{offer_name}' created in Adobe Target "
            f"(id={created_offer_id})."
        )
    else:
        print(
            f"WARNING: Offer '{offer_name}' create call completed, "
            "but no offer id was returned by Adobe Target."
        )
    return result


def attach_offer_to_variant(
    client: McpClient,
    activity_info: dict,
    offer_config: dict,
    html_content: str,
    offer_result: dict,
    type_map: dict,
) -> dict:
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        print("Skipping variant attach: activity_id is 0 or missing.")
        return {"skipped": True, "reason": "activity_id missing"}

    activity_type = map_activity_type(activity_info.get("activity_type", "XT"), type_map)
    variant_name = offer_config.get("variant") or activity_info.get("activity_variant")
    experience_name = (
        (offer_config.get("experience_name") or "").strip()
        or (offer_config.get("offer_name") or "").strip()
        or variant_name
    )
    offer_id = offer_result.get("id") or offer_config.get("offer_id")

    candidates = []
    for name in (experience_name, variant_name, offer_config.get("offer_name")):
        cleaned = (name or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    last_error = None
    for candidate in candidates:
        params = {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "variant_name": candidate,
            "experience_name": candidate,
        }
        if offer_id:
            params["offer_id"] = offer_id
        else:
            params["offer_content"] = html_content

        print(
            f"Attaching offer to activity {activity_id} "
            f"variant '{candidate}'..."
        )
        try:
            response = client.call_tool("update_variant_offer", params)
            return extract_tool_result(response)
        except Exception as error:  # noqa: BLE001
            last_error = error
            continue

    if last_error:
        raise last_error
    return {"skipped": True, "reason": "no variant name"}


def sync_activity_state(
    client: McpClient, activity_info: dict, status_map: dict
) -> dict:
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        print("Skipping activity state sync: activity_id is 0 or missing.")
        return {"skipped": True, "reason": "activity_id missing"}

    status = activity_info.get("activity_status", "saved")
    state = map_activity_state(status, status_map)

    print(f"Syncing activity {activity_id} state to '{state}'...")
    response = client.call_tool(
        "update_activity_state",
        {"activity_id": activity_id, "state": state},
    )
    return extract_tool_result(response)


def get_target_activity_snapshot(
    client: McpClient, activity_info: dict, type_map: dict
) -> dict:
    """Fetch the current Target activity payload once so every sync step can
    diff against it instead of making its own round trip. Returns `{}` when
    there's no activity yet (create path) or when the fetch fails — callers
    treat empty snapshot as "push everything anyway", not "skip".
    """
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        return {}
    activity_type = map_activity_type(
        activity_info.get("activity_type", "XT"), type_map
    )
    tool_name = f"get_{activity_type}_activity"
    try:
        response = client.call_tool(
            tool_name, {"activity_id": int(activity_id)}
        )
        result = extract_tool_result(response)
        return result if isinstance(result, dict) else {}
    except Exception as error:  # noqa: BLE001
        print(
            f"WARNING: Could not fetch current Target snapshot for diff: {error}"
        )
        return {}


def _extract_target_experiences(current: dict) -> list[dict]:
    """Pull the variant list from a Target activity payload, tolerating the
    two names the MCP schema has used (`experiences` and `variants`)."""
    for key in ("experiences", "variants", "options"):
        entries = current.get(key)
        if isinstance(entries, list) and entries:
            return [e for e in entries if isinstance(e, dict)]
    return []


def _local_variant_display_name(variant: dict) -> str:
    """The name we use both to reference the variant in Target and to match
    it against Target's `experiences[].name`. Falls back through the same
    priority order as `build_activity_experiences`."""
    return (
        (variant.get("experience_name") or "").strip()
        or (variant.get("offer_name") or "").strip()
        or (variant.get("variant") or "").strip()
    )


def sync_activity_name(
    client: McpClient,
    activity_info: dict,
    current: dict,
    config: dict,
) -> dict:
    """Push a rename to Target only when the (prefixed) local name differs.

    Compares AFTER applying the deploy prefix so we don't churn the name on
    every merge — the deploy always re-adds `[GitHub][user]` and Target
    already has it. Only surfaces a call to `update_activity_name` when the
    user actually changed the base name in `activity-info.json`.
    """
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        return {"skipped": True, "reason": "activity_id missing"}

    prefix = get_name_prefix(config)
    desired = with_name_prefix(
        activity_info.get("activity_name", ""), prefix
    ).strip()
    if not desired:
        return {"skipped": True, "reason": "no activity_name"}

    current_name = str(current.get("name") or "").strip()
    if current_name and current_name == desired:
        return {"skipped": True, "reason": "name unchanged"}

    print(
        f"Renaming Target activity {activity_id}: "
        f"'{current_name or '(unknown)'}' -> '{desired}'"
    )
    response = client.call_tool(
        "update_activity_name",
        {"activity_id": int(activity_id), "name": desired},
    )
    return {
        "previous": current_name,
        "next": desired,
        "result": extract_tool_result(response),
    }


def sync_activity_priority(
    client: McpClient, activity_info: dict, current: dict
) -> dict:
    """Optional. If `activity_priority` (0–999) is set locally AND differs
    from Target's current priority, push it via `update_activity_priority`.
    Absence in `activity-info.json` means "leave whatever Target has alone"."""
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        return {"skipped": True, "reason": "activity_id missing"}

    raw = activity_info.get("activity_priority")
    if raw is None or raw == "":
        return {"skipped": True, "reason": "priority not set locally"}
    try:
        desired = int(raw)
    except (TypeError, ValueError):
        return {"skipped": True, "reason": f"invalid priority '{raw}'"}
    if not 0 <= desired <= 999:
        return {"skipped": True, "reason": f"priority out of range: {desired}"}

    current_priority: int | None = None
    try:
        if current.get("priority") is not None:
            current_priority = int(current.get("priority"))
    except (TypeError, ValueError):
        current_priority = None
    if current_priority == desired:
        return {"skipped": True, "reason": "priority unchanged"}

    print(
        f"Updating Target activity {activity_id} priority: "
        f"{current_priority} -> {desired}"
    )
    response = client.call_tool(
        "update_activity_priority",
        {"activity_id": int(activity_id), "priority": desired},
    )
    return {
        "previous": current_priority,
        "next": desired,
        "result": extract_tool_result(response),
    }


def sync_activity_description(
    client: McpClient, activity_info: dict, current: dict
) -> dict:
    """Generic-field sync via `update_activity`. Description is the most
    common field that drifts between GitHub and Target — everything else
    (name/priority/schedule/state/variants) already has a specialized tool
    that produces cleaner diffs and audit trails."""
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        return {"skipped": True, "reason": "activity_id missing"}

    desired = str(activity_info.get("activity_description") or "").strip()
    if not desired:
        return {"skipped": True, "reason": "no description set locally"}
    current_desc = str(current.get("description") or "").strip()
    if current_desc == desired:
        return {"skipped": True, "reason": "description unchanged"}

    print(f"Updating Target activity {activity_id} description...")
    response = client.call_tool(
        "update_activity",
        {
            "activity_id": int(activity_id),
            "activity": {"description": desired},
        },
    )
    return {
        "previous": current_desc,
        "next": desired,
        "result": extract_tool_result(response),
    }


def sync_activity_variants(
    client: McpClient,
    activity_info: dict,
    current: dict,
    config: dict,
    actions: dict,
) -> dict:
    """Diff local `variants[]` against Target's `experiences[]`.

    * NEW locally, missing in Target      -> `add_activity_variant`
    * IN Target but missing locally       -> `remove_activity_variant`
                                             (only when `remove_missing_variants`
                                             action is true — destructive)
    * AB / ABT with a valid 100%-summed
      traffic_percent config              -> `update_traffic_split`

    Runs BEFORE the offer-attach loop so newly-added variants exist by the
    time `update_variant_offer` tries to point them at an offer.
    """
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        return {"skipped": True, "reason": "activity_id missing"}

    type_map = config.get("activity_type_map", {})
    activity_type = map_activity_type(
        activity_info.get("activity_type", "XT"), type_map
    )
    local_variants = activity_info.get("variants") or []
    target_variants = _extract_target_experiences(current)

    local_names: list[str] = []
    for variant in local_variants:
        if not isinstance(variant, dict):
            continue
        name = _local_variant_display_name(variant)
        if name:
            local_names.append(name)

    target_names = [
        str(v.get("name") or "").strip() for v in target_variants
    ]

    result: dict = {
        "added": [],
        "removed": [],
        "traffic_split": None,
    }

    if actions.get("add_activity_variants", True):
        for name in local_names:
            if name in target_names:
                continue
            print(
                f"Adding variant '{name}' to Target activity {activity_id}..."
            )
            try:
                response = client.call_tool(
                    "add_activity_variant",
                    {
                        "activity_id": int(activity_id),
                        "activity_type": activity_type,
                        "name": name,
                    },
                )
                result["added"].append(
                    {"name": name, "result": extract_tool_result(response)}
                )
                target_names.append(name)
            except Exception as error:  # noqa: BLE001
                result["added"].append({"name": name, "error": str(error)})

    if actions.get("remove_missing_variants", False):
        # Removing is destructive — only runs when explicitly opted in. Target
        # activities often have anonymous or auto-created variants we don't
        # want the deploy to accidentally wipe just because GitHub disagrees.
        for target_variant in target_variants:
            name = str(target_variant.get("name") or "").strip()
            if not name or name in local_names:
                continue
            print(
                f"Removing stale variant '{name}' from Target activity "
                f"{activity_id}..."
            )
            try:
                response = client.call_tool(
                    "remove_activity_variant",
                    {
                        "activity_id": int(activity_id),
                        "activity_type": activity_type,
                        "name": name,
                    },
                )
                result["removed"].append(
                    {"name": name, "result": extract_tool_result(response)}
                )
            except Exception as error:  # noqa: BLE001
                result["removed"].append({"name": name, "error": str(error)})

    if activity_type in {"ab", "abt"} and local_variants:
        splits: dict[str, int] = {}
        for variant in local_variants:
            if not isinstance(variant, dict):
                continue
            name = _local_variant_display_name(variant)
            if not name:
                continue
            try:
                splits[name] = int(variant.get("traffic_percent") or 0)
            except (TypeError, ValueError):
                splits[name] = 0

        total = sum(splits.values())
        if not splits:
            result["traffic_split"] = {
                "skipped": True,
                "reason": "no traffic_percent set on local variants",
            }
        elif total != 100:
            # Refuse to push an invalid split — Target would reject it too and
            # the error is friendlier surfaced here.
            result["traffic_split"] = {
                "skipped": True,
                "reason": (
                    f"local traffic_percent values sum to {total}, must be 100"
                ),
            }
        else:
            current_splits: dict[str, int] = {}
            for entry in target_variants:
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                try:
                    current_splits[name] = int(
                        entry.get("visitorPercentage")
                        or entry.get("traffic_percent")
                        or 0
                    )
                except (TypeError, ValueError):
                    current_splits[name] = 0
            if current_splits == splits:
                result["traffic_split"] = {
                    "skipped": True,
                    "reason": "traffic split unchanged",
                }
            else:
                print(
                    f"Updating traffic split for Target activity "
                    f"{activity_id}: {splits}"
                )
                try:
                    response = client.call_tool(
                        "update_traffic_split",
                        {
                            "activity_id": int(activity_id),
                            "activity_type": activity_type,
                            "splits": splits,
                        },
                    )
                    result["traffic_split"] = {
                        "previous": current_splits,
                        "next": splits,
                        "result": extract_tool_result(response),
                    }
                except Exception as error:  # noqa: BLE001
                    result["traffic_split"] = {"error": str(error)}

    return result


def sync_activity_schedule(client: McpClient, activity_info: dict) -> dict:
    """Push activity_start_date / activity_end_date from activity-info.json
    to Target via the `update_activity_schedule` MCP tool.

    Only runs when the activity already exists in Target (`activity_id > 0`).
    On the *initial* create path we already pass starts_at / ends_at inline to
    `create_*_activity`, so this function is primarily what makes schedule
    edits from the "Goals & schedule" panel actually land in Target on merge.

    Returns `{skipped: True, reason}` when there's nothing to do, so the
    deploy summary can distinguish "we tried and failed" from "there was
    nothing scheduled".
    """
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        return {"skipped": True, "reason": "activity_id missing"}

    starts_at = (activity_info.get("activity_start_date") or "").strip()
    ends_at = (activity_info.get("activity_end_date") or "").strip()
    if not starts_at and not ends_at:
        # Both blank means "no schedule" — don't push empty strings, MCP would
        # either reject them or, worse, clear an existing schedule silently.
        return {"skipped": True, "reason": "no schedule dates set"}

    params: dict = {"activity_id": int(activity_id)}
    if starts_at:
        params["starts_at"] = starts_at
    if ends_at:
        params["ends_at"] = ends_at

    print(
        f"Syncing activity {activity_id} schedule "
        f"(starts_at={starts_at or '-'}, ends_at={ends_at or '-'})..."
    )
    response = client.call_tool("update_activity_schedule", params)
    return extract_tool_result(response)


def validate_update_requirements(
    activity_info: dict, offers: list[dict], folder: Path
) -> None:
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        raise DeployValidationError(
            f"Update deploy for '{folder.name}' requires a non-zero activity_id "
            "in activity-info.json."
        )

    for offer in offers:
        if not offer.get("offer_id"):
            raise DeployValidationError(
                f"Update deploy for '{folder.name}' requires offer_id for "
                f"'{offer.get('html_file', 'offer')}' in activity-info.json."
            )


def deploy_activity_folder(
    client: McpClient, folder: Path, config: dict, *, deploy_mode: str
) -> dict:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    html_pattern = discovery.get("html_pattern", "*.html")

    activity_info = load_activity_info(folder, info_file)
    actions = resolve_actions(activity_info, config)
    offers = resolve_offers(folder, activity_info, html_pattern)
    activity_info, offers = apply_deploy_name_prefix(activity_info, offers, config)

    results = {
        "folder": str(folder.relative_to(ROOT)).replace("\\", "/"),
        "activity_name": activity_info.get("activity_name"),
        "deploy_mode": deploy_mode,
        "activity_create_result": None,
        "activity_update_result": None,
        "offers": [],
        "activity_state": None,
        # Diff-based sync outcomes. All start as None and get populated with
        # `{skipped: True, reason}` or an actual result when the corresponding
        # action runs. Deploy summary reads these to print per-step status.
        "activity_name_sync": None,
        "activity_priority_sync": None,
        "activity_description_sync": None,
        "activity_variants_sync": None,
        "activity_schedule": None,
    }

    print(f"\nDeploying activity folder ({deploy_mode}): {results['folder']}")

    if deploy_mode == "update":
        resolve_missing_target_ids(client, activity_info, offers, config)
        validate_update_requirements(activity_info, offers, folder)
        for offer in offers:
            offer["mode"] = "update"
        print(
            f"INFO: Updating existing Adobe Target activity "
            f"'{activity_info['activity_name']}' (id={activity_info['activity_id']})."
        )

    should_create_activity = (
        deploy_mode == "create"
        and actions.get("create_activity_if_missing", False)
        and not activity_info.get("activity_id")
    )

    if should_create_activity:
        assert_activity_name_available(
            client, activity_info["activity_name"], config
        )
        activity_result = create_target_activity(
            client,
            activity_info,
            config.get("activity_type_map", {}),
        )
        results["activity_create_result"] = activity_result
        created_id = activity_result.get("id")
        if created_id:
            activity_info["activity_id"] = created_id
    elif activity_info.get("activity_id"):
        print(
            f"INFO: Using existing Adobe Target activity "
            f"'{activity_info['activity_name']}' (id={activity_info['activity_id']})."
        )
        results["activity_update_result"] = {
            "activity_id": activity_info.get("activity_id"),
            "message": "Existing activity reused for offer update",
        }

    # ---------------------------------------------------------------------
    # Intelligent metadata + variants sync (only when an activity exists).
    #
    # Fetch Target's current state ONCE and thread it through every diff-
    # based helper below so each one can skip when its field is unchanged —
    # avoids gratuitous MCP calls and makes deploy summaries actionable
    # ("what actually changed this merge?").
    #
    # Order matters:
    #   1. Metadata (name/priority/description) — pure updates on the
    #      activity envelope, safe in any order relative to each other.
    #   2. Variants (add/remove) — must happen BEFORE the offer-attach loop
    #      below so that `update_variant_offer` finds the variants it needs
    #      to attach offers to.
    #   3. Traffic split — handled inside `sync_activity_variants` and only
    #      runs after adds/removes so percentages are calculated against the
    #      final variant set.
    #   4. State + schedule — run AFTER offers are attached (further down)
    #      so we never activate an activity that's still missing offer bindings.
    # ---------------------------------------------------------------------
    if activity_info.get("activity_id"):
        current_snapshot = get_target_activity_snapshot(
            client, activity_info, config.get("activity_type_map", {})
        )

        if actions.get("sync_activity_name", True):
            results["activity_name_sync"] = sync_activity_name(
                client, activity_info, current_snapshot, config
            )
        if actions.get("sync_activity_priority", True):
            results["activity_priority_sync"] = sync_activity_priority(
                client, activity_info, current_snapshot
            )
        if actions.get("sync_activity_description", True):
            results["activity_description_sync"] = sync_activity_description(
                client, activity_info, current_snapshot
            )
        if actions.get("sync_activity_variants", True):
            results["activity_variants_sync"] = sync_activity_variants(
                client, activity_info, current_snapshot, config, actions
            )

    if not offers:
        print("No offers found to deploy.")
        return results

    for offer_config in offers:
        html_file = offer_config["html_file"]
        html_content = read_offer_html(folder, html_file)

        if not html_content.strip():
            print(f"Warning: {html_file} is empty, skipping offer deploy.")
            continue

        if not actions.get("push_offer", True):
            continue

        assert_offer_name_available(
            client,
            offer_config["offer_name"],
            config,
            offer_config.get("offer_id"),
        )
        offer_result = deploy_offer(client, offer_config, html_content)
        offer_entry = {
            "html_file": html_file,
            "variant": offer_config.get("variant"),
            "offer_result": offer_result,
        }

        if actions.get("attach_offer_to_variant", False):
            offer_entry["variant_attach_result"] = attach_offer_to_variant(
                client,
                activity_info,
                offer_config,
                html_content,
                offer_result,
                config.get("activity_type_map", {}),
            )

        results["offers"].append(offer_entry)

    if actions.get("sync_activity_state", False):
        results["activity_state"] = sync_activity_state(
            client,
            activity_info,
            config.get("status_map", {}),
        )

    # Push schedule changes (start/end dates from Goals & schedule) to Target
    # AFTER create-or-update settled the activity_id. Safe to run in both
    # create and update modes: create already includes the initial dates
    # inline, but re-sending them here is idempotent and lets edits made in
    # ATGitOps land in Target on the next merge without a separate manual step.
    if actions.get("sync_activity_schedule", True) and activity_info.get(
        "activity_id"
    ):
        results["activity_schedule"] = sync_activity_schedule(
            client, activity_info
        )

    return results


def print_deploy_summary(all_results: list[dict]) -> None:
    print("\n=== Adobe Target Deploy Summary ===")

    for result in all_results:
        folder = result.get("folder", "unknown")
        activity_name = result.get("activity_name", "unknown")
        deploy_mode = result.get("deploy_mode", "unknown")
        print(f"\nFolder: {folder}")
        print(f"Activity: {activity_name}")
        print(f"Mode: {deploy_mode}")

        activity_create = result.get("activity_create_result") or {}
        activity_id = activity_create.get("id")
        if activity_id:
            print(
                f"  - Activity created in Adobe Target: "
                f"'{activity_name}' (id={activity_id})"
            )
        elif deploy_mode == "update":
            update_result = result.get("activity_update_result") or {}
            existing_id = update_result.get("activity_id")
            print(
                f"  - Activity updated in Adobe Target: "
                f"'{activity_name}' (id={existing_id})"
            )
        else:
            print("  - Activity not created in this deploy run.")

        offers = result.get("offers", [])
        if not offers:
            print("  - No offers deployed.")
        else:
            for offer in offers:
                offer_result = offer.get("offer_result") or {}
                offer_id = offer_result.get("id")
                html_file = offer.get("html_file", "unknown")
                if offer_id:
                    action = "updated" if deploy_mode == "update" else "deployed"
                    print(
                        f"  - Offer {action} from '{html_file}' "
                        f"in Adobe Target (id={offer_id})"
                    )
                else:
                    print(f"  - Offer processed from '{html_file}'.")

        # Report each intelligent sync step. A `None` value means the action
        # wasn't enabled; `{skipped: True}` means it ran but decided nothing
        # needed pushing; anything else is a real MCP call.
        name_sync = result.get("activity_name_sync")
        if isinstance(name_sync, dict) and not name_sync.get("skipped"):
            previous = name_sync.get("previous") or "(unknown)"
            nxt = name_sync.get("next") or "(unknown)"
            print(f"  - Activity renamed in Adobe Target: '{previous}' -> '{nxt}'")

        priority_sync = result.get("activity_priority_sync")
        if isinstance(priority_sync, dict) and not priority_sync.get("skipped"):
            print(
                f"  - Activity priority set to {priority_sync.get('next')} "
                f"(was {priority_sync.get('previous')})."
            )

        desc_sync = result.get("activity_description_sync")
        if isinstance(desc_sync, dict) and not desc_sync.get("skipped"):
            print("  - Activity description updated in Adobe Target.")

        variants_sync = result.get("activity_variants_sync")
        if isinstance(variants_sync, dict) and not variants_sync.get("skipped"):
            added = variants_sync.get("added") or []
            removed = variants_sync.get("removed") or []
            for entry in added:
                if entry.get("error"):
                    print(
                        f"  - WARN: Could not add variant '{entry.get('name')}': "
                        f"{entry.get('error')}"
                    )
                else:
                    print(f"  - Variant added: '{entry.get('name')}'")
            for entry in removed:
                if entry.get("error"):
                    print(
                        f"  - WARN: Could not remove variant '{entry.get('name')}': "
                        f"{entry.get('error')}"
                    )
                else:
                    print(f"  - Variant removed: '{entry.get('name')}'")
            traffic = variants_sync.get("traffic_split")
            if isinstance(traffic, dict) and not traffic.get("skipped"):
                if traffic.get("error"):
                    print(
                        f"  - WARN: Traffic split update failed: {traffic.get('error')}"
                    )
                else:
                    print(f"  - Traffic split updated to {traffic.get('next')}.")

        schedule = result.get("activity_schedule")
        if isinstance(schedule, dict) and not schedule.get("skipped"):
            print("  - Activity schedule synced to Adobe Target.")

    print("\nDeployment to Adobe Target MCP completed successfully.")


def main() -> int:
    config = load_config()
    mcp_url = os.environ.get(
        "MCP_SERVER_URL",
        config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp"),
    )

    try:
        token = resolve_access_token(config)
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        print(f"Failed to resolve Adobe access token: {error}", file=sys.stderr)
        return 1

    activity_changes = get_deployable_activity_changes(config)
    if not activity_changes:
        print(
            "ERROR: No activity changes found in this merge.",
            file=sys.stderr,
        )
        print(
            "Add a new activity folder or update an existing activity folder.",
            file=sys.stderr,
        )
        return 1

    print(
        "Activity changes to deploy: "
        + ", ".join(
            f"{change['folder'].relative_to(ROOT).as_posix()} ({change['mode']})"
            for change in activity_changes
        )
    )

    client = McpClient(mcp_url, token)
    print("Connecting to Adobe Target MCP server...")

    try:
        client.initialize()
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 401:
            raise

        print("Access token rejected (401). Refreshing token from client credentials...")
        try:
            refreshed_token = resolve_access_token(config, force_refresh=True)
            client.set_token(refreshed_token)
            client.initialize()
        except (ValueError, RuntimeError, httpx.HTTPError) as refresh_error:
            print(
                "Token refresh failed. Update ADOBE_ACCESS_TOKEN in GitHub Secrets.",
                file=sys.stderr,
            )
            print(refresh_error, file=sys.stderr)
            return 1

    all_results = []
    try:
        for change in activity_changes:
            all_results.append(
                deploy_activity_folder(
                    client,
                    change["folder"],
                    config,
                    deploy_mode=change["mode"],
                )
            )
    except DeployValidationError as error:
        print(f"Deploy validation failed: {error}", file=sys.stderr)
        return 1

    print_deploy_summary(all_results)
    print("\nDeployment details:")
    print(json.dumps(all_results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
