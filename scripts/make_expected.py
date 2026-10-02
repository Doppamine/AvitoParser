"""Скелет tests/fixtures/expected.json — один раз, руками, без сети.

Ожидаемые значения проставляет человек, сверяя дамп со скриншотом. Скрипт
кладёт только скелет: список дампов, статусы из постановки фазы 2 и подсказки
`_hint_*`, чтобы было понятно, какое место на скриншоте искать.

Подсказки — то, что нашёл парсер. Тесты их не читают. Если подсказка расходится
с тем, что видно на скриншоте, это находка, а не опечатка: значит парсер достаёт
не то, и об этом надо сказать, а не подгонять файл.

Запуск: python scripts/make_expected.py
Уже заполненные записи не трогаются: скрипт только дописывает недостающие дампы.
`--force` пересобирает всё заново и стирает ручную работу — нужен редко.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.parsing import parse_item_page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures'
TARGET = FIXTURES / 'expected.json'

# День сбора дампов фазы 0. Нужен, чтобы приставить год к подписям периодов.
COLLECTED_ON = date(2026, 8, 25)

# Дампы, снятые не в тот день. Ручной снят из обычного Chrome четырьмя днями позже.
COLLECTED_ON_OVERRIDES = {
    '7628611904_2026-09-20_2026-09-23_g2.html': date(2026, 8, 29),
}

# Статусы взяты из постановки фазы 2 и подтверждены заказчиком 29.08.
DUMPS = [
    ('20260825-230710_p1_u0_browser.html', '20260825-230710_p1_u0_full.png', 'ok',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_v_apart-otele_24_m_1_krovat_7628611904',
     'объявление 7628611904, карусель «Цены по датам» есть'),
    ('20260825-230710_p1_u1_browser.html', '20260825-230710_p1_u1_full.png', 'ok',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_30_m_3_krovati_4727479076',
     'объявление 4727479076, карусель есть'),
    ('20260825-230710_p1_u2_browser.html', '20260825-230710_p1_u2_full.png', 'ok',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_25_m_2_krovati_4875762986',
     'объявление 4875762986, карусель есть, два периода по две ночи'),
    ('20260825-230710_p1_u3_browser.html', '20260825-230710_p1_u3_full.png', 'captcha_overlay',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_25_m_2_krovati_4431710759',
     'объявление 4431710759, проверка поверх загруженной страницы: витринная цена есть, карусели нет'),
    ('20260825-230710_p1_u4_browser.html', '20260825-230710_p1_u4_full.png', 'ok',
     'https://avito.ru/moskva/kvartiry/kvartira-studiya_19_m_1_krovat_7251781371',
     'объявление 7251781371, карусель есть, все периоды через месяц'),
    ('20260825-230710_p1_u0_http.html', None, 'blocked',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_v_apart-otele_24_m_1_krovat_7628611904',
     'обычный HTTP-запрос, 403: вместо страницы заглушка «Доступ ограничен»'),
    ('20260825-230710_p1_u2_http.html', None, 'blocked',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_25_m_2_krovati_4875762986',
     'то же, второй экземпляр отказа'),
    ('7628611904_2026-09-20_2026-09-23_g2.html', '7628611904_2026-09-20_2026-09-23_g2.png', 'ok',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_v_apart-otele_24_m_1_krovat_7628611904',
     'снят вручную 29.08 из обычного Chrome: даты 20–23 сентября заданы адресом, '
     'на месте блок str-price-info с ценой на период. Единственная фикстура, '
     'где есть цена на запрошенный период'),
    ('20260825-230710_p1_u1_http_ck.html', None, 'no_dated_prices',
     'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_30_m_3_krovati_4727479076',
     'ключевая фикстура: страница пришла целиком, витринная цена есть, карусели нет. '
     'Парсер обязан вернуть отсутствие датированной цены, а не подставить витринную'),
]

FILLED_BY_HUMAN = None  # заполняет человек


def hints(dump: Path, interval=None) -> dict:
    page = parse_item_page(dump.read_text(encoding='utf-8', errors='replace'),
                           today=COLLECTED_ON, interval=interval)
    return {
        'title': page.title,
        'listing_price': str(page.listing_price) if page.listing_price else None,
        'periods': [
            {'label': period.label, 'total': str(period.total_price)}
            for period in page.periods
        ],
    }


# Период, запрошенный адресом. Свойство дампа, а не ожидание: он записан
# в имени файла и в самом адресе, гадать не о чем.
INTERVALS = {
    '7628611904_2026-09-20_2026-09-23_g2.html': {'check_in': '2026-09-20', 'nights': 3},
}


def entry_for(name, shot, status, url, note) -> dict:
    dump = FIXTURES / 'dumps' / name
    interval = INTERVALS.get(name)
    collected_on = COLLECTED_ON_OVERRIDES.get(name, COLLECTED_ON)
    page = parse_item_page(
        dump.read_text(encoding='utf-8', errors='replace'),
        today=collected_on,
        interval=_interval_object(interval),
    )
    requested = page.requested_period

    periods = [
        {
            '_hint_label': period.label,
            '_hint_total': str(period.total_price),
            '_hint_source': period.source,
            'check_in': FILLED_BY_HUMAN,
            'nights': FILLED_BY_HUMAN,
            'total_price': FILLED_BY_HUMAN,
            'price_per_night': FILLED_BY_HUMAN,
        }
        for period in page.periods
    ]

    expected = {
        'status': status,
        '_hint_title': page.title,
        'title': FILLED_BY_HUMAN,
        '_hint_listing_price': str(page.listing_price) if page.listing_price else None,
        'listing_price': FILLED_BY_HUMAN,
        'listing_price_note': '',
        'min_nights': FILLED_BY_HUMAN,
        'fees_included': FILLED_BY_HUMAN,
        'periods': periods,
    }

    if interval:
        expected['requested_period'] = {
            '_hint_base_price': str(requested.base_price) if requested else None,
            '_hint_discount': requested.discount_label if requested else None,
            '_hint_total': str(requested.total_price) if requested else None,
            '_hint_per_night': str(requested.price_per_night) if requested else None,
            'base_price': FILLED_BY_HUMAN,
            'discount_label': FILLED_BY_HUMAN,
            'total_price': FILLED_BY_HUMAN,
            'price_per_night': FILLED_BY_HUMAN,
        }

    record = {
        'file': name,
        'screenshot': shot,
        'url': url,
        'note': note,
        'collected_on': collected_on.isoformat(),
    }
    if interval:
        record['requested_interval'] = interval
    record['expected'] = expected
    return record


@dataclass(frozen=True)
class RequestedInterval:
    """Период для парсера. Своя, а не из monitor.services: та тянет за собой
    модели и Django, а этому скрипту база не нужна."""

    check_in: date
    nights: int


def _interval_object(interval):
    if not interval:
        return None
    return RequestedInterval(
        check_in=date.fromisoformat(interval['check_in']), nights=interval['nights']
    )


def build(existing=None) -> dict:
    """Скелет. Уже заполненные записи не трогаются — в них ручная работа."""
    filled = {item['file']: item for item in (existing or {}).get('dumps', [])}
    entries = []
    added = []
    for name, shot, status, url, note in DUMPS:
        if name in filled:
            entries.append(filled[name])
            continue
        entries.append(entry_for(name, shot, status, url, note))
        added.append(name)

    return {
        '_comment': (
            'Ожидаемые значения проставляет человек, сверяя дамп со скриншотом. '
            'null означает «не заполнено» — такой дамп тест пропускает с явным '
            'сообщением, а не проходит молча. Поля _hint_* это подсказки парсера, '
            'тесты их не читают; расхождение подсказки со скриншотом — находка.'
        ),
        '_how_to_fill': [
            'title — заголовок объявления так, как он на скриншоте',
            'listing_price — витринная цена «от N ₽ за сутки», числом без пробелов; '
            'если её на скриншоте нет, поставьте 0 и напишите почему в listing_price_note',
            'min_nights — минимальный срок, если он где-то виден; иначе оставьте null',
            'fees_included — true, если сервисный сбор площадки входит в сумму '
            'периода, false если нет, null если по скриншоту не определить',
            'по каждому периоду карусели: check_in в виде 2026-08-26 (с годом), '
            'nights числом, total_price сумма за период, price_per_night ваше деление',
            'requested_period — блок цены на запрошенный период: base_price до скидки, '
            'discount_label как написано на странице, total_price итог, '
            'price_per_night ваше деление итога на ночи',
            'у дампов со статусом blocked заполнять нечего; у captcha_overlay — '
            'только title и listing_price, периодов там нет',
        ],
        'dumps': entries,
    }, added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true',
                        help='пересобрать все записи заново, стерев ручную работу')
    args = parser.parse_args()

    existing = None
    if TARGET.exists() and not args.force:
        existing = json.loads(TARGET.read_text(encoding='utf-8'))

    data, added = build(existing)
    TARGET.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    if added:
        print(f'Добавлены записи: {", ".join(added)}')
        print('Заполните значения руками, сверяя со скриншотами в tests/fixtures/shots/.')
    else:
        print('Новых дампов нет, файл не изменился по существу.')


if __name__ == '__main__':
    main()
