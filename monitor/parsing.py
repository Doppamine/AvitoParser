"""Разбор сохранённой страницы объявления Avito.

Чистая функция: принимает HTML, возвращает структуру. В сеть не ходит —
иначе её нельзя проверить на дампах, а дампы здесь единственная защита
от ошибки, которую не видно по поведению системы.

Три вещи, ради которых модуль устроен именно так.

**Нормализация обязательна.** Состояние приложения вшито в HTML экранированным
(`\\"priceString\\":\\"…\\"`), разряды в числах отбиты `&nbsp;`, а подписи дат
склеены неразрывным пробелом и словосоединителем (`26⁠—⁠27 августа`).
Без нормализации шаблоны молча не находят ничего, и «не найдено» становится
неотличимо от «цены на странице нет».

**Витринная цена никогда не подменяет цену на даты.** Она возвращается отдельным
полем с пометкой источника и в `periods` не попадает ни при каких обстоятельствах.

**Молчаливых успехов не бывает.** Каждый исход, кроме `ok`, несёт причину.
Отсутствие блока дат и нечитаемый блок дат — разные исходы: первое означает,
что площадка не показала цены этому посетителю, второе — что сломался парсер.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

# --------------------------------------------------------------------- исходы


class ParseStatus(StrEnum):
    """Исход разбора страницы. В статус снимка превращает сборщик, не парсер."""

    OK = 'ok'
    # Страница цела, витринная цена есть, блока «Цены по датам» нет вовсе.
    # Блок под A/B-флагом площадки: показывается не всем и не всегда.
    NO_DATED_PRICES = 'no_dated_prices'
    # Блок есть, но из него не разобрался ни один период. Это поломка парсера,
    # а не решение площадки, и лечится она иначе.
    DATES_BLOCK_UNREADABLE = 'dates_block_unreadable'
    # Цена нашлась только в видимом тексте. Туда попадают залог, уборка
    # и комплект белья — все того же порядка величины, что и цена за ночь.
    PRICE_UNRELIABLE = 'price_unreliable'
    # Объявление загрузилось, проверка перекрыла блок цен: витринная цена
    # доступна, цен по датам нет. Внешне похоже на успех.
    CAPTCHA_OVERLAY = 'captcha_overlay'
    # Вместо страницы пришла заглушка проверки: содержимого нет вовсе.
    BLOCKED = 'blocked'
    NOT_AVAILABLE = 'not_available'
    # Ценовой объект и аналитика разошлись. Значит разметка изменилась,
    # и любое из чисел может оказаться чужим.
    STATE_CONFLICT = 'state_conflict'
    # Числа блока цены на период не сошлись между собой: сумма за период,
    # делённая на ночи, не совпала с ценой за сутки в той же разметке.
    PRICE_CONFLICT = 'price_conflict'
    FAILED = 'failed'


# ------------------------------------------------------------------ структуры


@dataclass(frozen=True)
class ParsedPeriod:
    """Один период из блока «Цены по датам». Сумма — за диапазон, не за ночь."""

    check_in: date
    nights: int
    total_price: Decimal
    price_per_night: Decimal
    label: str
    source: str = 'nearest_dates'
    # Цена до скидки за длительность и её подпись. В снимок не идут: сравнивать
    # надо те деньги, которые платит гость. Нужны в логе сбора, чтобы человек
    # сверил всю строку блока со скриншотом, а не одно число из трёх.
    base_price: Decimal | None = None
    discount_label: str = ''

    @property
    def check_out(self) -> date:
        return self.check_in + timedelta(days=self.nights)


@dataclass(frozen=True)
class ParsedPage:
    status: ParseStatus
    title: str | None = None
    # Витринная цена «от N ₽ за сутки». В сравнение не идёт никогда.
    listing_price: Decimal | None = None
    listing_price_source: str | None = None
    periods: list[ParsedPeriod] = field(default_factory=list)
    min_nights: int | None = None
    error_note: str = ''
    # Сколько карточек в блоке «Цены по датам». Нужно демонстрации: по нему
    # видно, все ли карточки попали в кадр, — полностраничный скриншот
    # обрезает карусель по ширине окна.
    cards_found: int = 0
    # Цена на запрошенный период из блока str-price-info. Она же лежит
    # в `periods`; отдельным полем — чтобы лог сбора показывал именно её.
    requested_period: ParsedPeriod | None = None

    @property
    def has_dated_prices(self) -> bool:
        return bool(self.periods)


# -------------------------------------------------------------- нормализация

# Словосоединитель U+2060 и неразрывный пробел U+00A0 стоят прямо внутри
# подписей дат: «26⁠—⁠27 августа». Их надо убрать до любых шаблонов.
_INVISIBLE = '⁠​﻿'
_SPACES = '    '

BLOCK_MARKERS = (
    'доступ ограничен',
    'проблема с ip',
    'подтвердите, что вы не робот',
    'слишком много запросов',
    'проверка браузера',
    'сервис недоступен',
)

GONE_MARKERS = (
    'объявление снято с публикации',
    'объявление больше не доступно',
    'страница не найдена',
)

RU_MONTHS = {
    'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'мая': 5, 'май': 5, 'июн': 6,
    'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12,
}

MAX_NIGHTS_IN_LABEL = 60


def normalize(text: str) -> str:
    """Снять экранирование и невидимые символы. Без этого шаблоны слепы."""
    text = text.replace('\\"', '"').replace('\\u0026', '&').replace('\\/', '/')
    text = html_lib.unescape(text)
    for char in _INVISIBLE:
        text = text.replace(char, '')
    for char in _SPACES:
        text = text.replace(char, ' ')
    return text


_SCRIPT_RE = re.compile(r'<(script|style)\b[^>]*>.*?</\1>', re.S | re.I)
_TAG_RE = re.compile(r'<[^>]+>')


def visible_text(html: str) -> str:
    """Текст, который видит человек: без скриптов и разметки.

    Скрипты вырезаются первыми и это не мелочь: состояние приложения содержит
    сотни A/B-флагов со словами вроде `captcha` и `recaptcha_enabled`, и поиск
    признаков блокировки по всему файлу дал бы блокировку на каждой странице.
    """
    body = _SCRIPT_RE.sub(' ', html)
    body = _TAG_RE.sub(' ', body)
    body = html_lib.unescape(body)
    for char in _INVISIBLE:
        body = body.replace(char, '')
    for char in _SPACES:
        body = body.replace(char, ' ')
    return re.sub(r'\s+', ' ', body)


# ------------------------------------------------------ состояние приложения


def _js_string_literal(html: str, marker: str) -> str | None:
    """Вырезать строковый литерал JS, идущий сразу за маркером.

    Литерал длиной в четверть мегабайта, внутри экранированные кавычки —
    поэтому границу ищем посимвольно по нечётности обратных слэшей,
    а не регуляркой.
    """
    start = html.find(marker)
    if start < 0:
        return None
    quote = html.find('"', start + len(marker))
    if quote < 0:
        return None
    pos = quote + 1
    while True:
        pos = html.find('"', pos)
        if pos < 0:
            return None
        backslashes = 0
        probe = pos - 1
        while probe >= 0 and html[probe] == '\\':
            backslashes += 1
            probe -= 1
        if backslashes % 2 == 0:
            return html[quote:pos + 1]
        pos += 1


def extract_state(html: str, marker: str) -> dict | None:
    """Состояние — это JSON внутри строкового литерала, то есть два разбора."""
    literal = _js_string_literal(html, marker)
    if literal is None:
        return None
    try:
        return json.loads(json.loads(literal))
    except (ValueError, TypeError):
        return None


HYDRATION_MARKER = 'window.__staticRouterHydrationData = JSON.parse('
PRELOADED_MARKER = 'window.__preloadedState__ ='


def extract_buyer_item(html: str) -> dict | None:
    """Объявление лежит только в __staticRouterHydrationData.

    В __preloadedState__ — оболочка страницы: A/B-флаги, регион, футер.
    Искать цену там бесполезно, и это проверено на всех дампах фазы 0.
    """
    state = extract_state(html, HYDRATION_MARKER)
    if not isinstance(state, dict):
        return None
    loader = state.get('loaderData')
    if not isinstance(loader, dict):
        return None
    for value in loader.values():
        if isinstance(value, dict) and isinstance(value.get('buyerItem'), dict):
            return value['buyerItem']
    return None


# ---------------------------------------------------------- витринная цена


def _digits_to_decimal(raw: str) -> Decimal | None:
    digits = re.sub(r'\D', '', normalize(str(raw)))
    if not digits:
        return None
    value = Decimal(digits)
    return value if 100 <= value <= Decimal('10000000') else None


def listing_price_candidates(buyer_item: dict) -> dict[str, Decimal]:
    """Все места, где лежит витринная цена, с путём до каждого.

    Независимых источников два: ценовой объект объявления и аналитическая
    нагрузка `ga`. Совпадение четырёх путей внутри ценового объекта запасом
    прочности не является — он либо разобрался целиком, либо не разобрался
    вовсе. Поправка от 29.08, см. DECISIONS.md.
    """
    found: dict[str, Decimal] = {}

    def take(path: str, raw) -> None:
        if raw in (None, ''):
            return
        value = _digits_to_decimal(raw)
        if value is not None:
            found[path] = value

    item = buyer_item.get('item') or {}
    formatted = item.get('formattedPrice') or {}

    take('state:priceString', buyer_item.get('priceString'))
    take('state:formattedPrice.formatedString', formatted.get('formatedString'))
    take('state:formattedPrice.value', formatted.get('value'))
    take('state:item.price', item.get('price'))

    for entry in buyer_item.get('ga') or []:
        if not isinstance(entry, dict):
            continue
        take('ga:itemPrice', entry.get('itemPrice'))
        take('ga:dynx_price', entry.get('dynx_price'))

    return found


def _listing_price(buyer_item: dict) -> tuple[Decimal | None, str | None, str]:
    """Витринная цена, её источник и причина расхождения, если оно есть."""
    candidates = listing_price_candidates(buyer_item)
    if not candidates:
        return None, None, ''
    distinct = set(candidates.values())
    if len(distinct) > 1:
        parts = ', '.join(f'{path} = {value}' for path, value in sorted(candidates.items()))
        return None, None, f'Ключи состояния разошлись: {parts}.'
    source = 'state:formattedPrice.value' if 'state:formattedPrice.value' in candidates \
        else sorted(candidates)[0]
    return distinct.pop(), source, ''


# --------------------------------------------------------------- заголовок


def _title(buyer_item: dict | None, html: str) -> str | None:
    """Заголовок объявления.

    Неразрывные пробелы Avito («24\xa0м²») заменяются обычными: заголовок ложится
    в базу и сверяется с тем, что человек списал со скриншота обычными пробелами.
    """
    raw = None
    if buyer_item:
        candidate = (buyer_item.get('item') or {}).get('title')
        if isinstance(candidate, str) and candidate.strip():
            raw = candidate
    if raw is None:
        match = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
        if match:
            raw = html_lib.unescape(match.group(1))
    if raw is None:
        return None
    return re.sub(r'\s+', ' ', normalize(raw)).strip() or None


def _min_nights(buyer_item: dict) -> int | None:
    """Минимальный срок проживания. На страницах фазы 0 его нет ни в каком виде.

    Смотрим ровно те ключи, где он лежал бы по смыслу. Если площадка когда-нибудь
    начнёт его отдавать — заберём; выдумывать значение нельзя.
    """
    item = buyer_item.get('item') or {}
    short_term = item.get('shortTermRent') or {}
    for source in (short_term, item):
        for key in ('minNights', 'minDays', 'minimumNights', 'minStay'):
            value = source.get(key)
            if isinstance(value, int) and 1 <= value <= 30:
                return value
    return None


# ----------------------------------------------------------- блок цен по датам

NEAREST_DATES_MARKER = 'data-marker="nearest-dates"'

# «5 670 ₽ 26—27 августа», «4 125 ₽ 31 авг—1 сент».
# Порядок «сумма, затем подпись» — часть вёрстки блока, и он единственный,
# на чём держится разбор: своих атрибутов у карточек периода нет.
_PERIOD_RE = re.compile(
    r'(\d[\d ]{2,9})\s*₽\s*'
    r'(\d{1,2}(?:\s+[а-яё]+)?\s*[—–\-]\s*\d{1,2}\s+[а-яё]+)'
)


def _resolve_range(label: str, today: date) -> tuple[date, int] | None:
    """Подпись диапазона в дату заезда и число ночей.

    Года в подписи нет — он приставляется относительно дня сбора. Это арифметика
    от известной точки, а не догадка: снимок всегда пишется в день сбора.
    """
    match = re.match(
        r'(\d{1,2})\s*([а-яё]*)\s*[—–\-]\s*(\d{1,2})\s+([а-яё]+)', label.strip()
    )
    if not match:
        return None
    day_from, month_from_raw, day_to, month_to_raw = match.groups()
    month_to = RU_MONTHS.get(month_to_raw[:3])
    month_from = RU_MONTHS.get(month_from_raw[:3], month_to)
    if not month_from or not month_to:
        return None

    check_in = _nearest_forward(int(day_from), month_from, today)
    if check_in is None:
        return None
    year_to = check_in.year + (1 if month_to < month_from else 0)
    try:
        check_out = date(year_to, month_to, int(day_to))
    except ValueError:
        return None
    nights = (check_out - check_in).days
    if not 0 < nights <= MAX_NIGHTS_IN_LABEL:
        return None
    return check_in, nights


def _nearest_forward(day: int, month: int, today: date) -> date | None:
    """Ближайшая такая дата, не сильно позади дня сбора.

    Запас в месяц назад: карусель показывает будущее, но снимок мог быть сделан
    в тот же день, а часовые пояса и полночь не повод потерять период.
    """
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if (candidate - today).days >= -31:
            return candidate
    return None


# Карточка периода. У каждой есть data-id с точными датами в ISO — это надёжнее
# подписи «26—27 августа», в которой нет года и который приходится приставлять
# арифметикой от дня сбора. Подпись остаётся запасным источником и сверкой.
_CARD_RE = re.compile(r'<li\b[^>]*>.*?</li>', re.S | re.I)
_CARD_DATES_RE = re.compile(
    r'data-id="(\d{4}-\d{2}-\d{2})--(\d{4}-\d{2}-\d{2})"'
)
_CARD_MONEY_RE = re.compile(r'(\d[\d ]{2,9})\s*₽')
_CARD_LABEL_RE = re.compile(
    r'(\d{1,2}(?:\s+[а-яё]+)?\s*[—–\-]\s*\d{1,2}\s+[а-яё]+)'
)


@dataclass(frozen=True)
class DatesBlock:
    """Что нашлось в блоке «Цены по датам»."""

    periods: list[ParsedPeriod]
    unreadable_labels: list[str]
    # Сколько карточек в блоке. Сверяется с числом разобранных периодов:
    # карточка, из которой ничего не вышло, иначе исчезает бесследно — а это
    # ровно признак смены вёрстки, ради которого блок и проверяется.
    cards_found: int


def _carousel_html(html: str) -> str | None:
    """Разметка самой карусели, от маркера до закрытия списка."""
    start = html.find(NEAREST_DATES_MARKER)
    if start < 0:
        return None
    end = html.find('</ul>', start)
    # Закрытия может не оказаться, если вёрстка сменилась: берём разумный кусок,
    # но не молчим об этом — карточек в нём просто не найдётся.
    return html[start:end if end > 0 else start + 8000]


def _card_period(card: str, today: date) -> tuple[ParsedPeriod | None, str | None]:
    """Одна карточка: период или подпись, которую не удалось разобрать."""
    text = re.sub(r'\s+', ' ', normalize(_TAG_RE.sub(' ', card)))
    money = _CARD_MONEY_RE.search(text)
    label_match = _CARD_LABEL_RE.search(text)
    label = re.sub(r'\s+', ' ', label_match.group(1)).strip() if label_match else ''

    total = _digits_to_decimal(money.group(1)) if money else None

    dates = _CARD_DATES_RE.search(card)
    from_label = _resolve_range(label, today) if label else None

    if dates:
        try:
            check_in = date.fromisoformat(dates.group(1))
            check_out = date.fromisoformat(dates.group(2))
        except ValueError:
            check_in = check_out = None
        nights = (check_out - check_in).days if check_in and check_out else 0
        # Подпись и data-id должны говорить одно и то же. Если разошлись —
        # карточку не берём: это тот же случай, что расхождение ключей состояния,
        # и число в ней может относиться к другому периоду.
        if from_label and check_in and from_label != (check_in, nights):
            return None, (
                f'«{label}» расходится с data-id ({dates.group(1)}—{dates.group(2)})'
            )
    else:
        # Запасной путь: подпись без года, год приставляется от дня сбора.
        check_in, nights = from_label if from_label else (None, 0)

    if total is None or check_in is None or not 0 < nights <= MAX_NIGHTS_IN_LABEL:
        return None, (label or text[:60].strip() or 'карточка без подписи')

    return ParsedPeriod(
        check_in=check_in,
        nights=nights,
        total_price=total.quantize(Decimal('0.01')),
        price_per_night=(total / nights).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        ),
        label=label or f'{check_in.isoformat()} + {nights}',
        source='nearest_dates:data-id' if dates else 'nearest_dates:label',
    ), None


def parse_nearest_dates(html: str, today: date) -> DatesBlock:
    """Периоды из блока «Цены по датам», по карточке за раз.

    Разбор идёт по карточкам, а не сплошным текстом блока: так карточка,
    из которой ничего не вышло, остаётся видна в счёте, а не исчезает.
    """
    carousel = _carousel_html(html)
    if carousel is None:
        return DatesBlock(periods=[], unreadable_labels=[], cards_found=0)

    periods: list[ParsedPeriod] = []
    unreadable: list[str] = []
    cards = _CARD_RE.findall(carousel)
    for card in cards:
        period, problem = _card_period(card, today)
        if period is not None:
            periods.append(period)
        else:
            unreadable.append(problem)

    return DatesBlock(
        periods=periods, unreadable_labels=unreadable, cards_found=len(cards)
    )


# ------------------------------------------------ цена на запрошенный период

# Блок цены на выбранные даты. Разобран на ручном дампе 29.08.
# Порядок в разметке: базовая цена, скидка, итог.
TOTAL_PRICE_MARKER = 'str-price-info/total-price'      # «12 690 ₽», зачёркнутая
DISCOUNT_MARKER = 'str-price-info/discount'            # «−10%»
FINAL_PRICE_MARKER = 'str-price-info/final-price'      # «11 421 ₽ за весь период»
DOM_PRICE_MARKER = 'item-view/item-price'              # «3 807» — за ночь на период

# Разряды в этом блоке отбиты `&nbsp;`, а слова склеены ими же:
# `11&nbsp;421&nbsp;₽&nbsp;за&nbsp;весь&nbsp;период`. Без нормализации
# ни один шаблон здесь не срабатывает.
_MONEY_RE = re.compile(r'(\d[\d ]{2,9})\s*(?:₽|%|$)')

# Расхождение между суммой, делённой на ночи, и ценой за сутки в той же
# разметке. Рубль — это округление площадки, а не расхождение: 11 420 на три
# ночи это 3806,67, и на странице будет 3 807.
PRICE_TOLERANCE = Decimal('1')


def _marker_text(html: str, marker: str, limit: int = 400) -> str:
    """Текст элемента с этим `data-marker`.

    Кусок обрезается по следующему `data-marker`: иначе пустой элемент
    молча подставил бы число из соседнего, а числа здесь соседствуют
    похожие — базовая цена, скидка и итог стоят подряд.
    """
    start = html.find(f'data-marker="{marker}"')
    if start < 0:
        return ''
    # Маркер стоит внутри открывающего тега, поэтому начинаем с его закрытия:
    # иначе в «текст» попадут остатки самого тега вместе с их числами —
    # у скидки там style с цветом в rgb, и цифры оттуда путаются с ценой.
    opened = html.find('>', start)
    if opened < 0:
        return ''
    following = html.find('data-marker=', opened)
    end = min(opened + limit, following if following > 0 else opened + limit)
    # Тот же путь, что для видимого текста: снимаются тела скриптов и теги.
    # Хвост обрезается по первому `<`, у которого закрытие не поместилось
    # в кусок, — иначе в текст попадают куски следующего тега.
    text = visible_text(html[opened + 1:end]).split('<')[0]
    return text.strip(' ·|')


def _marker_money(html: str, marker: str) -> Decimal | None:
    match = _MONEY_RE.search(_marker_text(html, marker))
    return _digits_to_decimal(match.group(1)) if match else None


def parse_period_block(html: str, interval) -> tuple[ParsedPeriod | None, str]:
    """Цена на запрошенный период. Возвращает период и причину расхождения.

    Витринную цену сюда не пускает сама конструкция: она живёт в состоянии
    приложения, а здесь читается только разметка блока `str-price-info`.
    """
    final = _marker_money(html, FINAL_PRICE_MARKER)
    if final is None or interval is None or not interval.nights:
        return None, ''

    per_night = (final / interval.nights).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )

    # Проверка целостности, как её сформулировал заказчик: сумма за период,
    # делённая на ночи, обязана сойтись с ценой за сутки в той же разметке.
    # Не сошлась — возвращаем ошибку, а не одно из двух чисел.
    dom_per_night = _marker_money(html, DOM_PRICE_MARKER)
    if dom_per_night is not None and abs(per_night - dom_per_night) > PRICE_TOLERANCE:
        return None, (
            f'Блок цены не сходится сам с собой: {final} за '
            f'{interval.nights} ноч. это {per_night} за ночь, '
            f'а в разметке {dom_per_night}.'
        )

    discount = _marker_text(html, DISCOUNT_MARKER)
    return ParsedPeriod(
        check_in=interval.check_in,
        nights=interval.nights,
        total_price=final.quantize(Decimal('0.01')),
        price_per_night=per_night,
        label=(
            f'{interval.check_in:%d.%m}—'
            f'{interval.check_in + timedelta(days=interval.nights):%d.%m}'
        ),
        source='str-price-info',
        base_price=_base_price(html),
        discount_label=discount,
    ), ''


def _base_price(html: str) -> Decimal | None:
    """Цена до скидки за длительность. В снимок не идёт — только в лог сбора."""
    value = _marker_money(html, TOTAL_PRICE_MARKER)
    return value.quantize(Decimal('0.01')) if value is not None else None


def _merge_requested(periods, requested):
    """Поставить цену из блока периода на место карусельной за тот же период.

    Карусель после выбора дат перестраивается под запрошенную длительность
    и содержит тот же период. Оба источника читаются, но в снимок идёт один —
    из блока: он и есть ответ площадки на наш вопрос.
    """
    key = (requested.check_in, requested.nights)
    merged = [p for p in periods if (p.check_in, p.nights) != key]
    merged.append(requested)
    return sorted(merged, key=lambda p: (p.check_in, p.nights))


def _carousel_disagrees(periods, requested) -> str:
    """Карусель и блок периода про один период должны говорить одно."""
    for period in periods:
        if (period.check_in, period.nights) != (requested.check_in, requested.nights):
            continue
        if period.total_price != requested.total_price:
            return (
                f'Блок периода и карусель разошлись про {requested.label}: '
                f'{requested.total_price} против {period.total_price}.'
            )
    return ''


# ------------------------------------------------------------ видимый текст

_VISIBLE_MONEY_RE = re.compile(r'(\d[\d ]{2,9})\s*(?:₽|руб)')


def visible_money(text: str) -> list[Decimal]:
    """Суммы из видимого текста. Ненадёжный источник: там залог и уборка."""
    values = []
    for match in _VISIBLE_MONEY_RE.finditer(text):
        value = _digits_to_decimal(match.group(1))
        if value is not None and value not in values:
            values.append(value)
    return values


# ------------------------------------------------------------------ разбор


def _has_marker(text: str, markers) -> str | None:
    lowered = text.lower()
    for marker in markers:
        if marker in lowered:
            return marker
    return None


def _is_gone(buyer_item: dict) -> bool:
    item = buyer_item.get('item') or {}
    if item.get('isActive') is False:
        return True
    return any(
        item.get(flag) is True
        for flag in ('isClosed', 'isArchived', 'isExpired', 'isDeleted', 'isBlocked')
    )


def parse_item_page(html: str, *, today: date, interval=None) -> ParsedPage:
    """Разобрать страницу объявления.

    `today` передаётся снаружи: подписи периодов идут без года, и он приставляется
    относительно дня сбора. Брать его из системных часов внутри значило бы сделать
    тесты зависимыми от календаря.

    `interval` — период, который запрашивали адресом: любой объект с полями
    `check_in` и `nights`. По нему читается блок `str-price-info`; без него
    разбирается только карусель ближайших дат. Модуль остаётся чистым —
    ни Django, ни базы он не знает.
    """
    if not html or not html.strip():
        return ParsedPage(status=ParseStatus.FAILED, error_note='Пустая страница.')

    text = visible_text(html)
    buyer_item = extract_buyer_item(html)
    title = _title(buyer_item, html)

    listing_price = listing_source = None
    conflict_note = ''
    if buyer_item is not None:
        listing_price, listing_source, conflict_note = _listing_price(buyer_item)

    # Отказы разбираются раньше цен: на странице проверки числа тоже встречаются.
    blocker = _has_marker(text, BLOCK_MARKERS)
    if blocker:
        if buyer_item is None:
            return ParsedPage(
                status=ParseStatus.BLOCKED,
                title=title,
                error_note=f'Вместо страницы — проверка площадки: «{blocker}».',
            )
        return ParsedPage(
            status=ParseStatus.CAPTCHA_OVERLAY,
            title=title,
            listing_price=listing_price,
            listing_price_source=listing_source,
            min_nights=_min_nights(buyer_item),
            error_note=(
                f'Объявление загрузилось, но проверка перекрыла блок цен: «{blocker}». '
                'Витринная цена доступна, цен по датам нет.'
            ),
        )

    if buyer_item is None:
        gone = _has_marker(text, GONE_MARKERS)
        if gone:
            return ParsedPage(
                status=ParseStatus.NOT_AVAILABLE,
                title=title,
                error_note=f'Объявление недоступно: «{gone}».',
            )
        candidates = visible_money(text)
        if candidates:
            listed = ', '.join(str(value) for value in candidates[:8])
            return ParsedPage(
                status=ParseStatus.PRICE_UNRELIABLE,
                title=title,
                error_note=(
                    'Состояние приложения не разобрано, числа найдены только '
                    f'в видимом тексте: {listed}. В цену такое не идёт: '
                    'там же лежат залог, уборка и комплект белья.'
                ),
            )
        return ParsedPage(
            status=ParseStatus.FAILED,
            title=title,
            error_note='Состояние приложения на странице не найдено.',
        )

    if conflict_note:
        return ParsedPage(
            status=ParseStatus.STATE_CONFLICT,
            title=title,
            error_note=(
                f'{conflict_note} Разметка изменилась — любое из чисел может '
                'оказаться чужим, поэтому не берём ни одно.'
            ),
        )

    min_nights = _min_nights(buyer_item)

    if _is_gone(buyer_item):
        return ParsedPage(
            status=ParseStatus.NOT_AVAILABLE,
            title=title,
            listing_price=listing_price,
            listing_price_source=listing_source,
            min_nights=min_nights,
            error_note='Объявление снято с публикации или закрыто.',
        )

    if NEAREST_DATES_MARKER not in html and FINAL_PRICE_MARKER not in html:
        return ParsedPage(
            status=ParseStatus.NO_DATED_PRICES,
            title=title,
            listing_price=listing_price,
            listing_price_source=listing_source,
            min_nights=min_nights,
            error_note=(
                'Блока «Цены по датам» на странице нет. Витринная цена есть, '
                'но она в сравнение не идёт.'
            ),
        )

    block = parse_nearest_dates(html, today)
    periods = block.periods
    notes = [_shortfall_note(block)]
    if not periods and NEAREST_DATES_MARKER in html:
        notes.insert(0, 'Блок «Цены по датам» есть, но ни один период из него '
                        'не разобрался. Похоже на смену вёрстки блока — '
                        'парсер надо чинить.')
    requested, conflict = parse_period_block(html, interval)

    if conflict:
        return ParsedPage(
            status=ParseStatus.PRICE_CONFLICT,
            title=title,
            listing_price=listing_price,
            listing_price_source=listing_source,
            min_nights=min_nights,
            cards_found=block.cards_found,
            error_note=(
                f'{conflict} Разметка изменилась — не берём ни одно из чисел.'
            ),
        )

    if requested is not None:
        disagreement = _carousel_disagrees(periods, requested)
        if disagreement:
            return ParsedPage(
                status=ParseStatus.PRICE_CONFLICT,
                title=title,
                listing_price=listing_price,
                listing_price_source=listing_source,
                min_nights=min_nights,
                cards_found=block.cards_found,
                error_note=f'{disagreement} Не берём ни одно из чисел.',
            )
        periods = _merge_requested(periods, requested)
    elif interval is not None:
        notes.append(
            'Блока цены на запрошенный период на странице нет: '
            f'{FINAL_PRICE_MARKER} не найден.'
        )

    # Ни карусель, ни блок периода не дали ни одной цены. Витринная при этом
    # могла извлечься — подставлять её вместо цены на даты нельзя.
    if not periods:
        return ParsedPage(
            status=ParseStatus.DATES_BLOCK_UNREADABLE,
            title=title,
            listing_price=listing_price,
            listing_price_source=listing_source,
            min_nights=min_nights,
            cards_found=block.cards_found,
            error_note=' '.join(note for note in notes if note),
        )

    return ParsedPage(
        status=ParseStatus.OK,
        title=title,
        listing_price=listing_price,
        listing_price_source=listing_source,
        periods=periods,
        min_nights=min_nights,
        cards_found=block.cards_found,
        requested_period=requested,
        error_note=' '.join(note for note in notes if note),
    )


def _shortfall_note(block: DatesBlock) -> str:
    """Что в блоке было, но не разобралось. Пустая строка, если разобралось всё."""
    parts = []
    if block.unreadable_labels:
        listed = ', '.join(f'«{label}»' for label in block.unreadable_labels[:5])
        parts.append(f'Не разобраны подписи периодов: {listed}.')
    missed = block.cards_found - len(block.periods) - len(block.unreadable_labels)
    if missed > 0:
        parts.append(
            f'В блоке {block.cards_found} карточек, разобрано периодов '
            f'{len(block.periods)}: {missed} не подошли ни под один шаблон.'
        )
    return ' '.join(parts)
