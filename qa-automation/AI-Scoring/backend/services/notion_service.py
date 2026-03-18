"""
Notion SOP fetcher.
Fetches SOPs from the Member Support Hub and matches call type to the right SOP.
Uses Notion REST API directly (no MCP needed in a server-side context).
"""

import os
import re
from typing import Optional
import httpx

NOTION_API_VERSION = "2022-06-28"
HUB_PAGE_ID = "2181efec9aec46758f5538a9a120524e"  # Member Support Hub Home Page

# Rule-based call type → keyword mapping.
# The first SOP whose keywords match the transcript wins.
# Add more entries as new SOPs are documented in Notion.
CALL_TYPE_KEYWORDS: dict[str, list[str]] = {
    "member_lockout": [
        "locked out", "lock out", "lockout", "can't get in", "cannot get in",
        "access code", "door code", "key fob", "entry", "access issue",
    ],
    "extension": [
        "extend", "extension", "stay longer", "extra night", "extra days",
        "checkout", "check out", "late checkout",
    ],
    "early_checkin": [
        "early check in", "early check-in", "early checkin", "arrive early",
        "before check-in time",
    ],
    "move_in": [
        "move in", "move-in", "first day", "arriving today", "new resident",
        "my first", "just moved",
    ],
    "maintenance": [
        "maintenance", "repair", "broken", "not working", "fix", "issue with",
        "appliance", "hvac", "ac ", "air conditioning", "heating", "plumbing",
        "water", "leak", "dishwasher", "washer", "dryer", "oven", "microwave",
    ],
    "billing": [
        "charge", "payment", "invoice", "bill", "refund", "overcharged",
        "credit card", "bank", "fee",
    ],
    "airbnb": [
        "airbnb", "vrbo", "third party", "booking.com", "external platform",
    ],
}


def _headers() -> dict:
    token = os.getenv("NOTION_API_KEY")
    if not token:
        return None  # caller checks for None and skips
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Call type classification
# ---------------------------------------------------------------------------

def classify_call_type(transcript_text: str) -> Optional[str]:
    """
    Classify the call type from transcript text using keyword matching.
    Returns a call_type key (e.g. "member_lockout") or None if no match.
    """
    text_lower = transcript_text.lower()
    for call_type, keywords in CALL_TYPE_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return call_type
    return None


# ---------------------------------------------------------------------------
# Notion page fetching
# ---------------------------------------------------------------------------

async def _get_child_pages(page_id: str) -> list[dict]:
    """Return direct child blocks of a Notion page that are child_page type."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_headers(),
            params={"page_size": 100},
            timeout=15,
        )
        response.raise_for_status()
        blocks = response.json().get("results", [])

    return [b for b in blocks if b.get("type") == "child_page"]


async def _extract_page_text(page_id: str, max_blocks: int = 200) -> str:
    """
    Recursively extract plain text content from a Notion page.
    Stops at max_blocks to avoid very large SOPs blowing up the prompt.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_headers(),
            params={"page_size": max_blocks},
            timeout=15,
        )
        response.raise_for_status()
        blocks = response.json().get("results", [])

    text_parts = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})
        rich_text = content.get("rich_text", [])
        line = "".join(rt.get("plain_text", "") for rt in rich_text).strip()
        if line:
            # Add simple formatting hints for the model
            if block_type in ("heading_1", "heading_2", "heading_3"):
                text_parts.append(f"\n## {line}")
            elif block_type == "bulleted_list_item":
                text_parts.append(f"- {line}")
            elif block_type == "numbered_list_item":
                text_parts.append(f"* {line}")
            else:
                text_parts.append(line)

    return "\n".join(text_parts)


async def get_hub_sop_index() -> dict[str, str]:
    """
    Build a {title_slug: page_id} index of all direct child pages of the Hub.
    Call this once at startup or on-demand to discover available SOPs.
    """
    children = await _get_child_pages(HUB_PAGE_ID)
    index = {}
    for child in children:
        title = child.get("child_page", {}).get("title", "").strip()
        page_id = child.get("id", "").replace("-", "")
        if title and page_id:
            slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
            index[slug] = {"page_id": page_id, "title": title}
    return index


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Simple mapping from call_type → partial title match in the Notion Hub.
# Adjust these strings to match your actual Notion page titles.
CALL_TYPE_TO_NOTION_TITLE: dict[str, str] = {
    "member_lockout": "lockout",
    "extension": "extension",
    "early_checkin": "check-in",
    "move_in": "move-in",
    "maintenance": "maintenance",
    "billing": "billing",
    "airbnb": "airbnb",
}


async def fetch_sop_for_call(transcript_text: str) -> dict:
    """
    Classify the call type, find the matching SOP in Notion, and return its text.
    Returns a dict with 'sop_title' and 'sop_content'.
    Returns empty strings if Notion is not configured or no match is found.
    """
    if not os.getenv("NOTION_API_KEY"):
        return {"sop_title": "", "sop_content": ""}

    call_type = classify_call_type(transcript_text)
    if not call_type:
        return {"sop_title": "", "sop_content": ""}

    title_keyword = CALL_TYPE_TO_NOTION_TITLE.get(call_type, "")
    if not title_keyword:
        return {"sop_title": "", "sop_content": ""}

    try:
        sop_index = await get_hub_sop_index()
        # Find the first page whose slug contains our keyword
        matched_page = None
        for slug, meta in sop_index.items():
            if title_keyword.lower() in slug or title_keyword.lower() in meta["title"].lower():
                matched_page = meta
                break

        if not matched_page:
            return {"sop_title": "", "sop_content": ""}

        sop_content = await _extract_page_text(matched_page["page_id"])
        return {
            "sop_title": matched_page["title"],
            "sop_content": sop_content,
        }

    except Exception as e:
        # SOP fetch failure is non-fatal — score without SOP context
        print(f"[notion_service] SOP fetch failed: {e}")
        return {"sop_title": "", "sop_content": ""}
