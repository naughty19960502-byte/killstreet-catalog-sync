#!/usr/bin/env python3
"""
KILLSTREET BASE Shop → Instagram/Meta カタログ XML フィード
自動スクレイピング & AI説明文リライトシステム

Usage: python update_by_scraping.py [--dry-run]
Output: docs/feed.xml
"""

import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Anthropic は任意（APIキーがなければルールベースでフォールバック）
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# ─── 設定 ────────────────────────────────────────────────────────────────
SHOP_BASE_URL   = "https://killstreet2.base.shop"
ITEMS_ALL_URL   = "https://killstreet2.base.shop/items/all"
BRAND           = "KILLSTREET"
FEED_TITLE      = "KILLSTREET Official Shop"
FEED_LINK       = SHOP_BASE_URL
FEED_DESC       = "KILLSTREET Official Products - Street Wear & Apparel"
OUTPUT_PATH     = Path("docs/feed.xml")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY   = (1.5, 3.0)   # スクレイピング間隔（礼儀として）
MAX_RETRIES     = 3
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"   # コスト重視

# ─── HTTP ────────────────────────────────────────────────────────────────

def get_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    return sess


def fetch(session: requests.Session, url: str) -> str | None:
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.RequestException as e:
            print(f"[WARN] fetch {url} ({attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
    return None


# ─── スクレイピング ──────────────────────────────────────────────────────

def get_product_ids(session: requests.Session) -> list[str]:
    """トップ + /items/all から商品IDを収集"""
    ids = set()
    urls = [SHOP_BASE_URL, ITEMS_ALL_URL]
    for url in urls:
        html = fetch(session, url)
        if not html:
            continue
        matches = re.findall(r'/items/(\d{6,12})', html)
        ids.update(matches)
        time.sleep(random.uniform(*REQUEST_DELAY))
    ids_list = sorted(ids, key=lambda x: int(x), reverse=True)
    print(f"[INFO] 商品ID: {len(ids_list)} 件検出")
    return ids_list


def extract_jsonld(html: str) -> dict | None:
    """JSON-LD (Schema.org Product) を抽出"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return item
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def scrape_product(session: requests.Session, product_id: str) -> dict | None:
    """1商品のデータを取得"""
    url  = f"{SHOP_BASE_URL}/items/{product_id}"
    html = fetch(session, url)
    if not html:
        return None

    ld = extract_jsonld(html)
    if not ld:
        # フォールバック: BeautifulSoupで直接パース
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.find("h1") or soup.find("h2") or {}).get_text(strip=True)
        price_el = soup.find(string=re.compile(r'¥[\d,]+'))
        return {
            "id":          product_id,
            "title":       title or f"KILLSTREET Item {product_id}",
            "description": "",
            "price":       re.sub(r'[^\d]', '', str(price_el)) if price_el else "0",
            "currency":    "JPY",
            "availability":"in stock",
            "image_url":   "",
            "product_url": url,
        }

    # JSON-LDから取得
    offers = ld.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    availability_raw = offers.get("availability", "")
    availability = (
        "in stock" if "InStock" in availability_raw else "out of stock"
    )

    images = ld.get("image", [])
    if isinstance(images, str):
        images = [images]
    image_url = images[0] if images else ""

    return {
        "id":          product_id,
        "title":       re.sub(r'&quot;', '"', ld.get("name", "")),
        "description": ld.get("description", ""),
        "price":       offers.get("price", "0"),
        "currency":    offers.get("priceCurrency", "JPY"),
        "availability": availability,
        "image_url":   image_url,
        "product_url": url,
    }


# ─── AI 説明文リライト ────────────────────────────────────────────────────

def rewrite_with_ai(client, raw_desc: str, title: str) -> str:
    """Claude APIで説明文をInstagram向けにリライト"""
    if not raw_desc.strip():
        raw_desc = title

    prompt = f"""あなたはストリートウェアブランド「KILLSTREET」の公式SNSライターです。
以下の商品説明を、Instagramカタログ向けに最適化してください。

ルール:
- 100文字以内の日本語
- エッジの効いたストリート感、反骨精神、力強さを表現
- ハッシュタグ・絵文字・URLは含めない
- 商品の本質的な価値を一言で刺す
- 英語フレーズを1つ入れると尚良し

商品名: {title}
元の説明: {raw_desc}

リライト後の説明文のみ出力:"""

    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"[WARN] AI rewrite failed: {e}")
        return fallback_rewrite(raw_desc, title)


def fallback_rewrite(raw_desc: str, title: str) -> str:
    """APIキーなしの場合のルールベースリライト"""
    text = raw_desc.strip()
    if not text:
        return f"{title} — KILLSTREET Official."

    # 先頭1〜2文を抽出
    sentences = re.split(r'[。．\.\n]', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
    headline  = sentences[0] if sentences else title

    # 100文字以内にカット
    if len(headline) > 95:
        headline = headline[:92] + "..."

    return headline


# ─── XML 生成 ────────────────────────────────────────────────────────────

def escape_xml(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;")
             .replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;"))


def generate_xml(products: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
        "  <channel>",
        f"    <title>{escape_xml(FEED_TITLE)}</title>",
        f"    <link>{FEED_LINK}</link>",
        f"    <description>{escape_xml(FEED_DESC)}</description>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
    ]
    for p in products:
        price_str = f"{p['price']} {p['currency']}"
        lines += [
            "    <item>",
            f"      <g:id>{escape_xml(p['id'])}</g:id>",
            f"      <g:title>{escape_xml(p['title'])}</g:title>",
            f"      <g:description>{escape_xml(p['description'])}</g:description>",
            f"      <g:availability>{p['availability']}</g:availability>",
            f"      <g:condition>new</g:condition>",
            f"      <g:price>{price_str}</g:price>",
            f"      <g:link>{escape_xml(p['product_url'])}</g:link>",
        ]
        if p.get("image_url"):
            lines.append(f"      <g:image_link>{escape_xml(p['image_url'])}</g:image_link>")
        lines += [
            f"      <g:brand>{BRAND}</g:brand>",
            "    </item>",
        ]
    lines += ["  </channel>", "</rss>"]
    return "\n".join(lines)


# ─── メイン ──────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    # Anthropicクライアント初期化
    ai_client = None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if HAS_ANTHROPIC and api_key:
        ai_client = anthropic.Anthropic(api_key=api_key)
        print("[INFO] AI rewrite: Claude API enabled")
    else:
        print("[INFO] AI rewrite: fallback mode (no API key)")

    session = get_session()

    # 商品ID収集
    product_ids = get_product_ids(session)
    if not product_ids:
        print("[ERROR] 商品IDを取得できませんでした")
        sys.exit(1)

    # 各商品をスクレイピング
    products = []
    for i, pid in enumerate(product_ids, 1):
        print(f"[{i}/{len(product_ids)}] Scraping item {pid}...")
        try:
            product = scrape_product(session, pid)
        except Exception as e:
            print(f"  -> SKIP (unexpected error: {e})")
            continue
        if not product:
            print(f"  -> SKIP (fetch failed)")
            continue

        # 説明文リライト
        raw = product["description"]
        if ai_client:
            product["description"] = rewrite_with_ai(ai_client, raw, product["title"])
        else:
            product["description"] = fallback_rewrite(raw, product["title"])

        products.append(product)
        print(f"  -> OK: {product['title'][:40]} | {product['availability']}")

        if not dry_run:
            time.sleep(random.uniform(*REQUEST_DELAY))

    if not products:
        print("[ERROR] 有効な商品データがありません")
        sys.exit(1)

    # XML出力
    if not dry_run:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        xml_content = generate_xml(products)
        OUTPUT_PATH.write_text(xml_content, encoding="utf-8")
        print(f"\n[OK] {OUTPUT_PATH} 生成完了 ({len(products)} 商品)")
    else:
        print(f"\n[DRY-RUN] {len(products)} 商品を処理 (XML未出力)")
        for p in products[:3]:
            print(f"  - {p['id']}: {p['title'][:50]}")
            print(f"    desc: {p['description'][:80]}")


if __name__ == "__main__":
    main()
