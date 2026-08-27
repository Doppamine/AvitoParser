#!/usr/bin/env python
"""Тестовые данные для страницы сравнения.

Данные заведомо выдуманные и помечены в заголовках: перепутать их с собранными
нельзя. Скрипт ничего не удаляет и не переписывает — снимки цен неизменяемы,
а на пустую базу он рассчитан ровно один раз.

Снимки лежат интервалами, как их смотрит менеджер: ближайшие даты по две и три
ночи и отдельный период через месяц.

Запуск:  python scripts/seed.py
"""

import os
import random
import sys
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from monitor.models import (  # noqa: E402
    Apartment,
    Competitor,
    PriceKind,
    PriceSnapshot,
    SnapshotStatus,
    Source,
)
from monitor.services import Interval, default_interval, horizon_dates  # noqa: E402

# Фиксированное зерно: данные воспроизводимы, разговор о конкретной ячейке возможен.
rng = random.Random(20260827)

# Профили конкурентов подобраны так, чтобы на странице было видно не только
# благополучие: без сбоев непонятно, как выглядит неполная картина.
NORMAL = 'normal'              # цены есть во всех сборах
STALE = 'stale'                # последний сбор не удался, показывается вчерашнее число
BLOCKED = 'blocked'            # последний сбор упёрся в блокировку
LISTING_ONLY = 'listing'       # проверка перекрыла цены по датам, витринная осталась
NO_DATA = 'nodata'             # ни одного снимка
OTHER_PERIOD = 'other_period'  # на дальнюю дату собран другой период, не запрошенный

APARTMENTS = [
    {
        'title': '[тест] Студия на Мясницкой',
        'address': 'Москва, Мясницкая ул., 1',
        'min_nights': 2,
        'competitors': [
            ('[тест] Студия у Чистых прудов', NORMAL, True),
            ('[тест] Апартаменты на Лубянке', STALE, True),
            ('[тест] Квартира у метро Тургеневская', LISTING_ONLY, True),
            ('[тест] Студия в центре, без снимков', NO_DATA, True),
        ],
    },
    {
        'title': '[тест] Двушка на Ленинском',
        'address': 'Москва, Ленинский пр-т, 42',
        'min_nights': 2,
        'competitors': [
            ('[тест] Двушка у Гагаринской', NORMAL, True),
            ('[тест] Апартаменты Ленинский 44', OTHER_PERIOD, True),
            ('[тест] Квартира на Вавилова', BLOCKED, True),
            ('[тест] Снятое с публикации объявление', NORMAL, False),
        ],
    },
    {
        'title': '[тест] Однушка у метро Сокол',
        'address': 'Москва, Ленинградский пр-т, 75',
        'min_nights': 2,
        'competitors': [
            ('[тест] Однушка на Соколе', NORMAL, True),
            ('[тест] Апартаменты Аэропорт', NORMAL, True),
            ('[тест] Студия на Балтийской', STALE, True),
            ('[тест] Квартира на Песчаной', OTHER_PERIOD, True),
        ],
    },
]

# Период через месяц — как в примере заказчика: менеджер смотрит не завтрашний день,
# а произвольный интервал в будущем.
FAR_START = timezone.localdate() + timedelta(days=24)
FAR_INTERVAL = Interval(FAR_START, 3)


def collection_intervals():
    """Что считается собранным: ближайшие даты и период через месяц."""
    near = [default_interval()]
    near += [Interval(day, 2) for day in horizon_dates()]
    near += [Interval(day, 3) for day in horizon_dates()[::2]]
    far = [FAR_INTERVAL, Interval(FAR_START + timedelta(days=1), 2)]
    return sorted(set(near + far), key=lambda i: (i.check_in, i.nights))


def night_price(base, day):
    """Цена одной ночи. Пятница и суббота дороже на 20–30%."""
    price = base + Decimal(rng.randint(-400, 400))
    if day.weekday() in (4, 5):
        price *= Decimal('1.20') + Decimal(rng.randint(0, 10)) / Decimal(100)
    return price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def make_dated(owner, interval, collected_at, source, status, base=None, note=''):
    """Снимок на интервал. Цена появляется только у успешного исхода."""
    fields = {
        'price_kind': PriceKind.DATED,
        'check_in': interval.check_in if interval else None,
        'nights': interval.nights if interval else None,
        'collected_at': collected_at,
        'source': source,
        'status': status,
        'error_note': note,
        # Входит ли сервисный сбор площадки в сумму — не выяснено. Заполнять нечем.
        'fees_included': None,
    }
    if status == SnapshotStatus.OK:
        total = sum(
            (
                night_price(base, interval.check_in + timedelta(days=offset))
                for offset in range(interval.nights)
            ),
            Decimal('0'),
        )
        fields['total_price'] = total
        # Менеджер делит сумму на число ночей — делаем то же самое.
        fields['price_per_night'] = (total / interval.nights).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
    key = 'competitor' if isinstance(owner, Competitor) else 'apartment'
    PriceSnapshot.objects.create(**{key: owner}, **fields)


def make_listing(competitor, collected_at, base):
    """Витринная цена «от N ₽». Без дат — и в сравнение она не попадает."""
    # По замерам фазы 0 витринная ниже реальной в 1,25–1,9 раза.
    shown = (base / Decimal('1.5')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    PriceSnapshot.objects.create(
        competitor=competitor,
        price_kind=PriceKind.LISTING,
        price_per_night=shown,
        collected_at=collected_at,
        source=Source.AVITO,
        status=SnapshotStatus.OK,
        fees_included=None,
    )


def collect_for(competitor, profile, intervals, runs, base):
    for index, collected_at in enumerate(runs):
        is_last_run = index == len(runs) - 1
        make_listing(competitor, collected_at, base)

        if profile == LISTING_ONLY:
            for interval in intervals:
                make_dated(
                    competitor, interval, collected_at, Source.AVITO,
                    SnapshotStatus.CAPTCHA_OVERLAY,
                    note='Проверка перекрыла блок «Цены по датам».',
                )
            continue
        if is_last_run and profile == STALE:
            for interval in intervals:
                make_dated(
                    competitor, interval, collected_at, Source.AVITO,
                    SnapshotStatus.FAILED, note='Страница не догрузилась.',
                )
            continue
        if is_last_run and profile == BLOCKED:
            # Блокировка приходит до разбора страницы: какой период запрашивали,
            # из ответа уже не следует.
            make_dated(
                competitor, None, collected_at, Source.AVITO, SnapshotStatus.BLOCKED,
                note='HTTP 429: слишком много запросов.',
            )
            continue

        for interval in intervals:
            if profile == OTHER_PERIOD and interval.check_in == FAR_START:
                # У объявления минимальный срок длиннее запрошенного: площадка отдала
                # цену на другой период. Подставлять её вместо запрошенной нельзя.
                interval = Interval(FAR_START, interval.nights + 1)
            make_dated(
                competitor, interval, collected_at, Source.AVITO,
                SnapshotStatus.OK, base=base,
            )


def main():
    if Apartment.objects.exists():
        print('В базе уже есть данные. Seed ничего не трогает — очистите базу вручную.')
        return 1

    intervals = collection_intervals()
    now = timezone.now()
    # Три ежедневных сбора: позавчерашний, вчерашний и сегодняшний утренний.
    runs = [now - timedelta(days=2), now - timedelta(days=1), now - timedelta(hours=3)]

    for spec in APARTMENTS:
        apartment = Apartment.objects.create(
            title=spec['title'], address=spec['address'], min_nights=spec['min_nights']
        )
        our_base = Decimal(rng.randrange(4500, 8500, 100))
        for collected_at in runs:
            for interval in intervals:
                make_dated(
                    apartment, interval, collected_at, Source.REALTYCALENDAR,
                    SnapshotStatus.OK, base=our_base,
                )

        for title, profile, is_active in spec['competitors']:
            competitor = Competitor.objects.create(
                apartment=apartment,
                url=f'https://www.avito.ru/moskva/kvartiry/test_{rng.randrange(10**9)}',
                title=title,
                is_active=is_active,
            )
            if profile == NO_DATA:
                continue
            collect_for(
                competitor, profile, intervals, runs,
                base=Decimal(rng.randrange(4000, 9000, 100)),
            )

    print(
        f'Заведено: {Apartment.objects.count()} квартир, '
        f'{Competitor.objects.count()} конкурентов, '
        f'{PriceSnapshot.objects.count()} снимков на {len(intervals)} периодов.\n'
        f'Ближайшие выходные: {default_interval()}. Период через месяц: {FAR_INTERVAL}.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
