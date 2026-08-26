#!/usr/bin/env python
"""Тестовые данные для страницы сравнения.

Данные заведомо выдуманные и помечены в заголовках: перепутать их с собранными
нельзя. Скрипт ничего не удаляет и не переписывает — снимки цен неизменяемы,
а на пустую базу он рассчитан ровно один раз.

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

from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from monitor.models import (  # noqa: E402
    Apartment,
    Competitor,
    PriceKind,
    PriceSnapshot,
    SnapshotStatus,
    Source,
)
from monitor.services import horizon_dates  # noqa: E402

# Фиксированное зерно: данные воспроизводимы, разговор о конкретной ячейке возможен.
rng = random.Random(20260826)

# Профили конкурентов подобраны так, чтобы на странице было видно не только
# благополучие: без сбоев непонятно, как выглядит неполная картина.
NORMAL = 'normal'          # цены есть во всех сборах
STALE = 'stale'            # последний сбор не удался, показывается вчерашнее число
BLOCKED = 'blocked'        # последний сбор упёрся в блокировку
LISTING_ONLY = 'listing'   # проверка перекрыла цены по датам, витринная осталась
NO_DATA = 'nodata'         # ни одного снимка

APARTMENTS = [
    {
        'title': '[тест] Студия на Мясницкой',
        'address': 'Москва, Мясницкая ул., 1',
        'min_nights': 2,
        'competitors': [
            ('[тест] Студия у Чистых прудов', 1, NORMAL, True),
            ('[тест] Апартаменты на Лубянке', 2, STALE, True),
            ('[тест] Квартира у метро Тургеневская', 1, LISTING_ONLY, True),
            ('[тест] Студия в центре, без снимков', 1, NO_DATA, True),
        ],
    },
    {
        'title': '[тест] Двушка на Ленинском',
        'address': 'Москва, Ленинский пр-т, 42',
        'min_nights': 2,
        'competitors': [
            ('[тест] Двушка у Гагаринской', 2, NORMAL, True),
            ('[тест] Апартаменты Ленинский 44', 1, NORMAL, True),
            ('[тест] Квартира на Вавилова', 1, BLOCKED, True),
            ('[тест] Снятое с публикации объявление', 1, NORMAL, False),
        ],
    },
    {
        'title': '[тест] Однушка у метро Сокол',
        'address': 'Москва, Ленинградский пр-т, 75',
        'min_nights': 2,
        'competitors': [
            ('[тест] Однушка на Соколе', 1, NORMAL, True),
            ('[тест] Апартаменты Аэропорт', 2, NORMAL, True),
            ('[тест] Студия на Балтийской', 2, STALE, True),
            ('[тест] Квартира на Песчаной', 1, LISTING_ONLY, True),
        ],
    },
]


def night_price(base, day):
    """Цена одной ночи. Пятница и суббота дороже на 20–30%."""
    price = base + Decimal(rng.randint(-400, 400))
    if day.weekday() in (4, 5):
        price *= Decimal('1.20') + Decimal(rng.randint(0, 10)) / Decimal(100)
    return price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def make_dated(owner, day, nights, collected_at, source, status, base=None, note=''):
    """Снимок на дату. Цена появляется только у успешного исхода."""
    fields = {
        'price_kind': PriceKind.DATED,
        'check_in': day,
        'nights': nights,
        'collected_at': collected_at,
        'source': source,
        'status': status,
        'error_note': note,
        # Входит ли сервисный сбор площадки в сумму — не выяснено. Заполнять нечем.
        'fees_included': None,
    }
    if status == SnapshotStatus.OK:
        total = sum(
            (night_price(base, day + timedelta(days=offset)) for offset in range(nights)),
            Decimal('0'),
        )
        fields['total_price'] = total
        # При nights > 1 это среднее по диапазону, а не цена конкретной ночи.
        fields['price_per_night'] = (total / nights).quantize(
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


def main():
    if Apartment.objects.exists():
        print('В базе уже есть данные. Seed ничего не трогает — очистите базу вручную.')
        return 1

    dates = horizon_dates()
    now = timezone.now()
    # Три ежедневных сбора: позавчерашний, вчерашний и сегодняшний утренний.
    runs = [now - timedelta(days=2), now - timedelta(days=1), now - timedelta(hours=3)]

    for spec in APARTMENTS:
        apartment = Apartment.objects.create(
            title=spec['title'], address=spec['address'], min_nights=spec['min_nights']
        )
        our_base = Decimal(rng.randrange(4500, 8500, 100))
        for collected_at in runs:
            for day in dates:
                make_dated(
                    apartment,
                    day,
                    1,
                    collected_at,
                    Source.REALTYCALENDAR,
                    SnapshotStatus.OK,
                    base=our_base,
                )

        for title, nights, profile, is_active in spec['competitors']:
            competitor = Competitor.objects.create(
                apartment=apartment,
                url=f'https://www.avito.ru/moskva/kvartiry/test_{rng.randrange(10**9)}',
                title=title,
                is_active=is_active,
            )
            if profile == NO_DATA:
                continue

            base = Decimal(rng.randrange(4000, 9000, 100))
            for index, collected_at in enumerate(runs):
                is_last_run = index == len(runs) - 1
                if profile == LISTING_ONLY:
                    make_listing(competitor, collected_at, base)
                    for day in dates:
                        make_dated(
                            competitor, day, nights, collected_at, Source.AVITO,
                            SnapshotStatus.CAPTCHA_OVERLAY,
                            note='Проверка перекрыла блок «Цены по датам».',
                        )
                    continue

                make_listing(competitor, collected_at, base)
                if is_last_run and profile == STALE:
                    for day in dates:
                        make_dated(
                            competitor, day, nights, collected_at, Source.AVITO,
                            SnapshotStatus.FAILED, note='Страница не догрузилась.',
                        )
                    continue
                if is_last_run and profile == BLOCKED:
                    # Блокировка приходит до разбора страницы: дата запроса неизвестна.
                    PriceSnapshot.objects.create(
                        competitor=competitor,
                        price_kind=PriceKind.DATED,
                        collected_at=collected_at,
                        source=Source.AVITO,
                        status=SnapshotStatus.BLOCKED,
                        error_note='HTTP 429: слишком много запросов.',
                        fees_included=None,
                    )
                    continue
                for day in dates:
                    make_dated(
                        competitor, day, nights, collected_at, Source.AVITO,
                        SnapshotStatus.OK, base=base,
                    )

    print(
        f'Заведено: {Apartment.objects.count()} квартир, '
        f'{Competitor.objects.count()} конкурентов, '
        f'{PriceSnapshot.objects.count()} снимков '
        f'на {settings.PRICE_HORIZON_DAYS} дат.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
