"""Страница сравнения. Вёрстку не проверяем, проверяем, что она показывает."""

from decimal import Decimal

import pytest
from django.urls import reverse

from monitor.models import Competitor, PriceKind, SnapshotStatus

pytestmark = pytest.mark.django_db


def test_page_shows_our_apartment_first(client, apartment, competitor, today, make_snapshot):
    make_snapshot(apartment, today, price=Decimal('5000.00'), source='realtycalendar')
    make_snapshot(competitor, today, price=Decimal('4500.00'))

    response = client.get(reverse('apartment-detail', args=[apartment.pk]))

    assert response.status_code == 200
    assert response.context['table'].ours.is_ours
    assert response.context['table'].ours.title == apartment.title


def test_listing_price_is_absent_from_the_page(client, apartment, competitor, today, make_snapshot):
    """Витринного числа не должно быть ни в контексте, ни в разметке."""
    make_snapshot(
        competitor,
        price_kind=PriceKind.LISTING,
        check_in=None,
        nights=None,
        price=Decimal('3333.00'),
    )
    make_snapshot(
        competitor, today, price=None, status=SnapshotStatus.CAPTCHA_OVERLAY
    )

    response = client.get(reverse('apartment-detail', args=[apartment.pk]))
    body = response.content.decode()

    assert '3333' not in body
    assert '3 333' not in body
    rows = [response.context['table'].ours] + response.context['table'].competitors
    assert all(not cell.has_price for row in rows for cell in row.cells)


def test_column_count_follows_the_horizon_setting(client, apartment, settings):
    settings.PRICE_HORIZON_DAYS = 4

    response = client.get(reverse('apartment-detail', args=[apartment.pk]))

    assert len(response.context['table'].dates) == 4


def test_sorting_by_a_date_column(client, apartment, today, make_snapshot):
    cheap = Competitor.objects.create(apartment=apartment, url='https://avito.ru/1', title='бета')
    dear = Competitor.objects.create(apartment=apartment, url='https://avito.ru/2', title='альфа')
    make_snapshot(cheap, today, price=Decimal('4000.00'))
    make_snapshot(dear, today, price=Decimal('8000.00'))

    url = reverse('apartment-detail', args=[apartment.pk])
    by_name = client.get(url).context['rows']
    by_price = client.get(url, {'sort': today.isoformat()}).context['rows']

    assert [row.title for row in by_name] == ['альфа', 'бета']
    assert [row.title for row in by_price] == ['бета', 'альфа']


def test_unknown_sort_value_is_ignored(client, apartment):
    url = reverse('apartment-detail', args=[apartment.pk])

    assert client.get(url, {'sort': 'вчера'}).context['sort_date'] is None
    # Дата вне горизонта — сортировать не по чему.
    assert client.get(url, {'sort': '2020-01-01'}).context['sort_date'] is None


def test_inactive_competitors_are_hidden_by_default(client, apartment, competitor):
    Competitor.objects.create(
        apartment=apartment, url='https://avito.ru/old', title='снят', is_active=False
    )
    url = reverse('apartment-detail', args=[apartment.pk])

    hidden = client.get(url)
    shown = client.get(url, {'show_inactive': '1'})

    assert [row.title for row in hidden.context['rows']] == [competitor.title]
    assert hidden.context['inactive_count'] == 1
    assert 'снят' in [row.title for row in shown.context['rows']]


def test_missing_apartment_gives_404(client, db):
    assert client.get(reverse('apartment-detail', args=[999])).status_code == 404
