"""Сбор ссылок на объявления с выдачи Avito — вспомогательный скрипт фазы 0.

Нужен потому, что в probe.py стояли заглушки, а поисковые системы
конкретные объявления Avito не отдают.

Заход анонимный, как у обычного посетителя: без авторизации, без прокси,
без подмены отпечатка. Пауза 3-6 секунд, страницы открываются последовательно.

Запуск:
    .venv/bin/python scripts/find_urls.py
"""

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("out")
DUMPS = OUT / "dumps"
SHOTS = OUT / "shots"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Разные срезы выдачи, чтобы попались объявления разных типов:
# обычное посуточное, и отдельно раздел Авито Путешествий.
SEARCHES = [
    ("posutochno_all", "https://www.avito.ru/moskva/kvartiry/sdam/posutochno"),
    ("posutochno_1k", "https://www.avito.ru/moskva/kvartiry/sdam/posutochno/1-komnatnye"),
]

# Ссылка на объявление у Avito всегда заканчивается числовым идентификатором.
LISTING_RE = re.compile(r'/moskva/kvartiry/[a-z0-9_,\-\.]+_(\d{6,})(?:\?|$)')


def pause():
    time.sleep(random.uniform(3, 6))


def collect(page, tag: str, url: str) -> list[dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    html = page.content()
    (DUMPS / f"{stamp}_search_{tag}.html").write_text(html, encoding="utf-8")
    page.screenshot(path=str(SHOTS / f"{stamp}_search_{tag}.png"), full_page=False)
    print(f"[{tag}] status={resp.status if resp else '?'} title={page.title()!r} len={len(html)}")

    items = []
    seen = set()
    for a in page.query_selector_all('a[itemprop="url"], a[data-marker="item-title"]'):
        href = a.get_attribute("href") or ""
        m = LISTING_RE.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        items.append({
            "id": m.group(1),
            "url": "https://www.avito.ru" + href.split("?")[0],
            "title": (a.inner_text() or "").strip()[:120],
        })
    return items


def main():
    for d in (OUT, DUMPS, SHOTS):
        d.mkdir(parents=True, exist_ok=True)

    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="ru-RU",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        for tag, url in SEARCHES:
            try:
                found += collect(page, tag, url)
            except Exception as e:
                print(f"[{tag}] ошибка: {type(e).__name__}: {e}")
            pause()
        browser.close()

    uniq = {i["id"]: i for i in found}
    (OUT / "found_urls.json").write_text(
        json.dumps(list(uniq.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nнайдено объявлений: {len(uniq)}")
    for i in list(uniq.values())[:30]:
        print(f"  {i['url']}\n      {i['title']}")


if __name__ == "__main__":
    main()
