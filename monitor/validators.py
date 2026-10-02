"""Проверка ссылок на объявления. Одна и та же и на странице квартиры, и в админке."""

from urllib.parse import urlparse, urlsplit, urlunsplit

from django.core.exceptions import ValidationError

ALLOWED_HOST = 'avito.ru'


def validate_avito_url(value):
    """Ссылка должна вести на avito.ru: собирать цены мы умеем только оттуда."""
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https'):
        raise ValidationError('Ссылка должна начинаться с http:// или https://.')

    host = (parsed.hostname or '').lower()
    if host != ALLOWED_HOST and not host.endswith(f'.{ALLOWED_HOST}'):
        raise ValidationError(
            'Ссылка должна вести на avito.ru, а ведёт на %(host)s.',
            params={'host': host or 'неизвестный адрес'},
        )


# Площадка сама называет канонической ссылку с `www` и без параметров:
# именно она лежит в `seoCanonicalUrl` состояния страницы.
CANONICAL_HOST = 'www.avito.ru'
HOST_ALIASES = ('avito.ru', 'www.avito.ru', 'm.avito.ru')


def normalize_avito_url(value):
    """Схема, домен и путь. Все параметры запроса отбрасываются.

    Менеджер копирует ссылку из адресной строки, а там после выбора дат висят
    `checkIn`, `checkOut` и `guestsDetailed`. К объявлению они не относятся:
    это состояние просмотра, а не адрес объекта. Без нормализации одно и то же
    объявление с хвостом и без хвоста заводится дважды и даёт в таблице дубль,
    а сборщик приписывает свои параметры к чужим.

    `context` из ссылок поисковой выдачи отбрасывается вместе с остальными
    по той же причине: это метка сессии поиска, а не часть адреса объявления.
    """
    if not value:
        return value
    parsed = urlsplit(value.strip())

    host = (parsed.hostname or '').lower()
    if host in HOST_ALIASES:
        host = CANONICAL_HOST

    path = parsed.path.rstrip('/')
    return urlunsplit(('https', host, path, '', ''))
