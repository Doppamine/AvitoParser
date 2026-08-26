"""Проверка ссылок на объявления. Одна и та же и на странице квартиры, и в админке."""

from urllib.parse import urlparse

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
