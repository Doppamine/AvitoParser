"""Фабрики для тестов. Все суммы — Decimal, ни одного float."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from monitor.models import (
    Apartment,
    Competitor,
    PriceKind,
    PriceSnapshot,
    SnapshotStatus,
    Source,
)


@pytest.fixture
def apartment(db):
    return Apartment.objects.create(title='Наша студия', address='Москва, Тверская, 1')


@pytest.fixture
def competitor(apartment):
    return Competitor.objects.create(
        apartment=apartment,
        url='https://www.avito.ru/moskva/kvartiry/1',
        title='Конкурент',
    )


@pytest.fixture
def today():
    return timezone.localdate()


@pytest.fixture
def make_snapshot():
    """Снимок на дату. По умолчанию — успешный, за одну ночь."""

    def factory(
        owner,
        check_in=None,
        *,
        price=Decimal('5000.00'),
        nights=1,
        status=SnapshotStatus.OK,
        price_kind=PriceKind.DATED,
        hours_ago=1,
        note='',
        source=Source.AVITO,
    ):
        key = 'competitor' if isinstance(owner, Competitor) else 'apartment'
        return PriceSnapshot.objects.create(
            **{key: owner},
            price_kind=price_kind,
            check_in=check_in,
            nights=nights,
            price_per_night=price,
            total_price=None if price is None or nights is None else price * nights,
            collected_at=timezone.now() - timedelta(hours=hours_ago),
            source=source,
            status=status,
            error_note=note,
        )

    return factory
