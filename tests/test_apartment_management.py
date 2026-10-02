"""Заведение и правка своих квартир из интерфейса.

Заказчицу в админку не пускаем, поэтому список объектов она ведёт сама:
на первой странице заводит, на странице квартиры правит.
"""

import pytest
from django.urls import reverse

from monitor.models import Apartment

pytestmark = pytest.mark.django_db

OWN_URL = 'https://www.avito.ru/moskva/kvartiry/nasha_studiya_1234567'

FIELDS = {
    'title': 'Студия на Мясницкой',
    'address': 'Москва, Мясницкая, 1',
    'min_nights': '2',
    'avito_url': OWN_URL,
}


def create(client, **overrides):
    return client.post(reverse('apartment-list'), {**FIELDS, **overrides}, follow=True)


def edit(client, apartment, params='', **overrides):
    url = reverse('apartment-detail', args=[apartment.pk]) + params
    return client.post(url, {**FIELDS, **overrides})


def messages_of(response):
    return [str(message) for message in response.context['messages']]


def test_apartment_is_created_from_the_list_page(client):
    response = create(client)

    apartment = Apartment.objects.get()
    assert apartment.title == 'Студия на Мясницкой'
    assert apartment.address == 'Москва, Мясницкая, 1'
    assert apartment.min_nights == 2
    assert apartment.is_active
    # Заводят квартиру ради конкурентов, поэтому сразу её страница, а не список.
    assert response.redirect_chain[-1][0] == reverse('apartment-detail', args=[apartment.pk])
    assert 'заведена' in ' '.join(messages_of(response))


def test_own_listing_is_required(client):
    """Без своей цены таблица перестаёт быть сравнением, поэтому ссылка обязательна."""
    response = create(client, avito_url='')

    assert not Apartment.objects.exists()
    assert 'сравнивать не с чем' in response.content.decode()


def test_own_listing_url_is_normalized(client):
    """Ссылку копируют из адресной строки, а там висят выбранные даты и гости."""
    create(client, avito_url=f'{OWN_URL}?checkIn=2026-09-20&checkOut=2026-09-23')

    assert Apartment.objects.get().avito_url == OWN_URL


def test_title_is_required(client):
    response = create(client, title='')

    assert not Apartment.objects.exists()
    assert response.status_code == 200


def test_foreign_own_listing_is_rejected(client):
    response = create(client, avito_url='https://www.cian.ru/rent/flat/1234567/')

    assert not Apartment.objects.exists()
    assert 'avito.ru' in response.content.decode()


def test_typed_values_survive_a_rejection(client):
    """Полей четыре: заставлять набирать всё заново из-за одной ошибки нельзя."""
    response = create(client, title='Пентхаус', avito_url='какая-то строка')

    assert not Apartment.objects.exists()
    assert 'Пентхаус' in response.content.decode()


@pytest.mark.parametrize('nights', ['0', '400'])
def test_absurd_min_nights_is_rejected(client, nights):
    assert not create(client, min_nights=nights).context['form'].is_valid()
    assert not Apartment.objects.exists()


def test_get_on_the_list_creates_nothing(client):
    response = client.get(reverse('apartment-list'))

    assert response.status_code == 200
    assert not Apartment.objects.exists()


def test_fields_are_edited_from_the_apartment_page(client, apartment):
    edit(client, apartment, title='Студия на Мясницкой, 1', min_nights='3',
         avito_url=OWN_URL)

    apartment.refresh_from_db()
    assert apartment.title == 'Студия на Мясницкой, 1'
    assert apartment.min_nights == 3
    assert apartment.avito_url == OWN_URL


def test_editing_returns_to_the_same_period(client, apartment):
    params = '?from=2026-09-20&to=2026-09-23&sort=price'

    response = edit(client, apartment, params)

    assert response.status_code == 302
    assert response.url == reverse('apartment-detail', args=[apartment.pk]) + params


def test_a_bad_url_leaves_the_apartment_untouched(client, apartment):
    response = edit(client, apartment, title='Новое имя',
                    avito_url='https://www.cian.ru/rent/flat/1/')

    apartment.refresh_from_db()
    assert apartment.title == 'Наша студия'
    # Страница возвращается сразу с ошибкой и с набранным текстом, без перехода.
    assert response.status_code == 200
    assert 'Новое имя' in response.content.decode()


def test_editing_touches_no_snapshots(client, apartment, competitor, today, make_snapshot):
    """Правка карточки — не сбор: история цен от неё не меняется."""
    make_snapshot(competitor, today)

    edit(client, apartment, title='Переименована')

    assert competitor.snapshots.count() == 1


def test_own_listing_of_another_apartment_is_rejected(client, apartment):
    """Одно объявление у двух квартир — это одна квартира, заведённая дважды."""
    apartment.avito_url = OWN_URL
    apartment.save()

    response = create(client, title='Та же, но ещё раз')

    assert Apartment.objects.count() == 1
    assert 'Наша студия' in response.content.decode()


def test_the_apartment_keeps_its_own_listing_on_edit(client, apartment):
    """Проверка дубля не должна натыкаться на саму правимую квартиру."""
    edit(client, apartment)
    response = edit(client, apartment, title='Переименована')

    apartment.refresh_from_db()
    assert response.status_code == 302
    assert apartment.title == 'Переименована'
    assert apartment.avito_url == OWN_URL


def test_own_listing_may_not_be_a_competitor_of_the_same_apartment(client, apartment,
                                                                   competitor):
    response = edit(client, apartment, avito_url=competitor.url)

    apartment.refresh_from_db()
    assert apartment.avito_url == ''
    assert response.status_code == 200
    assert 'конкурентом этой же квартиры' in response.content.decode()


def test_query_string_does_not_hide_a_duplicate(client, apartment, competitor):
    """Ссылка из адресной строки тащит выбранные даты — сравнивать надо нормализованное."""
    response = edit(client, apartment,
                    avito_url=f'{competitor.url}?checkIn=2026-09-20&guestsDetailed=%7B%7D')

    apartment.refresh_from_db()
    assert apartment.avito_url == ''
    assert 'конкурентом этой же квартиры' in response.content.decode()


def test_a_retired_competitor_does_not_block_the_own_listing(client, apartment, competitor):
    """Иначе описка запирала бы поле навсегда: удалить конкурента нечем."""
    competitor.is_active = False
    competitor.save(update_fields=['is_active'])

    response = edit(client, apartment, avito_url=competitor.url)

    apartment.refresh_from_db()
    assert response.status_code == 302
    assert apartment.avito_url == competitor.url
