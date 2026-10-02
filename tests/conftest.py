"""Фабрики для тестов. Все суммы — Decimal, ни одного float."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
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
        guests=2,
    ):
        key = 'competitor' if isinstance(owner, Competitor) else 'apartment'
        return PriceSnapshot.objects.create(
            **{key: owner},
            price_kind=price_kind,
            check_in=check_in,
            nights=nights,
            price_per_night=price,
            total_price=None if price is None or nights is None else price * nights,
            guests=guests,
            collected_at=timezone.now() - timedelta(hours=hours_ago),
            source=source,
            status=status,
            error_note=note,
        )

    return factory


@pytest.fixture
def manager(db, django_user_model):
    """Пользователь интерфейса. Ролей и прав нет: вход один и открывает всё."""
    return django_user_model.objects.create_user(username='manager', password='secret')


@pytest.fixture
def client(client, manager):
    """Тесты страниц ходят под входом.

    Замещает клиента pytest-django целиком: вход закрыт посредником на всё
    приложение, и анонимный клиент отвечал бы переадресацией на каждой странице.
    Сама стена проверяется отдельно, через `anonymous_client`.
    """
    client.force_login(manager)
    return client


@pytest.fixture
def anonymous_client():
    return Client()
