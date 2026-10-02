"""Вход по логину и паролю. Проверяется стена, а не форма.

Инструмент внутренний, но лежит он в сети, и в нём адреса объявлений заказчицы
и её цены. Ни одна страница не должна отвечать анонимному посетителю.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def guarded_urls(apartment, competitor):
    return [
        reverse('apartment-list'),
        reverse('apartment-detail', args=[apartment.pk]),
        reverse('competitor-add', args=[apartment.pk]),
        reverse('competitor-retire', args=[apartment.pk, competitor.pk]),
        reverse('competitor-restore', args=[apartment.pk, competitor.pk]),
        '/admin/',
    ]


def test_every_page_is_closed_to_anonymous(anonymous_client, guarded_urls):
    for url in guarded_urls:
        response = anonymous_client.get(url)
        assert response.status_code == 302, url
        assert '/login' in response['Location'], url


def test_anonymous_post_does_not_change_anything(anonymous_client, apartment, competitor):
    """Стена стоит и на записи: переадресация — до обработчика, а не после."""
    response = anonymous_client.post(
        reverse('competitor-retire', args=[apartment.pk, competitor.pk])
    )

    competitor.refresh_from_db()
    assert response.status_code == 302
    assert competitor.is_active


def test_login_page_itself_is_open(anonymous_client):
    """Иначе вход переадресовывал бы сам на себя."""
    assert anonymous_client.get(reverse('login')).status_code == 200


def test_login_returns_to_the_requested_page(anonymous_client, manager, apartment):
    """Ссылка на период, присланная в переписке, после входа открывается сама."""
    target = reverse('apartment-detail', args=[apartment.pk]) + '?from=2026-09-20&to=2026-09-23'

    login_page = anonymous_client.get(target)
    response = anonymous_client.post(
        login_page['Location'],
        {'username': 'manager', 'password': 'secret', 'next': target},
    )

    assert response.status_code == 302
    assert response['Location'] == target


def test_wrong_password_does_not_let_in(anonymous_client, manager):
    response = anonymous_client.post(
        reverse('login'), {'username': 'manager', 'password': 'не тот'}
    )

    assert response.status_code == 200
    assert not response.wsgi_request.user.is_authenticated


def test_logout_closes_the_pages_again(client, apartment):
    """Выход — POST: ссылкой его срабатывала бы любая чужая картинка."""
    detail = reverse('apartment-detail', args=[apartment.pk])
    assert client.get(detail).status_code == 200

    client.post(reverse('logout'))

    assert client.get(detail).status_code == 302
