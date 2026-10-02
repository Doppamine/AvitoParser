"""Ссылка на объявление приводится к каноническому виду при сохранении.

Менеджер копирует ссылку из адресной строки, а там после выбора дат висят
`checkIn`, `checkOut` и `guestsDetailed`; в ссылке из выдачи — метка сессии
`context`. К объявлению они не относятся, и без нормализации одно объявление
заводится дважды: в таблице дубль, а сборщик дописывает своё к чужому.
"""

from datetime import date

import pytest

from monitor.forms import CompetitorAddForm
from monitor.models import Apartment, Competitor
from monitor.validators import normalize_avito_url

CLEAN = 'https://www.avito.ru/moskva/kvartiry/kvartira-studiya_24_m_7628611904'

# Списано из адресной строки после выбора 20–23 сентября на двоих.
FROM_ADDRESS_BAR = (
    CLEAN + '?guestsDetailed=%7B%22version%22%3A1%2C%22totalCount%22%3A2'
    '%2C%22adultsCount%22%3A2%2C%22children%22%3A%5B%5D%7D'
    '&checkIn=2026-09-20&checkOut=2026-09-23'
)

FROM_SEARCH = CLEAN + '?context=H4sIAAAAAAAA_wE_AMD_YToyOntzOjEzOiJsb2NhbFB'


# ------------------------------------------------------------- сама функция


@pytest.mark.parametrize('given', [
    CLEAN,
    FROM_ADDRESS_BAR,
    FROM_SEARCH,
    CLEAN + '/',
    CLEAN + '#gallery',
    CLEAN.replace('https://www.', 'https://'),
    CLEAN.replace('https://www.', 'http://m.'),
    CLEAN.replace('www.AVITO', 'www.avito').replace('avito.ru', 'AVITO.RU'),
])
def test_all_forms_normalize_to_one(given):
    assert normalize_avito_url(given) == CLEAN


def test_path_is_kept_intact():
    """Путь — это и есть адрес объявления, его трогать нельзя."""
    assert normalize_avito_url(FROM_ADDRESS_BAR).endswith('_7628611904')


def test_empty_value_stays_empty():
    """У квартиры ссылки может не быть вовсе, и это не повод падать."""
    assert normalize_avito_url('') == ''


# ----------------------------------------------------------------- модель


def test_competitor_url_is_normalized_on_save(db):
    apartment = Apartment.objects.create(title='Наша студия')
    competitor = Competitor.objects.create(apartment=apartment, url=FROM_ADDRESS_BAR)
    competitor.refresh_from_db()
    assert competitor.url == CLEAN


def test_apartment_url_is_normalized_on_save(db):
    apartment = Apartment.objects.create(title='Наша студия', avito_url=FROM_SEARCH)
    apartment.refresh_from_db()
    assert apartment.avito_url == CLEAN


def test_address_bar_link_and_clean_link_are_one_competitor(db):
    """Тот самый случай: ссылка с хвостом и без хвоста — одно объявление.

    Без нормализации это две строки в таблице по одному и тому же объявлению.
    """
    apartment = Apartment.objects.create(title='Наша студия')
    Competitor.objects.create(apartment=apartment, url=FROM_ADDRESS_BAR)

    form = CompetitorAddForm({'url': CLEAN}, apartment=apartment)

    assert not form.is_valid()
    assert 'уже есть в списке' in form.errors['url'][0]
    assert apartment.competitors.count() == 1


def test_clean_link_first_then_address_bar_link(db):
    """Порядок значения не имеет: дубль ловится с любой стороны."""
    apartment = Apartment.objects.create(title='Наша студия')
    Competitor.objects.create(apartment=apartment, url=CLEAN)

    form = CompetitorAddForm({'url': FROM_ADDRESS_BAR}, apartment=apartment)

    assert not form.is_valid()
    assert apartment.competitors.count() == 1


def test_search_link_is_the_same_competitor_too(db):
    apartment = Apartment.objects.create(title='Наша студия')
    Competitor.objects.create(apartment=apartment, url=FROM_SEARCH)

    form = CompetitorAddForm({'url': FROM_ADDRESS_BAR}, apartment=apartment)

    assert not form.is_valid()


def test_soft_deleted_duplicate_is_recognised_by_normalized_form(db):
    """Убранный из работы дубль должен узнаваться, а не заводиться заново."""
    apartment = Apartment.objects.create(title='Наша студия')
    Competitor.objects.create(apartment=apartment, url=CLEAN, is_active=False)

    form = CompetitorAddForm({'url': FROM_ADDRESS_BAR}, apartment=apartment)

    assert not form.is_valid()
    assert 'убрано из работы' in form.errors['url'][0]


def test_different_listings_stay_different(db):
    apartment = Apartment.objects.create(title='Наша студия')
    Competitor.objects.create(apartment=apartment, url=CLEAN)

    other = CLEAN.replace('7628611904', '4727479076')
    form = CompetitorAddForm({'url': other}, apartment=apartment)

    assert form.is_valid()
    form.save()
    assert apartment.competitors.count() == 2


def test_same_link_under_two_apartments_is_allowed(db):
    """Одно объявление может быть конкурентом двум нашим квартирам сразу."""
    first = Apartment.objects.create(title='Первая')
    second = Apartment.objects.create(title='Вторая')
    Competitor.objects.create(apartment=first, url=CLEAN)

    form = CompetitorAddForm({'url': FROM_ADDRESS_BAR}, apartment=second)

    assert form.is_valid()


# -------------------------------------------------------- адрес для сбора


def test_collector_builds_its_url_from_the_canonical_one(settings):
    """Сборщик не дописывает своё к чужому: сначала канонический вид."""
    from monitor.collector.avito_url import with_dates
    from monitor.services import Interval

    settings.COLLECT_GUESTS = 2
    url = with_dates(FROM_ADDRESS_BAR, Interval(check_in=date(2026, 10, 5), nights=2))

    assert url.count('checkIn') == 1
    assert url.count('guestsDetailed') == 1
    assert 'context' not in url
    assert '2026-09-20' not in url
    assert url.startswith(CLEAN + '?')


def test_collector_url_drops_the_search_context(settings):
    from monitor.collector.avito_url import with_dates

    settings.COLLECT_GUESTS = 2
    assert 'context' not in with_dates(FROM_SEARCH)
