"""Правила, которые обязана держать база, а не соглашение между разработчиками."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from monitor.models import (
    Apartment,
    Competitor,
    PriceKind,
    PriceSnapshot,
    SnapshotIsImmutable,
    SnapshotStatus,
    Source,
)

pytestmark = pytest.mark.django_db


def base_fields(**overrides):
    fields = {
        'price_kind': PriceKind.DATED,
        'check_in': timezone.localdate(),
        'nights': 1,
        'price_per_night': Decimal('5000.00'),
        'collected_at': timezone.now(),
        'source': Source.AVITO,
        'status': SnapshotStatus.OK,
    }
    fields.update(overrides)
    return fields


def test_snapshot_needs_exactly_one_owner(apartment, competitor):
    with pytest.raises(IntegrityError), transaction.atomic():
        PriceSnapshot.objects.create(**base_fields())

    with pytest.raises(IntegrityError), transaction.atomic():
        PriceSnapshot.objects.create(
            apartment=apartment, competitor=competitor, **base_fields()
        )


def test_listing_snapshot_needs_no_dates(competitor):
    """Витринная цена не привязана к датам — это её определение, а не недосмотр."""
    snapshot = PriceSnapshot.objects.create(
        competitor=competitor,
        **base_fields(price_kind=PriceKind.LISTING, check_in=None, nights=None),
    )
    assert snapshot.pk
    assert snapshot.check_in is None
    assert snapshot.nights is None


def test_listing_snapshot_rejects_dates(competitor):
    with pytest.raises(IntegrityError), transaction.atomic():
        PriceSnapshot.objects.create(
            competitor=competitor, **base_fields(price_kind=PriceKind.LISTING)
        )


def test_successful_dated_snapshot_requires_dates(competitor):
    with pytest.raises(IntegrityError), transaction.atomic():
        PriceSnapshot.objects.create(competitor=competitor, **base_fields(check_in=None))

    with pytest.raises(IntegrityError), transaction.atomic():
        PriceSnapshot.objects.create(competitor=competitor, **base_fields(nights=None))


def test_failed_dated_snapshot_may_have_no_dates(competitor):
    """Блокировка приходит до разбора страницы: на какую дату был запрос — неизвестно.

    Записать сбой всё равно надо, иначе неудача исчезает из истории.
    """
    snapshot = PriceSnapshot.objects.create(
        competitor=competitor,
        **base_fields(
            check_in=None,
            nights=None,
            price_per_night=None,
            status=SnapshotStatus.BLOCKED,
            error_note='HTTP 429',
        ),
    )
    assert snapshot.pk


def test_ok_snapshot_requires_price(competitor):
    """Исход «получено» без цены — тот самый ложный успех первого прогона разведки."""
    with pytest.raises(IntegrityError), transaction.atomic():
        PriceSnapshot.objects.create(
            competitor=competitor, **base_fields(price_per_night=None)
        )


def test_fees_included_is_unknown_by_default(competitor):
    snapshot = PriceSnapshot.objects.create(competitor=competitor, **base_fields())
    assert snapshot.fees_included is None


def test_repeated_collection_creates_new_row(competitor, today):
    first = PriceSnapshot.objects.create(competitor=competitor, **base_fields())
    second = PriceSnapshot.objects.create(
        competitor=competitor, **base_fields(price_per_night=Decimal('5500.00'))
    )

    assert first.pk != second.pk
    assert PriceSnapshot.objects.filter(competitor=competitor, check_in=today).count() == 2
    first.refresh_from_db()
    assert first.price_per_night == Decimal('5000.00')


def test_snapshot_cannot_be_changed(competitor):
    snapshot = PriceSnapshot.objects.create(competitor=competitor, **base_fields())
    snapshot.price_per_night = Decimal('1.00')
    with pytest.raises(SnapshotIsImmutable):
        snapshot.save()


def test_snapshot_cannot_be_deleted(competitor):
    snapshot = PriceSnapshot.objects.create(competitor=competitor, **base_fields())
    with pytest.raises(SnapshotIsImmutable):
        snapshot.delete()


def test_competitor_with_snapshots_is_protected(competitor):
    """Мягкое удаление — единственный способ убрать конкурента из работы."""
    PriceSnapshot.objects.create(competitor=competitor, **base_fields())

    with pytest.raises(ProtectedError), transaction.atomic():
        competitor.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        Competitor.objects.all().delete()

    assert Competitor.objects.filter(pk=competitor.pk).exists()


def test_apartment_with_snapshots_is_protected(apartment, competitor):
    """Каскад через конкурента упирается в тот же PROTECT — снимки переживают всё."""
    PriceSnapshot.objects.create(competitor=competitor, **base_fields())

    with pytest.raises(ProtectedError), transaction.atomic():
        apartment.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        Apartment.objects.all().delete()

    assert PriceSnapshot.objects.count() == 1


def test_soft_delete_keeps_snapshots(competitor):
    PriceSnapshot.objects.create(competitor=competitor, **base_fields())

    competitor.is_active = False
    competitor.save()

    assert PriceSnapshot.objects.filter(competitor=competitor).count() == 1


def test_competitor_url_is_unique_per_apartment(apartment, competitor):
    with pytest.raises(IntegrityError), transaction.atomic():
        Competitor.objects.create(apartment=apartment, url=competitor.url)

    other = Apartment.objects.create(title='Другая квартира')
    assert Competitor.objects.create(apartment=other, url=competitor.url).pk


@pytest.mark.parametrize(
    'url',
    [
        'https://www.avito.ru/moskva/kvartiry/1',
        'https://avito.ru/moskva/kvartiry/1',
        'https://m.avito.ru/moskva/kvartiry/1',
    ],
)
def test_avito_urls_pass_validation(apartment, url):
    Competitor(apartment=apartment, url=url).full_clean()


@pytest.mark.parametrize(
    'url',
    [
        'https://www.cian.ru/rent/flat/1/',
        'https://avito.ru.evil.example/moskva/1',
        'ftp://avito.ru/1',
    ],
)
def test_foreign_urls_fail_validation(apartment, url):
    """Одна и та же проверка работает и на странице квартиры, и в админке."""
    with pytest.raises(ValidationError):
        Competitor(apartment=apartment, url=url).full_clean()
