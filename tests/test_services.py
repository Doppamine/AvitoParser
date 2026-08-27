"""Сборка картины цен на период.

Здесь два главных правила: витринная цена в сравнение не попадает, и снимок ищется
по паре (дата заезда, число ночей), а не по одной дате.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from monitor.models import Competitor, PriceKind, SnapshotStatus
from monitor.services import (
    Interval,
    default_interval,
    latest_prices,
    parse_interval,
    sort_rows,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def interval(today):
    return Interval(check_in=today + timedelta(days=24), nights=3)


def only_cell(apartment, interval):
    return latest_prices(apartment, interval).competitors[0].cell


def row_named(table, title):
    for row in table.competitors:
        if row.title == title:
            return row
    raise AssertionError(f'нет строки {title}')


def test_interval_describes_a_stay():
    stay = Interval(check_in=date(2026, 9, 20), nights=3)

    assert stay.check_out == date(2026, 9, 23)
    assert stay.nights_label == '3 ночи'
    assert str(stay) == '20.09 — 23.09, 3 ночи'


def test_default_interval_is_the_nearest_weekend():
    """Пятница — воскресенье: то, с чего менеджер начинает чаще всего."""
    monday = date(2026, 8, 24)

    weekend = default_interval(today=monday)

    assert weekend == Interval(check_in=date(2026, 8, 28), nights=2)
    assert weekend.check_out == date(2026, 8, 30)


def test_default_interval_on_friday_is_the_same_day():
    friday = date(2026, 8, 28)

    assert default_interval(today=friday).check_in == friday


@pytest.mark.parametrize(
    'check_in, check_out, nights',
    [('2026-09-20', '2026-09-23', 3), ('2026-09-20', '2026-09-21', 1)],
)
def test_period_comes_from_the_address(check_in, check_out, nights):
    stay, error = parse_interval(check_in, check_out)

    assert error is None
    assert stay == Interval(check_in=date.fromisoformat(check_in), nights=nights)


@pytest.mark.parametrize(
    'check_in, check_out',
    [
        ('вчера', 'завтра'),
        ('2026-09-23', '2026-09-20'),
        ('2026-09-20', '2026-09-20'),
        ('2026-09-20', '2027-09-20'),
        ('2026-09-20', None),
    ],
)
def test_broken_period_falls_back_to_the_weekend(check_in, check_out):
    """Пустая страница из-за опечатки в дате хуже, чем ближайшие выходные."""
    monday = date(2026, 8, 24)

    stay, error = parse_interval(check_in, check_out, today=monday)

    assert stay == default_interval(today=monday)
    assert error


def test_no_period_in_the_address_means_the_weekend():
    monday = date(2026, 8, 24)

    stay, error = parse_interval(None, None, today=monday)

    assert stay == default_interval(today=monday)
    assert error is None


def test_price_is_taken_only_for_the_requested_period(apartment, competitor, interval, make_snapshot):
    """Цена за две ночи не подставляется вместо цены за три: это разные числа."""
    make_snapshot(competitor, interval.check_in, nights=2, price=Decimal('4000.00'))

    cell = only_cell(apartment, interval)

    assert not cell.has_price
    assert 'С той же датой заезда собрано: 2 ночи' in cell.miss_reason


def test_price_of_the_requested_period_is_shown_with_its_total(apartment, competitor, interval, make_snapshot):
    make_snapshot(competitor, interval.check_in, nights=3, price=Decimal('5000.00'))

    cell = only_cell(apartment, interval)

    assert cell.price_per_night == Decimal('5000.00')
    assert cell.total_price == Decimal('15000.00')
    assert cell.nights_label == '3 ночи'


def test_another_check_in_date_is_not_used(apartment, competitor, interval, make_snapshot):
    make_snapshot(competitor, interval.check_in + timedelta(days=1), nights=3)

    assert not only_cell(apartment, interval).has_price


def test_listing_price_never_reaches_comparison(apartment, competitor, interval, make_snapshot):
    """Если есть только витринная цена, в ячейке прочерк, а не витринное число.

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

    assert not only_cell(apartment, interval).has_price


def test_listing_price_is_not_used_as_fallback(apartment, competitor, interval, make_snapshot):
    make_snapshot(
        competitor, price_kind=PriceKind.LISTING, check_in=None, nights=None,
        price=Decimal('3300.00'),
    )
    make_snapshot(
        competitor, interval.check_in, nights=interval.nights, price=None,
        status=SnapshotStatus.CAPTCHA_OVERLAY, note='Проверка перекрыла блок цен.',
    )

    cell = only_cell(apartment, interval)

    assert not cell.has_price
    assert 'проверка поверх страницы' in cell.miss_reason


def test_latest_successful_snapshot_wins(apartment, competitor, interval, make_snapshot):
    for hours_ago, price in [(5, '5000.00'), (1, '6000.00'), (9, '4000.00')]:
        make_snapshot(
            competitor, interval.check_in, nights=interval.nights,
            price=Decimal(price), hours_ago=hours_ago,
        )

    assert only_cell(apartment, interval).price_per_night == Decimal('6000.00')


def test_last_known_price_survives_a_failed_run(apartment, competitor, interval, make_snapshot):
    """Ручного ввода нет: показывать нечего, кроме вчерашнего числа с меткой времени."""
    make_snapshot(
        competitor, interval.check_in, nights=interval.nights,
        price=Decimal('5000.00'), hours_ago=26,
    )
    make_snapshot(
        competitor, interval.check_in, nights=interval.nights, price=None,
        status=SnapshotStatus.FAILED, hours_ago=1,
    )

    cell = only_cell(apartment, interval)

    assert cell.price_per_night == Decimal('5000.00')
    assert cell.is_stale


def test_failed_snapshot_leaves_empty_cell_with_reason(apartment, competitor, interval, make_snapshot):
    make_snapshot(
        competitor, interval.check_in, nights=interval.nights, price=None,
        status=SnapshotStatus.BLOCKED, note='HTTP 429', hours_ago=1,
    )

    cell = only_cell(apartment, interval)

    assert not cell.has_price
    assert 'блокировка' in cell.miss_reason
    assert 'HTTP 429' in cell.miss_reason


def test_competitor_without_snapshots_keeps_its_row(apartment, competitor, interval):
    cell = only_cell(apartment, interval)

    assert not cell.has_price
    assert cell.miss_reason == 'данных нет'


def test_fresh_snapshot_is_not_stale(apartment, competitor, interval, make_snapshot):
    make_snapshot(competitor, interval.check_in, nights=interval.nights, hours_ago=11)
    assert not only_cell(apartment, interval).is_stale

    other = Competitor.objects.create(apartment=apartment, url='https://avito.ru/2')
    make_snapshot(other, interval.check_in, nights=interval.nights, hours_ago=13)
    assert row_named(latest_prices(apartment, interval), other.url).cell.is_stale


def test_cheaper_competitors_are_marked(apartment, competitor, interval, make_snapshot):
    make_snapshot(
        apartment, interval.check_in, nights=interval.nights,
        price=Decimal('5000.00'), source='realtycalendar',
    )
    make_snapshot(competitor, interval.check_in, nights=interval.nights, price=Decimal('4500.00'))
    dearer = Competitor.objects.create(apartment=apartment, url='https://avito.ru/3')
    make_snapshot(dearer, interval.check_in, nights=interval.nights, price=Decimal('5500.00'))

    table = latest_prices(apartment, interval)

    assert table.ours.cell.price_per_night == Decimal('5000.00')
    assert row_named(table, competitor.title).cell.is_cheaper
    assert not row_named(table, dearer.url).cell.is_cheaper


def test_snapshots_of_another_apartment_do_not_leak(apartment, competitor, interval, make_snapshot):
    from monitor.models import Apartment

    stranger = Apartment.objects.create(title='Чужая квартира')
    stranger_competitor = Competitor.objects.create(
        apartment=stranger, url='https://avito.ru/stranger'
    )
    make_snapshot(stranger_competitor, interval.check_in, nights=interval.nights, price=Decimal('9999.00'))
    make_snapshot(competitor, interval.check_in, nights=interval.nights, price=Decimal('5000.00'))

    table = latest_prices(apartment, interval)

    assert len(table.competitors) == 1
    assert table.competitors[0].cell.price_per_night == Decimal('5000.00')


def test_sort_by_price_puts_missing_prices_last(apartment, interval, make_snapshot):
    cheap = Competitor.objects.create(apartment=apartment, url='https://avito.ru/cheap', title='дешёвый')
    dear = Competitor.objects.create(apartment=apartment, url='https://avito.ru/dear', title='дорогой')
    Competitor.objects.create(apartment=apartment, url='https://avito.ru/none', title='без цены')
    make_snapshot(cheap, interval.check_in, nights=interval.nights, price=Decimal('4000.00'))
    make_snapshot(dear, interval.check_in, nights=interval.nights, price=Decimal('8000.00'))

    table = latest_prices(apartment, interval)

    assert [row.title for row in sort_rows(table.competitors, 'price')] == [
        'дешёвый', 'дорогой', 'без цены',
    ]
    assert [row.title for row in sort_rows(table.competitors)] == [
        'без цены', 'дешёвый', 'дорогой',
    ]


def test_inactive_competitors_are_separated(apartment, competitor, interval):
    Competitor.objects.create(
        apartment=apartment, url='https://avito.ru/old', title='снят', is_active=False
    )

    table = latest_prices(apartment, interval)

    assert [row.title for row in table.active_competitors] == [competitor.title]
    assert [row.title for row in table.inactive_competitors] == ['снят']


def test_horizon_setting_no_longer_drives_the_page(apartment, interval, settings, make_snapshot):
    """Страница показывает выбранный период, а не окно фонового сбора."""
    settings.PRICE_HORIZON_DAYS = 1
    make_snapshot(apartment, interval.check_in, nights=interval.nights, source='realtycalendar')

    table = latest_prices(apartment, interval)

    assert table.interval == interval
    assert table.ours.cell.has_price


def test_a_period_a_month_away_is_ordinary(apartment, competitor, make_snapshot):
    """Менеджер смотрит произвольный интервал в будущем, а не ближайшие дни."""
    far = Interval(check_in=timezone.localdate() + timedelta(days=24), nights=3)
    make_snapshot(competitor, far.check_in, nights=far.nights, price=Decimal('7000.00'))

    assert only_cell(apartment, far).price_per_night == Decimal('7000.00')
