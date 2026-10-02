"""Ведение списка конкурентов со страницы квартиры — повседневная работа менеджера."""

import pytest
from django.urls import reverse

from monitor.models import Competitor

pytestmark = pytest.mark.django_db

VALID_URL = 'https://www.avito.ru/moskva/kvartiry/studiya_30_m_1234567'


def add(client, apartment, url, **extra):
    return client.post(
        reverse('competitor-add', args=[apartment.pk]), {'url': url, **extra}, follow=True
    )


def messages_of(response):
    return [str(message) for message in response.context['messages']]


def test_competitor_is_added_from_the_apartment_page(client, apartment):
    response = add(client, apartment, VALID_URL)

    competitor = Competitor.objects.get(apartment=apartment)
    assert competitor.url == VALID_URL
    assert competitor.is_active
    assert 'Объявление добавлено' in ' '.join(messages_of(response))


def test_url_without_scheme_is_accepted(client, apartment):
    """Менеджер вставляет ссылку как скопировалась; схему дописываем сами."""
    add(client, apartment, 'www.avito.ru/moskva/kvartiry/studiya_1')

    assert Competitor.objects.get().url.startswith('https://')


def test_foreign_url_is_rejected(client, apartment):
    response = add(client, apartment, 'https://www.cian.ru/rent/flat/1234567/')

    assert not Competitor.objects.exists()
    assert 'avito.ru' in ' '.join(messages_of(response))


def test_garbage_input_is_rejected(client, apartment):
    response = add(client, apartment, 'какая-то строка')

    assert not Competitor.objects.exists()
    assert messages_of(response)


def test_duplicate_url_is_rejected(client, apartment):
    add(client, apartment, VALID_URL)
    response = add(client, apartment, VALID_URL)

    assert Competitor.objects.count() == 1
    assert 'уже есть в списке' in ' '.join(messages_of(response))


def test_duplicate_of_a_retired_competitor_explains_how_to_bring_it_back(client, apartment):
    add(client, apartment, VALID_URL)
    competitor = Competitor.objects.get()
    client.post(reverse('competitor-retire', args=[apartment.pk, competitor.pk]))

    response = add(client, apartment, VALID_URL)

    assert Competitor.objects.count() == 1
    assert 'верните его в работу' in ' '.join(messages_of(response))


def test_the_same_url_may_belong_to_another_apartment(client, apartment):
    from monitor.models import Apartment

    other = Apartment.objects.create(title='Другая квартира')
    add(client, apartment, VALID_URL)
    add(client, other, VALID_URL)

    assert Competitor.objects.count() == 2


def test_retire_is_soft_and_keeps_snapshots(client, apartment, competitor, today, make_snapshot):
    make_snapshot(competitor, today)

    client.post(reverse('competitor-retire', args=[apartment.pk, competitor.pk]))

    competitor.refresh_from_db()
    assert not competitor.is_active
    assert competitor.snapshots.count() == 1


def test_retired_competitor_leaves_the_table_but_can_be_restored(client, apartment, competitor):
    detail = reverse('apartment-detail', args=[apartment.pk])
    client.post(reverse('competitor-retire', args=[apartment.pk, competitor.pk]))

    assert client.get(detail).context['rows'] == []
    assert client.get(detail).context['inactive_count'] == 1

    client.post(reverse('competitor-restore', args=[apartment.pk, competitor.pk]))

    competitor.refresh_from_db()
    assert competitor.is_active
    assert len(client.get(detail).context['rows']) == 1


def test_a_competitor_of_another_apartment_cannot_be_touched(client, apartment, competitor):
    from monitor.models import Apartment

    other = Apartment.objects.create(title='Чужая квартира')

    response = client.post(reverse('competitor-retire', args=[other.pk, competitor.pk]))

    assert response.status_code == 404
    competitor.refresh_from_db()
    assert competitor.is_active


def test_get_requests_change_nothing(client, apartment, competitor):
    urls = [
        reverse('competitor-add', args=[apartment.pk]),
        reverse('competitor-retire', args=[apartment.pk, competitor.pk]),
        reverse('competitor-restore', args=[apartment.pk, competitor.pk]),
    ]

    assert [client.get(url).status_code for url in urls] == [405, 405, 405]


def test_the_period_survives_a_change(client, apartment, competitor, today):
    """После добавления менеджер возвращается к тому же периоду и той же сортировке."""
    from datetime import timedelta

    from monitor.services import Interval

    far = Interval(check_in=today + timedelta(days=24), nights=3)
    back = (
        f'from={far.check_in.isoformat()}&to={far.check_out.isoformat()}'
        '&sort=price&show_inactive=1'
    )

    response = add(client, apartment, VALID_URL, back=back)

    assert response.context['interval'] == far
    assert response.context['sort_by'] == 'price'
    assert response.context['show_inactive']


def test_only_known_view_params_come_back(client, apartment):
    """Из присланной строки берутся только параметры вида, всё прочее отбрасывается."""
    response = add(client, apartment, VALID_URL, back='next=https://evil.example/&show_inactive=1')

    target = response.redirect_chain[-1][0]
    assert target.startswith(reverse('apartment-detail', args=[apartment.pk]))
    assert 'evil.example' not in target
    assert 'next' not in target


def test_the_own_listing_may_not_be_added_as_a_competitor(client, apartment):
    """Строка, сравниваемая сама с собой, — не сравнение."""
    apartment.avito_url = VALID_URL
    apartment.save()

    response = add(client, apartment, VALID_URL)

    assert not Competitor.objects.exists()
    assert 'собственное объявление' in ' '.join(messages_of(response))


def test_the_own_listing_is_recognised_through_a_query_string(client, apartment):
    apartment.avito_url = VALID_URL
    apartment.save()

    response = add(client, apartment, f'{VALID_URL}?checkIn=2026-09-20')

    assert not Competitor.objects.exists()
    assert 'собственное объявление' in ' '.join(messages_of(response))


def test_a_competitor_that_became_the_own_listing_is_not_restored(client, apartment,
                                                                  competitor):
    """Пока конкурент лежал вне работы, его ссылка могла стать своим объявлением."""
    client.post(reverse('competitor-retire', args=[apartment.pk, competitor.pk]))
    apartment.avito_url = competitor.url
    apartment.save()

    response = client.post(
        reverse('competitor-restore', args=[apartment.pk, competitor.pk]), follow=True
    )

    competitor.refresh_from_db()
    assert not competitor.is_active
    assert 'вернуть нельзя' in ' '.join(messages_of(response))
