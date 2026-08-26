"""Сборка картины цен. Главное здесь — что витринная цена в неё не попадает."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from monitor.models import Competitor, PriceKind, SnapshotStatus
from monitor.services import horizon_dates, latest_prices, sort_rows

pytestmark = pytest.mark.django_db


def cell_of(table, row_title, index=0):
    for row in table.competitors:
        if row.title == row_title:
            return row.cells[index]
    raise AssertionError(f'нет строки {row_title}')


def test_listing_price_never_reaches_comparison(apartment, competitor, today, make_snapshot):
    """Если на дату есть только витринная цена, в ячейке прочерк, а не витринное число.

    Подстановка витринной цены вместо реальной — самая опасная ошибка проекта:
    система при этом выглядит полностью рабочей.
    """
    make_snapshot(
        competitor,
        price_kind=PriceKind.LISTING,
        check_in=None,
        nights=None,
        price=Decimal('3300.00'),
    )

    table = latest_prices(apartment, [today])
    cell = table.competitors[0].cells[0]

    assert not cell.has_price
    assert cell.price_per_night is None


def test_listing_price_is_not_used_as_fallback(apartment, competitor, today, make_snapshot):
    """Даже когда сбор на дату не удался, витринная цена не подставляется."""
    make_snapshot(
        competitor,
        price_kind=PriceKind.LISTING,
        check_in=None,
        nights=None,
        price=Decimal('3300.00'),
        hours_ago=1,
    )
    make_snapshot(
        competitor,
        today,
        price=None,
        status=SnapshotStatus.CAPTCHA_OVERLAY,
        note='Проверка перекрыла блок цен.',
        hours_ago=1,
    )

    cell = latest_prices(apartment, [today]).competitors[0].cells[0]

    assert not cell.has_price
    assert 'проверка поверх страницы' in cell.miss_reason


def test_latest_successful_snapshot_wins(apartment, competitor, today, make_snapshot):
    make_snapshot(competitor, today, price=Decimal('5000.00'), hours_ago=5)
    make_snapshot(competitor, today, price=Decimal('6000.00'), hours_ago=1)
    make_snapshot(competitor, today, price=Decimal('4000.00'), hours_ago=9)

    cell = latest_prices(apartment, [today]).competitors[0].cells[0]

    assert cell.price_per_night == Decimal('6000.00')


def test_last_known_price_survives_a_failed_run(apartment, competitor, today, make_snapshot):
    """Ручного ввода нет: показывать нечего, кроме вчерашнего числа с меткой времени."""
    make_snapshot(competitor, today, price=Decimal('5000.00'), hours_ago=26)
    make_snapshot(competitor, today, price=None, status=SnapshotStatus.FAILED, hours_ago=1)

    cell = latest_prices(apartment, [today]).competitors[0].cells[0]

    assert cell.price_per_night == Decimal('5000.00')
    assert cell.is_stale


def test_failed_snapshot_leaves_empty_cell_with_reason(apartment, competitor, today, make_snapshot):
    make_snapshot(
        competitor,
        today,
        price=None,
        status=SnapshotStatus.BLOCKED,
        note='HTTP 429',
        hours_ago=1,
    )

    cell = latest_prices(apartment, [today]).competitors[0].cells[0]

    assert not cell.has_price
    assert 'блокировка' in cell.miss_reason
    assert 'HTTP 429' in cell.miss_reason


def test_competitor_without_snapshots_keeps_its_row(apartment, competitor, today):
    table = latest_prices(apartment, horizon_dates(start=today))

    row = table.competitors[0]
    assert len(row.cells) == len(table.dates)
    assert all(cell.miss_reason == 'данных нет' for cell in row.cells)


def test_fresh_snapshot_is_not_stale(apartment, competitor, today, make_snapshot):
    make_snapshot(competitor, today, hours_ago=11)
    assert not latest_prices(apartment, [today]).competitors[0].cells[0].is_stale

    other = Competitor.objects.create(apartment=apartment, url='https://avito.ru/2')
    make_snapshot(other, today, hours_ago=13)
    assert cell_of(latest_prices(apartment, [today]), other.url).is_stale


def test_price_of_a_multi_night_range_is_marked_as_averaged(apartment, competitor, today, make_snapshot):
    """Цена за ночь при nights > 1 — среднее: выходные внутри диапазона дороже."""
    make_snapshot(competitor, today, nights=2, price=Decimal('5500.00'))

    cell = latest_prices(apartment, [today]).competitors[0].cells[0]

    assert cell.is_averaged
    assert cell.nights_label == '2 ночи'


def test_single_night_price_is_not_marked_as_averaged(apartment, competitor, today, make_snapshot):
    make_snapshot(competitor, today, nights=1)
    assert not latest_prices(apartment, [today]).competitors[0].cells[0].is_averaged


def test_cheaper_competitors_are_marked(apartment, competitor, today, make_snapshot):
    make_snapshot(apartment, today, price=Decimal('5000.00'), source='realtycalendar')
    make_snapshot(competitor, today, price=Decimal('4500.00'))
    dearer = Competitor.objects.create(apartment=apartment, url='https://avito.ru/3')
    make_snapshot(dearer, today, price=Decimal('5500.00'))

    table = latest_prices(apartment, [today])

    assert table.ours.cells[0].price_per_night == Decimal('5000.00')
    assert cell_of(table, competitor.title).is_cheaper
    assert not cell_of(table, dearer.url).is_cheaper


def test_snapshots_of_another_apartment_do_not_leak(apartment, competitor, today, make_snapshot):
    from monitor.models import Apartment

    stranger = Apartment.objects.create(title='Чужая квартира')
    stranger_competitor = Competitor.objects.create(
        apartment=stranger, url='https://avito.ru/stranger'
    )
    make_snapshot(stranger_competitor, today, price=Decimal('9999.00'))
    make_snapshot(competitor, today, price=Decimal('5000.00'))

    table = latest_prices(apartment, [today])

    assert len(table.competitors) == 1
    assert table.competitors[0].cells[0].price_per_night == Decimal('5000.00')


def test_horizon_length_comes_from_settings(settings, today):
    settings.PRICE_HORIZON_DAYS = 3
    dates = horizon_dates(start=today)

    assert dates == [today + timedelta(days=offset) for offset in range(3)]


def test_sort_rows_puts_missing_prices_last(apartment, today, make_snapshot):
    cheap = Competitor.objects.create(apartment=apartment, url='https://avito.ru/cheap', title='дешёвый')
    dear = Competitor.objects.create(apartment=apartment, url='https://avito.ru/dear', title='дорогой')
    Competitor.objects.create(apartment=apartment, url='https://avito.ru/none', title='без цены')
    make_snapshot(cheap, today, price=Decimal('4000.00'))
    make_snapshot(dear, today, price=Decimal('8000.00'))

    table = latest_prices(apartment, [today])
    titles = [row.title for row in sort_rows(table.competitors, today)]

    assert titles == ['дешёвый', 'дорогой', 'без цены']


def test_inactive_competitors_are_separated(apartment, competitor):
    Competitor.objects.create(
        apartment=apartment, url='https://avito.ru/old', title='снят', is_active=False
    )

    table = latest_prices(apartment, [timezone.localdate()])

    assert [row.title for row in table.active_competitors] == [competitor.title]
    assert [row.title for row in table.inactive_competitors] == ['снят']
