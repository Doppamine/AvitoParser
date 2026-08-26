"""Фаза 0: проверка, как снимаются цены с объявлений Avito.

Скрипт ничего не решает за нас — он собирает факты:
где в странице лежит цена, отдаётся ли она без браузера,
меняется ли от дат и сколько заходов проходит без блокировки.

Осознанно НЕ выбирает «правильную» цену. Витринная «от» и цена на конкретные
даты — разные числа, обычно оба есть на одной странице. Отличить их можно
только сверив со скриншотом глазами, поэтому скрипт предъявляет всех кандидатов
с контекстом, откуда каждый взят.

Работает через постоянный профиль браузера в out/profile: окно настоящее,
видимое, проверку площадки человек проходит руками один раз, дальше профиль
переиспользуется. Это не обход защиты: капчу решает человек, прокси не
используются, отпечаток не подменяется — наоборот, браузер представляется
самим собой.

Установка:
    python3.12 -m venv .venv
    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/playwright install chromium

Порядок запуска:
    .venv/bin/python scripts/probe.py --warmup          # один раз: пройти проверку руками
    .venv/bin/python scripts/probe.py --loop 5 --pause 60    # работает ли схема вообще
    .venv/bin/python scripts/probe.py --loop 20 --pause 300  # доля отказов

Ссылки берутся из scripts/urls.txt (одна на строку, # — комментарий).

Результаты:
    out/probe.csv           — таблица исходов
    out/dumps/*.html        — сырые страницы (фикстуры для тестов парсера, фаза 2)
    out/shots/*.png         — скриншоты для сверки глазами
    out/candidates/*.json   — кандидаты в цену с контекстом
    out/profile/            — профиль браузера, переиспользуется между запусками
"""

import argparse
import csv
import html as H
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

# ------------------------------------------------------------------ настройки

URLS_FILE = Path("scripts/urls.txt")

OUT = Path("out")
DUMPS = OUT / "dumps"
SHOTS = OUT / "shots"
CANDS = OUT / "candidates"
PROFILE = OUT / "profile"
CSV_PATH = OUT / "probe.csv"

# Медленнее, чем требует CLAUDE.md (там 3-6 с). Осознанно: холодные заходы
# упирались в проверку, и темп — единственный рычаг, который нам разрешён.
PAUSE_MIN, PAUSE_MAX = 15.0, 30.0

HOME = "https://www.avito.ru/"

CAPTCHA_MARKERS = [
    "подтвердите, что вы не робот", "доступ ограничен", "проблема с ip",
    "firewall", "captcha", "капч", "проверка браузера", "слишком много запросов",
]
GONE_MARKERS = [
    "объявление снято с публикации", "объявление больше не доступно",
    "страница не найдена",
]

# Написания дат в адресе для режима --date-test.
# Список эмпирический: какое из них работает — предмет вопроса 3.
DATE_PARAM_VARIANTS = [
    ("checkIn/checkOut", "checkIn={ci}&checkOut={co}"),
    ("check_in/check_out", "check_in={ci}&check_out={co}"),
    ("dateFrom/dateTo", "dateFrom={ci}&dateTo={co}"),
    ("checkInDate/checkOutDate", "checkInDate={ci}&checkOutDate={co}"),
]

# ------------------------------------------------------------------ поиск цены


def _context(html: str, pos: int, width: int = 80) -> str:
    """Кусок страницы вокруг находки — без него число ничего не значит."""
    chunk = html[max(0, pos - width): pos + width]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk)).strip()


RU_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
             "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def normalize(html: str) -> str:
    """Состояние приложения вшито в HTML экранированным, разряды отбиты &nbsp;.

    Без этой нормализации шаблоны молча не находят ничего — а «ничего не найдено»
    и «цены на странице нет» это разные вещи, и путать их нельзя.
    """
    return H.unescape(html.replace('\\"', '"').replace("\\u0026", "&"))


def parse_nearest_dates(html: str) -> list[dict]:
    """Блок «ближайшие даты»: суммы ЗА ДИАПАЗОН, не за ночь.

    Даты выбирает площадка, а не мы: у части объявлений это ближайшие свободные,
    отстоящие на месяц. Число ночей считаем из подписи — это арифметика, а не догадка.
    """
    i = html.find('data-marker="nearest-dates"')
    if i < 0:
        return []
    seg = re.sub(r"[\s\u2060\u00a0]+", " ",
                 H.unescape(re.sub(r"<[^>]+>", " ", html[i:i + 4000])))
    out = []
    for m in re.finditer(r"([\d ]{3,9})\u20bd\s*(\d{1,2}(?:\s*[а-я]+)?\s*[—–-]\s*\d{1,2}\s*[а-я]+)", seg):
        total = re.sub(r"\D", "", m.group(1))
        label = m.group(2).strip()
        out.append({"source": "nearest_dates", "value": total,
                    "context": f"итог за диапазон {label}",
                    "label": label, "nights": nights_of(label)})
    return out


def nights_of(label: str) -> int | None:
    """Число ночей из подписи диапазона: «27—28 августа», «31 авг—1 сент»."""
    m = re.match(r"(\d{1,2})\s*([а-я]*)\s*[—–-]\s*(\d{1,2})\s*([а-я]*)", label)
    if not m:
        return None
    d1, m1, d2, m2 = int(m.group(1)), m.group(2)[:3], int(m.group(3)), m.group(4)[:3]
    mo2 = RU_MONTHS.get(m2)
    mo1 = RU_MONTHS.get(m1, mo2)
    if not mo1 or not mo2:
        return None
    year = datetime.now().year
    try:
        a = datetime(year + (1 if mo1 < datetime.now().month - 6 else 0), mo1, d1)
        b = datetime(year + (1 if mo2 < datetime.now().month - 6 else 0), mo2, d2)
    except ValueError:
        return None
    n = (b - a).days
    return n if 0 < n < 60 else None


def find_prices(html: str) -> list[dict]:
    """Все места, где в странице похоже на цену, с указанием источника.

    Задача не выбрать правильную, а показать, что вообще есть. На странице
    сосуществуют витринная цена «от N за сутки» и суммы на конкретные даты —
    это разные числа, и выбирает между ними человек по скриншоту.
    """
    norm = normalize(html)
    found: list[dict] = []

    def add(source, value, pos, note="", src_text=None):
        value = re.sub(r"\D", "", str(value))
        if value and 100 <= int(value) <= 10_000_000:
            found.append({"source": source, "value": value,
                          "context": _context(src_text or norm, pos), "note": note})

    # 1. JSON-LD — самый стабильный источник, если он есть
    for block in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block.group(1).strip())
        except Exception:
            continue
        dump = json.dumps(data, ensure_ascii=False)
        for m in re.finditer(r'"price"\s*:\s*"?([\d.]+)"?', dump):
            add("ld_json", m.group(1), block.start(), note=dump[:200], src_text=html)

    # 2. Состояние приложения. Витринная цена лежит строкой целиком — вместе
    #    с «от» и «за сутки», и эти слова важнее самого числа.
    for key in ("priceString", "formatedString", "normalizedPrice"):
        for m in re.finditer(rf'"{key}"\s*:\s*"([^"]{{0,60}})"', norm):
            raw = m.group(1)
            if re.search(r"\d", raw):
                add(f"state:{key}", raw, m.start(), note=raw.strip())

    for m in re.finditer(r'"(price[A-Za-z]*|[a-zA-Z]+Price)"\s*:\s*"?(\d{3,7})"?', norm):
        add(f"state:{m.group(1)}", m.group(2), m.start())

    # 3. Микроразметка
    for m in re.finditer(
            r'itemprop=["\']price["\'][^>]*content=["\']([\d.]+)["\']', html):
        add("itemprop", m.group(1), m.start(), src_text=html)

    # 4. Блок ближайших дат — суммы за диапазон
    found += parse_nearest_dates(html)

    # 5. Видимый текст с рублём — самый ненадёжный, но показывает порядок величины
    for m in re.finditer(r"([\d][\d\s\u00a0\u2009]{2,9})\s*(?:\u20bd|руб)", norm):
        add("visible_text", m.group(1), m.start())

    seen, uniq = set(), []
    for f in found:
        key = (f["source"], f["value"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def find_min_nights(html: str) -> list[dict]:
    """Кандидаты в минимальный срок проживания (вопрос 4).

    Без него цены несравнимы: у нас 2 ночи, у многих конкурентов 3.
    """
    html = normalize(html)
    out = []
    patterns = [
        (r'"min[A-Za-z]*(?:Nights|Days|Period)"\s*:\s*"?(\d{1,3})"?', "state"),
        (r'от\s+(\d{1,3})\s*(?:сут|ноч|дн)', "text_ot"),
        (r'минимальн\w*\s+срок\D{0,30}?(\d{1,3})', "text_min"),
        (r'на\s+срок\s+от\s+(\d{1,3})', "text_srok"),
    ]
    for pat, tag in patterns:
        for m in re.finditer(pat, html, re.I):
            out.append({"source": tag, "value": m.group(1),
                        "context": _context(html, m.start(), 60)})
    seen, uniq = set(), []
    for o in out:
        key = (o["source"], o["value"])
        if key not in seen:
            seen.add(key)
            uniq.append(o)
    return uniq


def find_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


# Проверка приходит не только вместо страницы, но и поверх загруженной —
# отдельным окном поверх объявления. В первом прогоне такой случай был принят
# за успех, потому что смотрелись только первые 8000 символов.
OVERLAY_MARKERS = ["доступ ограничен", "проблема с ip", "сервис недоступен"]


def is_check_page(html: str, title: str = "") -> str:
    """Страница проверки площадки. Пустая строка — значит не она."""
    low = (html[:8000] + " " + title).lower()
    for marker in CAPTCHA_MARKERS:
        if marker in low:
            return marker
    return ""


def has_check_overlay(html: str) -> str:
    """Окно проверки поверх загруженного объявления. Ищем по всей странице."""
    low = html.lower()
    for marker in OVERLAY_MARKERS:
        if marker in low:
            return marker
    return ""


def classify(html: str, http_status, prices: list, title: str = "",
             exc: str = "") -> tuple[str, str]:
    """Исход захода. Разные виды отказа нельзя сваливать в один «blocked»:
    без этого статистика длинного прогона (вопрос 6) ничего не значит."""
    if exc:
        # Текст исключения Playwright многострочный; в CSV это законно, но глазами
        # такую таблицу не прочесть.
        return ("timeout" if "imeout" in exc else "error"), " ".join(exc.split())[:120]

    is_listing = 'data-marker="item-view' in html
    marker = is_check_page(html, title)
    if marker and not is_listing:
        return "captcha", marker
    overlay = has_check_overlay(html)
    if overlay:
        # Объявление пришло, но поверх него проверка: витринная цена в состоянии
        # есть, а цен по датам не будет — их подгружает отдельный запрос.
        return "captcha_overlay", f"{overlay} поверх объявления"
    if http_status in (403, 429):
        return f"http_{http_status}", "площадка отказала"
    if http_status == 404:
        return "not_found", "страница не найдена"
    low = html[:8000].lower()
    for m in GONE_MARKERS:
        if m in low:
            return "not_available", m
    # Порядок важен: найденные цены сами по себе доказывают, что страница пришла,
    # поэтому проверка на короткий ответ идёт после них, а не до.
    #
    # «ok» ставится только если сработал надёжный источник. Если число нашлось
    # лишь в видимом тексте — это может оказаться залогом или доплатой за уборку,
    # что и случилось на первом прогоне. Такой исход помечается отдельно.
    reliable = [p for p in prices if not p["source"].startswith("visible_text")]
    if reliable:
        return "ok", ""
    if prices:
        return "price_unreliable", "число нашлось только в видимом тексте"
    if len(html) < 2000:
        return "empty", f"ответ {len(html)} байт"
    if 'data-marker="item-view' not in html:
        return "wrong_page", "пришла не страница объявления"
    return "no_price", "страница объявления пришла, цены не найдено"


# ------------------------------------------------------------------ три способа


def _record(row, tag, html, prices, nights):
    (DUMPS / f"{tag}.html").write_text(html, encoding="utf-8")
    (CANDS / f"{tag}.json").write_text(json.dumps(
        {"url": row["url"], "method": row["method"], "title": row["title"],
         "outcome": row["outcome"], "prices": prices, "min_nights": nights},
        ensure_ascii=False, indent=2), encoding="utf-8")


def _fill(row, html, status, title):
    prices, nights = find_prices(html), find_min_nights(html)
    row["status"] = status
    row["title"] = title
    row["outcome"], row["note"] = classify(html, status, prices, title)
    row["n_prices"], row["n_nights"] = len(prices), len(nights)
    row["prices"] = json.dumps([p["value"] for p in prices][:10], ensure_ascii=False)
    return prices, nights


def via_http(url: str, tag: str, ua: str, cookies: dict | None) -> dict:
    """Ступень 1: обычный запрос без браузера.

    Вызывается дважды: начисто и с куками из прогретого профиля. Если цена
    приходит начисто — Playwright на сервере не нужен вообще. Если только
    с куками — браузер нужен, но лишь изредка, для получения сессии.
    """
    method = "http_ck" if cookies else "http"
    row = {"method": method, "url": url, "title": "", "n_prices": 0, "n_nights": 0}
    try:
        r = httpx.get(url, headers={"User-Agent": ua, "Accept-Language": "ru-RU,ru;q=0.9"},
                      cookies=cookies or {}, timeout=25, follow_redirects=True)
        html = r.text
        prices, nights = _fill(row, html, r.status_code, find_title(html))
        _record(row, f"{tag}_{method}", html, prices, nights)
    except Exception as e:
        row["status"] = ""
        row["outcome"], row["note"] = classify("", None, [], exc=f"{type(e).__name__}: {e}")
        row["prices"] = ""
    return row


def via_browser(page, url: str, tag: str, solve: bool = False) -> dict:
    """Ступень 2: настоящий браузер с постоянным профилем."""
    row = {"method": "browser", "url": url, "title": "", "n_prices": 0, "n_nights": 0}
    try:
        # Ждём только первый байт. Страница проверки отдаётся с кодом 403/429
        # мгновенно, но не дозагружается никогда — ожидание полной загрузки
        # превращает явный отказ в таймаут и теряет причину.
        resp = page.goto(url, wait_until="commit", timeout=30000)
        status = resp.status if resp else None
        if status not in (403, 429):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass  # частично загруженную страницу разберём как есть
            page.wait_for_timeout(3000)  # даём догрузиться цене
        html, title = page.content(), page.title()[:120]

        if solve and is_check_page(html, title):
            wait_for_human("Появилась проверка площадки. Пройдите её в окне браузера")
            page.wait_for_timeout(2000)
            html, title = page.content(), page.title()[:120]

        prices, nights = _fill(row, html, status if status is not None else "", title)
        _record(row, f"{tag}_browser", html, prices, nights)
        # Два снимка: полный — чтобы найти цену глазами и сверить её с кандидатами,
        # видимый — чтобы понять, что показывается первым экраном.
        page.screenshot(path=str(SHOTS / f"{tag}.png"))
        page.screenshot(path=str(SHOTS / f"{tag}_full.png"), full_page=True)
    except Exception as e:
        row["status"] = ""
        row["outcome"], row["note"] = classify("", None, [], exc=f"{type(e).__name__}: {e}")
        row["prices"] = ""
    return row


# ------------------------------------------------------------------ прогон


def pause():
    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))


def wait_for_human(text: str):
    print(f"\n>>> {text}, затем нажмите Enter здесь. ", end="", flush=True)
    try:
        input()
    except EOFError:
        # Запуск без терминала: ждать некого, просто идём дальше.
        print("(нет терминала, продолжаю)")


def load_urls() -> list[str]:
    if not URLS_FILE.exists():
        raise SystemExit(
            f"Нет файла {URLS_FILE}. Положите туда ссылки на объявления, "
            f"по одной на строку. Без реальных ссылок фаза 0 не выполняется.")
    raw = [ln.strip() for ln in URLS_FILE.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.strip().startswith("#")]
    if not raw:
        raise SystemExit(f"{URLS_FILE} пуст.")

    # Ссылки копируются руками из браузера, поэтому схему дописываем, но вслух:
    # молча исправленный адрес — это адрес, за который никто не отвечает.
    urls = []
    for ln in raw:
        url = ln if ln.startswith("http") else "https://" + ln
        if url != ln:
            print(f"ВНИМАНИЕ: дописана схема — {url[:60]}…")
        if "avito.ru" not in url:
            raise SystemExit(f"Не похоже на ссылку Avito: {ln[:80]}")
        urls.append(url)
    print(f"ссылок загружено: {len(urls)}")
    return urls


def open_browser(p, headed: bool):
    """Постоянный профиль: проверка, пройденная руками, переживает перезапуск.

    User-agent и часовой пояс не подменяем — браузер представляется собой,
    настройки только приводят его к московскому посетителю.
    """
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=not headed,
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1280, "height": 900},
    )
    # Профиль восстанавливает вкладки прошлого запуска. Работать в такой вкладке
    # нельзя: один раз прогон намертво завис на первом же обращении к ней.
    # Открываем свою и закрываем восстановленные.
    page = ctx.new_page()
    for other in ctx.pages:
        if other is not page:
            try:
                other.close()
            except Exception:
                pass
    ctx.set_default_timeout(45000)
    ctx.set_default_navigation_timeout(60000)
    return ctx, page


def warmup(page):
    """Один раз при первом запуске: человек проходит проверку, профиль её запоминает."""
    page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    marker = is_check_page(page.content(), page.title())
    if marker:
        print(f"Проверка площадки на входе: {marker!r}")
        wait_for_human("Пройдите проверку в окне браузера")
    else:
        print("Проверки нет, площадка открылась сразу.")
    page.wait_for_timeout(1500)
    still = is_check_page(page.content(), page.title())
    print("Итог прогрева:", "проверка всё ещё висит" if still else "проверка пройдена")
    print(f"Профиль сохранён в {PROFILE}")


def cookies_of(ctx) -> dict:
    return {c["name"]: c["value"] for c in ctx.cookies()
            if "avito" in c.get("domain", "")}


def run_pass(ctx, page, urls, n: int, writer, fh, ua: str, solve: bool, plain_http: bool):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, url in enumerate(urls):
        tag = f"{stamp}_p{n}_u{i}"
        print(f"  → объявление {i + 1}/{len(urls)}", flush=True)
        rows = [via_browser(page, url, tag, solve=solve)]
        if plain_http:
            pause()
            rows.append(via_http(url, tag, ua, None))
            pause()
            rows.append(via_http(url, tag, ua, cookies_of(ctx)))
        for row in rows:
            row["time"] = datetime.now().strftime("%H:%M:%S")
            row["pass"] = n
            writer.writerow(row)
            fh.flush()
            print(f"  [{row['method']:8}] {str(row['status']):>4} {row['outcome']:12} "
                  f"цен:{row['n_prices']:<3} срок:{row['n_nights']:<3} {row['prices'][:60]}",
                  flush=True)
        pause()


def run_date_test(page, urls, check_in: str, check_out: str, writer, fh, solve: bool):
    """Вопрос 3: управляется ли цена датами через адрес страницы."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, url in enumerate(urls[:2]):  # двух ссылок достаточно, объём бережём
        variants = [("базовый", url)] + [
            (name, f"{url}{'&' if '?' in url else '?'}"
                   + tpl.format(ci=check_in, co=check_out))
            for name, tpl in DATE_PARAM_VARIANTS]
        for j, (name, variant_url) in enumerate(variants):
            tag = f"{stamp}_date_u{i}_v{j}"
            row = via_browser(page, variant_url, tag, solve=solve)
            row["time"] = datetime.now().strftime("%H:%M:%S")
            row["pass"] = f"date:{name}"
            writer.writerow(row)
            fh.flush()
            print(f"  [{name:24}] {row['outcome']:12} {row['prices'][:60]}")
            pause()



def run_offline():
    """Перебрать сохранённые дампы заново, без единого обращения к площадке.

    Нужен потому, что шаблоны поиска правятся чаще, чем стоит ходить на площадку:
    дампы фазы 0 — те же фикстуры, на которых будет стоять парсер фазы 2.
    """
    files = sorted(DUMPS.glob("*.html"))
    if not files:
        raise SystemExit(f"В {DUMPS} нет дампов.")
    for f in files:
        html = f.read_text(encoding="utf-8")
        prices, nights = find_prices(html), find_min_nights(html)
        title = find_title(html)
        outcome, note = classify(html, 200, prices, title)
        print(f"\n=== {f.name}  ({len(html):,} байт)  {outcome} ===")
        print(f"    заголовок: {title[:70]}")
        for p in prices:
            extra = f"  [{p['label']}, ночей: {p.get('nights')}]" if p.get("label") else ""
            print(f"    {p['source']:22} {p['value']:>8}{extra}   …{p['context'][:60]}…")
        print(f"    минимальный срок: "
              f"{[n['value'] for n in nights] if nights else 'НЕ НАЙДЕН'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warmup", action="store_true",
                    help="открыть площадку и дать человеку пройти проверку")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="сколько проходов")
    ap.add_argument("--pause", type=int, default=300, help="пауза между проходами, сек")
    ap.add_argument("--date-test", action="store_true", help="вопрос 3: даты в адресе")
    ap.add_argument("--check-in", default="", help="дата заезда ГГГГ-ММ-ДД")
    ap.add_argument("--check-out", default="", help="дата выезда ГГГГ-ММ-ДД")
    ap.add_argument("--headless", action="store_true",
                    help="без окна; по умолчанию окно видимое — так проверка приходит реже")
    ap.add_argument("--solve", action="store_true",
                    help="останавливаться и ждать, пока человек пройдёт проверку")
    ap.add_argument("--offline", action="store_true",
                    help="перебрать уже сохранённые дампы, не обращаясь к площадке")
    ap.add_argument("--no-http", action="store_true",
                    help="только браузер, без проверки обычного HTTP")
    args = ap.parse_args()

    for d in (OUT, DUMPS, SHOTS, CANDS):
        d.mkdir(parents=True, exist_ok=True)

    if args.offline:
        run_offline()
        return

    urls = [] if args.warmup else load_urls()

    fields = ["time", "pass", "method", "url", "status", "outcome", "note",
              "title", "n_prices", "n_nights", "prices"]
    new = not CSV_PATH.exists()
    fh = CSV_PATH.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
    if new:
        writer.writeheader()

    with sync_playwright() as p:
        print("браузер: запуск…", flush=True)
        ctx, page = open_browser(p, headed=not args.headless)
        print("браузер: окно готово", flush=True)
        ua = page.evaluate("() => navigator.userAgent")  # тот же, что у окна
        print(f"браузер: {ua[:60]}…", flush=True)

        try:
            if args.warmup:
                warmup(page)
            elif args.date_test:
                if not (args.check_in and args.check_out):
                    raise SystemExit("для --date-test нужны --check-in и --check-out")
                print(f"\n=== даты в адресе: {args.check_in} → {args.check_out} ===")
                run_date_test(page, urls, args.check_in, args.check_out, writer, fh, args.solve)
            else:
                passes = 1 if args.once or args.loop == 0 else args.loop
                for n in range(1, passes + 1):
                    print(f"\n=== проход {n}/{passes} ===")
                    run_pass(ctx, page, urls, n, writer, fh, ua,
                             solve=args.solve, plain_http=not args.no_http)
                    if n < passes:
                        print(f"    пауза {args.pause} сек")
                        time.sleep(args.pause)
        finally:
            ctx.close()

    fh.close()
    print(f"\nГотово. Таблица: {CSV_PATH}, дампы: {DUMPS}, "
          f"скриншоты: {SHOTS}, кандидаты: {CANDS}")


if __name__ == "__main__":
    main()
