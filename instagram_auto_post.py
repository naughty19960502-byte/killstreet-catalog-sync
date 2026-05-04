#!/usr/bin/env python3
"""
Post new catalog items from docs/feed.xml to Instagram.

Dry-run by default:
  python instagram_auto_post.py --dry-run --sync-existing

Publish:
  python instagram_auto_post.py --publish --sync-existing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


FEED_PATH = Path("docs/feed.xml")
STATE_PATH = Path("instagram_posted_items.json")
GRAPH_API_VERSION = os.environ.get("IG_GRAPH_API_VERSION", "v24.0")
GRAPH_API_BASE = os.environ.get("IG_GRAPH_API_BASE", "https://graph.facebook.com")
XML_NS = {"g": "http://base.google.com/ns/1.0"}


@dataclass
class Product:
    id: str
    title: str
    description: str
    price: str
    link: str
    image_url: str
    availability: str


def text_at(node: ET.Element, path: str, default: str = "") -> str:
    found = node.find(path, XML_NS)
    return (found.text or "").strip() if found is not None else default


def load_products(feed_path: Path) -> list[Product]:
    if not feed_path.exists():
        raise FileNotFoundError(f"Feed not found: {feed_path}")

    root = ET.parse(feed_path).getroot()
    products: list[Product] = []
    for item in root.findall("./channel/item"):
        products.append(
            Product(
                id=text_at(item, "g:id"),
                title=text_at(item, "g:title"),
                description=text_at(item, "g:description"),
                price=text_at(item, "g:price"),
                link=text_at(item, "g:link"),
                image_url=text_at(item, "g:image_link"),
                availability=text_at(item, "g:availability"),
            )
        )
    return [p for p in products if p.id and p.image_url]


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"posted_ids": {}, "updated_at": None}
    with state_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if "posted_ids" not in data or not isinstance(data["posted_ids"], dict):
        data["posted_ids"] = {}
    return data


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def caption_for(product: Product) -> str:
    lines = [
        product.title,
        "",
        product.description,
        "",
        product.price,
        product.link,
        f"商品ID: {product.id}",
    ]
    caption = "\n".join(line for line in lines if line is not None).strip()
    return caption[:2200]


def graph_request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        data=data,
        timeout=60,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"Instagram API {method} {path} failed: {payload}")
    return payload


def extract_product_ids(text: str) -> set[str]:
    ids = set(re.findall(r"(?:商品ID|product_id)\s*[:：]\s*(\d{6,12})", text))
    ids.update(re.findall(r"/items/(\d{6,12})", text))
    return ids


def sync_existing_posts(ig_user_id: str, token: str, state: dict[str, Any], limit: int) -> int:
    added = 0
    fetched = 0
    path = f"{ig_user_id}/media"
    params: dict[str, Any] = {
        "fields": "id,caption,permalink,timestamp",
        "limit": min(max(limit, 1), 100),
    }

    while fetched < limit:
        payload = graph_request("GET", path, token, params=params)
        for media in payload.get("data", []):
            fetched += 1
            text = "\n".join(
                str(media.get(key, "")) for key in ("caption", "permalink")
            )
            for product_id in extract_product_ids(text):
                if product_id not in state["posted_ids"]:
                    state["posted_ids"][product_id] = {
                        "media_id": media.get("id"),
                        "source": "instagram_existing",
                        "synced_at": datetime.now(timezone.utc).isoformat(),
                    }
                    added += 1
            if fetched >= limit:
                break

        after = payload.get("paging", {}).get("cursors", {}).get("after")
        if not after or fetched >= limit:
            break
        params["after"] = after

    return added


def publish_product(ig_user_id: str, token: str, product: Product) -> str:
    container = graph_request(
        "POST",
        f"{ig_user_id}/media",
        token,
        data={
            "image_url": product.image_url,
            "caption": caption_for(product),
        },
    )
    creation_id = container["id"]

    # Image containers are normally ready quickly, but a short pause avoids
    # racing Meta's container processing in workflow runs.
    time.sleep(5)

    published = graph_request(
        "POST",
        f"{ig_user_id}/media_publish",
        token,
        data={"creation_id": creation_id},
    )
    return published["id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed", type=Path, default=FEED_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--max-posts", type=int, default=1)
    parser.add_argument("--sync-existing", action="store_true")
    parser.add_argument("--sync-limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.publish and args.dry_run:
        print("[ERROR] Use either --publish or --dry-run, not both.")
        return 2
    publish = bool(args.publish)

    products = load_products(args.feed)
    state = load_state(args.state)
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    ig_user_id = os.environ.get("IG_USER_ID", "")

    if (publish or args.sync_existing) and (not token or not ig_user_id):
        print("[ERROR] IG_ACCESS_TOKEN and IG_USER_ID are required.")
        return 2

    if args.sync_existing:
        added = sync_existing_posts(ig_user_id, token, state, args.sync_limit)
        print(f"[INFO] Synced {added} existing Instagram product ids.")

    posted_ids = set(state["posted_ids"].keys())
    candidates = [
        p
        for p in products
        if p.id not in posted_ids and p.availability.lower() == "in stock"
    ]

    print(f"[INFO] Feed products: {len(products)}")
    print(f"[INFO] Already posted ids: {len(posted_ids)}")
    print(f"[INFO] New in-stock candidates: {len(candidates)}")

    selected = candidates[: max(args.max_posts, 0)]
    if not selected:
        save_state(args.state, state)
        print("[OK] Nothing new to post.")
        return 0

    for product in selected:
        print(f"[POST] {product.id}: {product.title}")
        if publish:
            media_id = publish_product(ig_user_id, token, product)
            state["posted_ids"][product.id] = {
                "media_id": media_id,
                "source": "instagram_publish",
                "posted_at": datetime.now(timezone.utc).isoformat(),
                "title": product.title,
                "link": product.link,
            }
            print(f"  -> published media_id={media_id}")
        else:
            print("  -> dry-run, not published")

    if publish or args.sync_existing:
        save_state(args.state, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
