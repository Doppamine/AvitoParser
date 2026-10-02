"""Команда `collect` от начала до конца, на настоящих дампах фазы 0.

Живого сбора здесь нет: страницы берутся из подставного режима. Проверяется
то, ради чего команда и написана, — что в таблице показа стоят числа из блока
«Цены по датам», а не витринные, и что прочерк всегда объяснён.
"""

import csv
import json
import shutil
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from monitor.models import Apartment, Competitor, PriceKind, PriceSnapshot, SnapshotStatus

FIXTURE_DUMPS = Path(__file__).parent / 'fixtures' / 'dumps'

# Дампы фазы 0 и объявления, которым они соответствуют.
PAGES = {
    'https://www.avito.ru/moskva/kvartiry/1': '20260825-230710_p1_u0_browser.html',
    'https://www.avito.ru/moskva/kvartiry/2': '20260825-230710_p1_u1_browser.html',
    'https://www.avito.ru/moskva/kvartiry/3': '20260825-230710_p1_u1_http_ck.html',
    'https://www.avito.ru/moskva/kvartiry/4': '20260825-230710_p1_u3_browser.html',
}


@pytest.fixture
def replay_dir(tmp_path):
    """Папка подставного режима из настоящих дампов."""
    for name in set(PAGES.values()):
        shutil.copy2(FIXTURE_DUMPS / name, tmp_path / name)
    (tmp_path / 'replay.json').write_text(
        json.dumps(PAGES, ensure_ascii=False), encoding='utf-8'
    )
    return tmp_path


@pytest.fixture
def apartment_with_competitors(db):
    apartment = Apartment.objects.create(title='Наша студия', address='Москва, Окружной пр., 10Б')
    for url in PAGES:
        Competitor.objects.create(apartment=apartment, url=url)
    return apartment


def run(apartment, replay_dir, **kwargs):
    out, err = StringIO(), StringIO()
    call_command('collect', apartment=apartment.pk, replay=str(replay_dir),
                 stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def test_collect_writes_dated_snapshots_from_real_dumps(
    apartment_with_competitors, replay_dir, settings
):
    settings.COLLECT_PAUSE_SECONDS = (0, 0)
    out, _ = run(apartment_with_competitors, replay_dir, no_demo=True)

    # На дампе с проверкой поверх страницы обход останавливается, поэтому
    # доходим до него и не дальше — это и есть правило «встать, а не идти».
    assert 'Квартира: Наша студия' in out
    assert PriceSnapshot.objects.filter(
        price_kind=PriceKind.DATED, status=SnapshotStatus.OK
    ).exists()


def test_listing_price_never_reaches_the_table(
    apartment_with_competitors, replay_dir, settings
):
    """Витринных 3 350 и 3 800 в таблице показа быть не должно."""
    settings.COLLECT_PAUSE_SECONDS = (0, 0)
    out, _ = run(apartment_with_competitors, replay_dir, no_demo=True)

    table = out.split('=' * 78)[-1]
    assert '3 350' not in table
    assert '3 800' not in table
    assert 'в сравнение не идёт' in out


def test_requested_period_without_data_shows_a_dash_with_reason(
    apartment_with_competitors, replay_dir, settings
):
    """Период, которого в блоке нет, даёт прочерк и объяснение, а не число."""
    settings.COLLECT_PAUSE_SECONDS = (0, 0)
    check_in = date(2026, 9, 20)
    out, _ = run(
        apartment_with_competitors, replay_dir, no_demo=True,
        check_in=check_in.isoformat(), check_out=(check_in + timedelta(days=3)).isoformat(),
    )

    table = out.split('=' * 78)[-1]
    assert '—' in table
    assert not PriceSnapshot.objects.filter(
        price_kind=PriceKind.DATED, status=SnapshotStatus.OK,
        check_in=check_in, nights=3,
    ).exists()


def test_demo_folder_gets_table_files(
    apartment_with_competitors, replay_dir, settings, tmp_path, monkeypatch
):
    settings.COLLECT_PAUSE_SECONDS = (0, 0)
    demo_root = tmp_path / 'demo'
    monkeypatch.setattr('monitor.management.commands.collect.DEMO_ROOT', demo_root)

    run(apartment_with_competitors, replay_dir)

    folders = list(demo_root.iterdir())
    assert len(folders) == 1
    assert (folders[0] / 'table.md').exists()
    assert (folders[0] / 'table.csv').exists()

    with (folders[0] / 'table.csv').open(encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert all(row['title'] for row in rows)


def test_live_mode_builds_a_browser_fetcher_without_starting_it(db):
    """Браузер не поднимается, пока не понадобится первая страница."""
    from monitor.collector.browser import BrowserFetcher
    from monitor.management.commands.collect import Command

    fetcher = Command()._fetcher(
        {'replay': None, 'headless': True, 'warmup': False}, guests=2
    )
    assert isinstance(fetcher, BrowserFetcher)
    assert fetcher._page is None
    assert fetcher.guests == 2


def test_guests_are_recorded_on_every_snapshot(
    apartment_with_competitors, replay_dir, settings
):
    """Число гостей едет в снимок: без него снимки несопоставимы между собой."""
    settings.COLLECT_PAUSE_SECONDS = (0, 0)
    run(apartment_with_competitors, replay_dir, no_demo=True, guests=3)

    assert PriceSnapshot.objects.exists()
    assert set(PriceSnapshot.objects.values_list('guests', flat=True)) == {3}


def test_table_says_how_many_guests(apartment_with_competitors, replay_dir, settings):
    settings.COLLECT_PAUSE_SECONDS = (0, 0)
    out, _ = run(apartment_with_competitors, replay_dir, no_demo=True, guests=2)
    assert 'Цены на 2 гостей' in out


def test_unknown_apartment(db, replay_dir):
    with pytest.raises(CommandError, match='нет'):
        call_command('collect', apartment=999, replay=str(replay_dir),
                     stdout=StringIO(), stderr=StringIO())


def test_apartment_without_competitors(db, replay_dir):
    apartment = Apartment.objects.create(title='Пустая')
    with pytest.raises(CommandError, match='Собирать нечего'):
        call_command('collect', apartment=apartment.pk, replay=str(replay_dir),
                     stdout=StringIO(), stderr=StringIO())
