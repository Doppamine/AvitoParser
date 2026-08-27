"""Страница сравнения. Вёрстку не проверяем, проверяем, что она показывает."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from monitor.models import Competitor, PriceKind, SnapshotStatus
from monitor.services import Interval, default_interval

pytestmark = pytest.mark.django_db


@pytest.fixture
def weekend():
    return default_interval()


@pytest.fixture
def detail_url(apartment):
    return reverse('apartment-detail', args=[apartment.pk])


def period(interval):
    return {'from': interval.check_in.isoformat(), 'to': interval.check_out.isoformat()}


def messages_of(response):
    return [str(message) for message in response.context['messages']]


def test_page_defaults_to_the_nearest_weekend(client, detail_url, weekend):
    response = client.get(detail_url)

    assert response.status_code == 200
    assert response.context['interval'] == weekend


def test_period_comes_from_the_address(client, detail_url, today):
    far = Interval(check_in=today + timedelta(days=24), nights=3)

    response = client.get(detail_url, period(far))

    assert response.context['interval'] == far
    assert response.context['table'].interval == far


def test_broken_period_shows_the_weekend_and_says_so(client, detail_url, weekend):
    response = client.get(detail_url, {'from': '2026-09-23', 'to': '2026-09-20'})

    assert response.context['interval'] == weekend
    assert 'Выезд должен быть позже заезда.' in messages_of(response)


def test_page_shows_our_apartment_first(client, detail_url, apartment, competitor, weekend, make_snapshot):
    make_snapshot(
        apartment, weekend.check_in, nights=weekend.nights,
        price=Decimal('5000.00'), source='realtycalendar',
    )
    make_snapshot(competitor, weekend.check_in, nights=weekend.nights, price=Decimal('4500.00'))

    response = client.get(detail_url)

    assert response.context['table'].ours.is_ours
    assert response.context['table'].ours.cell.price_per_night == Decimal('5000.00')
    assert response.context['rows'][0].cell.is_cheaper


def test_listing_price_is_absent_from_the_page(client, detail_url, competitor, weekend, make_snapshot):
    """Витринного числа не должно быть ни в контексте, ни в разметке."""
    make_snapshot(
        competitor, price_kind=PriceKind.LISTING, check_in=None, nights=None,
        price=Decimal('3333.00'),
    )
    make_snapshot(
        competitor, weekend.check_in, nights=weekend.nights, price=None,
        status=SnapshotStatus.CAPTCHA_OVERLAY,
    )

    response = client.get(detail_url)
    body = response.content.decode()

    assert '3333' not in body and '3 333' not in body
    table = response.context['table']
    assert not table.ours.cell.has_price
    assert not table.competitors[0].cell.has_price


def test_the_period_survives_sorting(client, detail_url, today, weekend):
    far = Interval(check_in=today + timedelta(days=24), nights=3)

    response = client.get(detail_url, period(far) | {'sort': 'price'})

    assert response.context['interval'] == far
    assert response.context['sort_by'] == 'price'
    assert 'from=' in response.context['links']['by_title']


def test_sorting_by_price(client, detail_url, apartment, weekend, make_snapshot):
    cheap = Competitor.objects.create(apartment=apartment, url='https://avito.ru/1', title='бета')
    dear = Competitor.objects.create(apartment=apartment, url='https://avito.ru/2', title='альфа')
    make_snapshot(cheap, weekend.check_in, nights=weekend.nights, price=Decimal('4000.00'))
    make_snapshot(dear, weekend.check_in, nights=weekend.nights, price=Decimal('8000.00'))

    by_name = client.get(detail_url).context['rows']
    by_price = client.get(detail_url, {'sort': 'price'}).context['rows']

    assert [row.title for row in by_name] == ['альфа', 'бета']
    assert [row.title for row in by_price] == ['бета', 'альфа']


def test_unknown_sort_value_is_ignored(client, detail_url):
    assert client.get(detail_url, {'sort': 'цена'}).context['sort_by'] == 'title'


def test_inactive_competitors_are_hidden_by_default(client, detail_url, apartment, competitor):
    Competitor.objects.create(
        apartment=apartment, url='https://avito.ru/old', title='снят', is_active=False
    )

    hidden = client.get(detail_url)
    shown = client.get(detail_url, {'show_inactive': '1'})

    assert [row.title for row in hidden.context['rows']] == [competitor.title]
    assert hidden.context['inactive_count'] == 1
    assert 'снят' in [row.title for row in shown.context['rows']]


def test_live_refresh_is_not_offered_yet(client, detail_url):
    """Место под кнопку заложено, но нажать её нельзя: сбора ещё нет."""
    body = client.get(detail_url).content.decode()

    assert 'Обновить цены' in body
    assert 'disabled' in body


def test_missing_apartment_gives_404(client, db):
    assert client.get(reverse('apartment-detail', args=[999])).status_code == 404


def test_index_counts_only_active_competitors(client, apartment, competitor):
    Competitor.objects.create(
        apartment=apartment, url='https://avito.ru/old', title='снят', is_active=False
    )

    response = client.get(reverse('apartment-list'))

    assert response.context['apartments'][0].active_competitors == 1


def test_index_shows_the_last_successful_collection(client, apartment, competitor, today, make_snapshot):
    make_snapshot(competitor, today, hours_ago=30)
    fresh = make_snapshot(competitor, today, hours_ago=2)
    make_snapshot(competitor, today, price=None, status=SnapshotStatus.FAILED, hours_ago=1)

    response = client.get(reverse('apartment-list'))

    assert response.context['apartments'][0].last_success == fresh.collected_at


def test_index_ignores_listing_prices_as_a_sign_of_success(client, apartment, competitor, make_snapshot):
    """Одна витринная цена не значит, что цены на даты собраны."""
    make_snapshot(competitor, price_kind=PriceKind.LISTING, check_in=None, nights=None)

    response = client.get(reverse('apartment-list'))

    assert response.context['apartments'][0].last_success is None


def test_index_survives_an_apartment_without_data(client, apartment):
    response = client.get(reverse('apartment-list'))

    assert response.context['apartments'][0].active_competitors == 0
    assert response.context['apartments'][0].last_success is None
