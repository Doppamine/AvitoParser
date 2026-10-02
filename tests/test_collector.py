"""Тесты сборщика. Целиком на подставном режиме — ни одного обращения к Avito.

Проверяются ровно те сценарии, ради которых очередь и живёт в базе: блокировка
посреди обхода, проверка поверх страницы, снятое объявление и обрыв на середине.
Вживую они либо не воспроизводятся, либо стоят обращений к площадке.
"""

import asyncio
import json
import pathlib
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from monitor.collector.browser import BrowserFetcher
from monitor.collector.fetcher import FetchResult, ReplayFetcher
from monitor.collector.runner import run_queue
from monitor.models import (
    CollectTask,
    Competitor,
    PriceKind,
    PriceSnapshot,
    SnapshotStatus,
    TaskState,
)

TODAY = timezone.localdate()


# ----------------------------------------------------------- заготовки страниц


def item_state(price=3350, title='Квартира-студия, 25 м²', **flags):
    return {
        'priceString': f'{price}&nbsp;₽ за сутки',
        'ga': [{'itemPrice': price}],
        'item': {
            'title': title,
            'price': price,
            'formattedPrice': {'value': price, 'formatedString': f'{price}&nbsp;₽'},
            'isActive': True,
            **flags,
        },
    }


def page_html(*, buyer_item=None, body=''):
    parts = ['<html><head><title>Тест</title></head><body>', body]
    if buyer_item is not None:
        state = {'loaderData': {'catalog-or-main-or-item': {'buyerItem': buyer_item}}}
        literal = json.dumps(json.dumps(state, ensure_ascii=False))
        parts.append(
            f'<script>window.__staticRouterHydrationData = JSON.parse({literal});</script>'
        )
    parts.append('</body></html>')
    return ''.join(parts)


MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
          'августа', 'сентября', 'октября', 'ноября', 'декабря']


def label_for(check_in, check_out):
    return (f'{check_in.day} {MONTHS[check_in.month - 1][:3]}'
            f'—{check_out.day} {MONTHS[check_out.month - 1]}')


def dates_block(periods):
    """Карусель как на живой странице: карточки-li с точными датами в data-id."""
    cards = ''.join(
        f'<li data-id="{check_in}--{check_out}" role="option"><div>'
        f'<span>{total}&nbsp;₽</span><span>{label_for(check_in, check_out)}</span>'
        f'</div></li>'
        for total, check_in, check_out in periods
    )
    return f'<ul data-marker="nearest-dates" role="listbox">{cards}</ul>'


def good_page(price=3350, title='Конкурент из дампа', total='11 452'):
    """Страница с ценой на период: заезд завтра, две ночи."""
    check_in = TODAY + timedelta(days=1)
    return page_html(
        buyer_item=item_state(price, title),
        body=dates_block([(total, check_in, check_in + timedelta(days=2))]),
    )


BLOCKED_PAGE = '<html><head><title>Доступ ограничен</title></head><body>Доступ ограничен: проблема с IP</body></html>'
OVERLAY_PAGE = page_html(buyer_item=item_state(3500),
                         body='<div>Сервис недоступен. Попробуйте позже</div>')
GONE_PAGE = page_html(buyer_item=item_state(3350, isActive=False))
NO_BLOCK_PAGE = page_html(buyer_item=item_state(3800))


# ---------------------------------------------------------------- вспомогатели


@pytest.fixture
def no_sleep():
    """Паузы в тестах не нужны, а без подмены обход занял бы минуты."""
    return lambda seconds: None


def make_competitor(apartment, url, title=''):
    return Competitor.objects.create(apartment=apartment, url=url, title=title)


def make_task(owner, *, check_in=None, nights=None):
    key = 'competitor' if isinstance(owner, Competitor) else 'apartment'
    return CollectTask.objects.create(
        **{key: owner}, check_in=check_in, nights=nights
    )


# ------------------------------------------------------------------- сценарии


def test_two_tasks_produce_dated_and_listing_snapshots(apartment, no_sleep):
    first = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    second = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/2')
    make_task(first)
    make_task(second)

    fetcher = ReplayFetcher({first.url: good_page(), second.url: good_page(3800)})
    report = run_queue(fetcher, sleep=no_sleep)

    assert not report.stopped
    assert CollectTask.objects.filter(state=TaskState.DONE).count() == 2
    assert PriceSnapshot.objects.filter(price_kind=PriceKind.DATED,
                                        status=SnapshotStatus.OK).count() == 2
    assert PriceSnapshot.objects.filter(price_kind=PriceKind.LISTING).count() == 2


def test_listing_price_is_never_written_as_dated(apartment, no_sleep):
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor)

    run_queue(ReplayFetcher({competitor.url: good_page(price=3350, total='11 452')}),
              sleep=no_sleep)

    dated = PriceSnapshot.objects.get(price_kind=PriceKind.DATED)
    assert dated.price_per_night == Decimal('5726.00')
    assert dated.total_price == Decimal('11452.00')
    assert dated.price_per_night != Decimal('3350')


def test_block_stops_the_whole_run(apartment, no_sleep):
    """Проверка площадки останавливает обход: следующие задания не трогаем."""
    first = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    second = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/2')
    third = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/3')
    task_one, task_two, task_three = (make_task(c) for c in (first, second, third))

    fetcher = ReplayFetcher({
        first.url: good_page(),
        second.url: BLOCKED_PAGE,
        third.url: good_page(),
    })
    report = run_queue(fetcher, sleep=no_sleep)

    assert report.stopped
    task_one.refresh_from_db(); task_two.refresh_from_db(); task_three.refresh_from_db()
    assert task_one.state == TaskState.DONE
    assert task_two.state == TaskState.DEFERRED
    assert task_three.state == TaskState.PENDING
    assert task_two.deferred_until is not None
    assert PriceSnapshot.objects.filter(status=SnapshotStatus.BLOCKED).count() == 1
    # До третьего объявления сборщик не дошёл — иначе он усугублял бы блокировку.
    assert [url for url, _ in fetcher.calls] == [first.url, second.url]


def test_captcha_overlay_defers_and_keeps_listing_price(apartment, no_sleep):
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor)

    report = run_queue(ReplayFetcher({competitor.url: OVERLAY_PAGE}), sleep=no_sleep)

    task.refresh_from_db()
    assert report.stopped
    assert task.state == TaskState.DEFERRED
    assert PriceSnapshot.objects.filter(price_kind=PriceKind.DATED,
                                        status=SnapshotStatus.CAPTCHA_OVERLAY).count() == 1
    listing = PriceSnapshot.objects.get(price_kind=PriceKind.LISTING)
    assert listing.price_per_night == Decimal('3500')


def test_gone_item_does_not_stop_the_run(apartment, no_sleep):
    """Снятое объявление — результат, а не отказ площадки: идём дальше."""
    first = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    second = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/2')
    task_one, task_two = make_task(first), make_task(second)

    report = run_queue(
        ReplayFetcher({first.url: GONE_PAGE, second.url: good_page()}), sleep=no_sleep
    )

    task_one.refresh_from_db(); task_two.refresh_from_db()
    assert not report.stopped
    assert task_one.state == TaskState.DONE
    assert task_two.state == TaskState.DONE
    assert PriceSnapshot.objects.filter(status=SnapshotStatus.NOT_AVAILABLE).count() == 1


def test_page_without_dates_block_writes_failure_not_listing_price(apartment, no_sleep):
    """Витринная цена есть, цены на даты нет — в ячейку не должно попасть ничего."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor, check_in=TODAY + timedelta(days=1), nights=2)

    run_queue(ReplayFetcher({competitor.url: NO_BLOCK_PAGE}), sleep=no_sleep)

    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert not PriceSnapshot.objects.filter(price_kind=PriceKind.DATED,
                                            status=SnapshotStatus.OK).exists()
    failed = PriceSnapshot.objects.get(price_kind=PriceKind.DATED)
    assert failed.status == SnapshotStatus.FAILED
    assert failed.price_per_night is None
    assert failed.check_in == TODAY + timedelta(days=1)
    assert PriceSnapshot.objects.get(price_kind=PriceKind.LISTING).price_per_night == Decimal('3800')


def test_interrupted_run_keeps_what_was_collected(apartment, no_sleep):
    """Обрыв на третьем задании: первые два в базе, третье не потеряно."""
    first = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    second = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/2')
    third = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/3')
    tasks = [make_task(c) for c in (first, second, third)]

    fetcher = ReplayFetcher({
        first.url: good_page(),
        second.url: good_page(3800),
        third.url: RuntimeError('браузер отвалился'),
    })
    with pytest.raises(RuntimeError):
        run_queue(fetcher, sleep=no_sleep)

    for task in tasks:
        task.refresh_from_db()
    assert tasks[0].state == TaskState.DONE
    assert tasks[1].state == TaskState.DONE
    # Задание вернулось в очередь целым: сбой инструмента не результат сбора.
    assert tasks[2].state == TaskState.PENDING
    assert 'браузер отвалился' in tasks[2].note
    assert PriceSnapshot.objects.filter(price_kind=PriceKind.DATED,
                                        status=SnapshotStatus.OK).count() == 2


def test_deferred_task_waits_for_its_time(apartment, no_sleep):
    """Отложенное задание не берётся, пока срок не вышел."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor)
    CollectTask.objects.filter(pk=task.pk).update(
        state=TaskState.DEFERRED, deferred_until=timezone.now() + timedelta(hours=8)
    )

    fetcher = ReplayFetcher({competitor.url: good_page()})
    report = run_queue(fetcher, sleep=no_sleep)

    assert report.outcomes == []
    assert fetcher.calls == []


def test_deferred_task_runs_when_its_time_has_come(apartment, no_sleep):
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor)
    CollectTask.objects.filter(pk=task.pk).update(
        state=TaskState.DEFERRED, deferred_until=timezone.now() - timedelta(minutes=1)
    )

    run_queue(ReplayFetcher({competitor.url: good_page()}), sleep=no_sleep)

    task.refresh_from_db()
    assert task.state == TaskState.DONE
    assert task.attempts == 1


def test_competitor_title_is_filled_once(apartment, no_sleep):
    empty = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    named = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/2',
                            title='Название от менеджера')
    make_task(empty)
    make_task(named)

    run_queue(
        ReplayFetcher({
            empty.url: good_page(title='Квартира-студия, 25 м²'),
            named.url: good_page(title='Квартира-студия, 30 м²'),
        }),
        sleep=no_sleep,
    )

    empty.refresh_from_db(); named.refresh_from_db()
    assert empty.title == 'Квартира-студия, 25 м²'
    assert named.title == 'Название от менеджера'


def test_all_dates_from_the_page_are_written(apartment, no_sleep):
    """Площадка отдала три периода — записываем три, а не только запрошенный."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor, check_in=TODAY + timedelta(days=1), nights=1)

    first = TODAY + timedelta(days=10)
    body = dates_block([
        ('5 670', first, first + timedelta(days=1)),
        ('5 130', first + timedelta(days=1), first + timedelta(days=2)),
        ('6 210', first + timedelta(days=2), first + timedelta(days=3)),
    ])
    html = page_html(buyer_item=item_state(), body=body)
    run_queue(ReplayFetcher({competitor.url: html}), sleep=no_sleep)

    assert PriceSnapshot.objects.filter(price_kind=PriceKind.DATED,
                                        status=SnapshotStatus.OK).count() == 3


def test_limit_stops_after_n_tasks(apartment, no_sleep):
    competitors = [
        make_competitor(apartment, f'https://www.avito.ru/moskva/kvartiry/{i}')
        for i in range(3)
    ]
    for competitor in competitors:
        make_task(competitor)

    fetcher = ReplayFetcher({c.url: good_page() for c in competitors})
    report = run_queue(fetcher, limit=2, sleep=no_sleep)

    assert len(report.outcomes) == 2
    assert CollectTask.objects.filter(state=TaskState.PENDING).count() == 1


def test_task_without_url_fails_without_fetching(apartment, no_sleep):
    task = make_task(apartment)  # у квартиры avito_url пуст
    fetcher = ReplayFetcher({})

    run_queue(fetcher, sleep=no_sleep)

    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert fetcher.calls == []


def test_replay_fetcher_records_the_requested_interval(apartment, no_sleep):
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor, check_in=date(2026, 9, 20), nights=3)

    fetcher = ReplayFetcher({competitor.url: good_page()})
    run_queue(fetcher, sleep=no_sleep)

    url, interval = fetcher.calls[0]
    assert url == competitor.url
    assert interval.check_in == date(2026, 9, 20)
    assert interval.nights == 3


def test_fetch_result_dump_path_reaches_the_snapshot(apartment, no_sleep):
    """Путь к дампу пишется в снимок: без него сбой нечем разбирать."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor)

    fetcher = ReplayFetcher({
        competitor.url: FetchResult(html=good_page(), http_status=200,
                                    dump_path='out/dumps/test.html')
    })
    run_queue(fetcher, sleep=no_sleep)

    assert PriceSnapshot.objects.filter(raw_dump_path='out/dumps/test.html').count() == 2


# ------------------------------------------------------------- адрес с датами

# Списано из адресной строки браузера 29.08 при выборе 20–23 сентября на двоих.
# Проверка побайтовая: параметр чужой, и «примерно такой» здесь ничего не значит.
GUESTS_FROM_ADDRESS_BAR = (
    'guestsDetailed=%7B%22version%22%3A1%2C%22totalCount%22%3A2'
    '%2C%22adultsCount%22%3A2%2C%22children%22%3A%5B%5D%7D'
)


def test_url_gets_dates_and_guests(settings):
    from monitor.collector.avito_url import with_dates
    from monitor.services import Interval

    settings.COLLECT_GUESTS = 2
    url = with_dates(
        'https://www.avito.ru/moskva/kvartiry/kvartira_7628611904',
        Interval(check_in=date(2026, 9, 20), nights=3),
    )
    assert 'checkIn=2026-09-20' in url
    assert 'checkOut=2026-09-23' in url
    assert GUESTS_FROM_ADDRESS_BAR in url


def test_url_drops_foreign_parameters():
    """Чужие параметры отбрасываются: сборщик не дописывает своё к чужому.

    Отдельно проверено в tests/test_url_normalization.py — там же случай
    «ссылка из адресной строки против чистой».
    """
    from monitor.collector.avito_url import with_dates
    from monitor.services import Interval

    url = with_dates(
        'https://www.avito.ru/moskva/kvartiry/kvartira_1?context=H4sIAAAA',
        Interval(check_in=date(2026, 9, 20), nights=3),
    )
    assert 'context' not in url
    assert url.startswith('https://www.avito.ru/moskva/kvartiry/kvartira_1?checkIn=')


def test_url_drops_dates_pasted_by_the_manager():
    """В ссылке из адресной строки могли остаться чужие даты — они не наши."""
    from monitor.collector.avito_url import with_dates
    from monitor.services import Interval

    url = with_dates(
        'https://www.avito.ru/moskva/kvartiry/kvartira_1?checkIn=2026-01-01&checkOut=2026-01-05',
        Interval(check_in=date(2026, 9, 20), nights=3),
    )
    assert '2026-01-01' not in url
    assert 'checkIn=2026-09-20' in url
    assert url.count('checkIn') == 1


def test_url_without_interval_still_sets_guests(settings):
    """Даже когда период не задан, число гостей фиксируется: иначе площадка
    возьмёт своё, и снимки окажутся собраны при разной вместимости."""
    from monitor.collector.avito_url import with_dates

    settings.COLLECT_GUESTS = 2
    url = with_dates('https://www.avito.ru/moskva/kvartiry/kvartira_1')
    assert 'checkIn' not in url
    assert GUESTS_FROM_ADDRESS_BAR in url


def test_url_without_scheme_becomes_https():
    from monitor.collector.avito_url import with_dates

    assert with_dates('//avito.ru/moskva/kvartiry/1').startswith('https://')


def test_guests_payload_is_built_from_the_number(settings):
    """Значение собирается из числа гостей, а не хранится готовой строкой."""
    from monitor.collector.avito_url import guests_payload

    assert json.loads(guests_payload(4)) == {
        'version': 1, 'totalCount': 4, 'adultsCount': 4, 'children': [],
    }


def test_guests_payload_matches_the_address_bar_byte_for_byte(settings):
    """Побайтовая сверка с тем, что площадка кладёт в адрес сама."""
    from monitor.collector.avito_url import with_dates

    settings.COLLECT_GUESTS = 2
    url = with_dates('https://www.avito.ru/moskva/kvartiry/kvartira_1')
    assert GUESTS_FROM_ADDRESS_BAR in url


def test_guests_count_reaches_the_url(settings):
    """Четверо в настройке — четверо в адресе, а не двое из старой строки."""
    from monitor.collector.avito_url import with_dates

    settings.COLLECT_GUESTS = 4
    url = with_dates('https://www.avito.ru/moskva/kvartiry/kvartira_1')
    assert '%22totalCount%22%3A4' in url
    assert '%22adultsCount%22%3A4' in url


# --------------------------------------------- цена на запрошенный период

MANUAL_DUMP = (
    __import__('pathlib').Path(__file__).parent
    / 'fixtures' / 'dumps' / '7628611904_2026-09-20_2026-09-23_g2.html'
)


def test_period_price_from_the_block_reaches_the_snapshot(apartment, no_sleep):
    """Сквозь весь сбор: в снимок едет 11 421 из блока, а не 3 350 из состояния."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor, check_in=date(2026, 9, 20), nights=3)

    html = MANUAL_DUMP.read_text(encoding='utf-8', errors='replace')
    run_queue(ReplayFetcher({competitor.url: html}), sleep=no_sleep)

    dated = PriceSnapshot.objects.get(
        price_kind=PriceKind.DATED, check_in=date(2026, 9, 20), nights=3,
        status=SnapshotStatus.OK,
    )
    assert dated.total_price == Decimal('11421.00')
    assert dated.price_per_night == Decimal('3807.00')

    listing = PriceSnapshot.objects.get(price_kind=PriceKind.LISTING)
    assert listing.price_per_night == Decimal('3350')


def test_neighbouring_periods_are_written_too(apartment, no_sleep):
    """Карусель после выбора дат отдаёт соседние периоды той же длительности."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor, check_in=date(2026, 9, 20), nights=3)

    html = MANUAL_DUMP.read_text(encoding='utf-8', errors='replace')
    run_queue(ReplayFetcher({competitor.url: html}), sleep=no_sleep)

    dated = PriceSnapshot.objects.filter(
        price_kind=PriceKind.DATED, status=SnapshotStatus.OK
    )
    assert dated.count() == 7
    assert set(dated.values_list('nights', flat=True)) == {3}


def test_price_conflict_writes_a_failure_not_a_number(apartment, no_sleep):
    """Блок не сошёлся сам с собой — в базу идёт сбой, а не одно из двух чисел."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor, check_in=date(2026, 9, 20), nights=3)

    broken = MANUAL_DUMP.read_text(encoding='utf-8', errors='replace').replace(
        'data-marker="item-view/item-price">3&nbsp;807',
        'data-marker="item-view/item-price">9&nbsp;999',
    )
    run_queue(ReplayFetcher({competitor.url: broken}), sleep=no_sleep)

    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert not PriceSnapshot.objects.filter(
        price_kind=PriceKind.DATED, status=SnapshotStatus.OK
    ).exists()
    failed = PriceSnapshot.objects.get(price_kind=PriceKind.DATED)
    assert failed.price_per_night is None
    assert 'не сходится' in failed.error_note


# ------------------------------------------------- цикл событий и база

class FakeBrowser:
    """Подделка под синхронный Playwright: заводит цикл событий, как настоящий.

    Настоящий `sync_playwright()` создаёт в вызывающем потоке цикл событий
    и крутит его гринлетами. Django на живой цикл в текущем потоке отвечает
    `SynchronousOnlyOperation` при любом обращении к базе — сбор скачивал
    страницу и падал на записи снимка. Подделка воспроизводит именно это:
    цикл, живой в том потоке, где работает браузер.
    """

    def __init__(self):
        self.loop = None
        self.thread = None

    def start(self):
        self.loop = asyncio.new_event_loop()
        # Настоящий Playwright крутит цикл гринлетами и возвращает управление
        # пользовательскому коду, не выходя из него: после start() вызов
        # asyncio.get_running_loop() успешно возвращает цикл — проверено
        # на живом playwright 1.62. Django смотрит ровно на это.
        # Приватная функция здесь не хитрость, а единственный способ добиться
        # того же наблюдаемого состояния без гринлетов.
        asyncio.events._set_running_loop(self.loop)
        self.thread = threading.current_thread()
        return self

    def stop(self):
        asyncio.events._set_running_loop(None)
        self.loop.close()


class FakeFetcher(BrowserFetcher):
    """`BrowserFetcher` без настоящего браузера: страницы заранее заготовлены."""

    def __init__(self, pages, **kwargs):
        super().__init__(**kwargs)
        self.pages = pages
        self.browser = FakeBrowser()
        self.fetch_threads = []

    def _open_browser(self):
        self._playwright = self.browser.start()
        self._page = object()

    def _close_browser(self):
        self._playwright = self._page = None

    def _do_fetch(self, url, interval):
        self.fetch_threads.append(threading.current_thread())
        # Внутри потока браузера цикл виден работающим — как у настоящего.
        assert asyncio.get_running_loop() is self.browser.loop
        return FetchResult(html=self.pages[url], http_status=200,
                           dump_path='out/dumps/fake.html')


def test_snapshots_are_written_although_the_browser_holds_an_event_loop(
    apartment, no_sleep
):
    """Регрессия: сбор скачивал страницу и падал на записи снимка.

    Синхронный Playwright заводит цикл событий в своём потоке. Пока браузер
    жил в потоке сбора, Django отвечал SynchronousOnlyOperation на любое
    обращение к базе.
    """
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor)

    fetcher = FakeFetcher({competitor.url: good_page()})
    try:
        report = run_queue(fetcher, sleep=no_sleep)
    finally:
        fetcher.close()

    assert not report.stopped
    assert PriceSnapshot.objects.filter(
        price_kind=PriceKind.DATED, status=SnapshotStatus.OK
    ).exists()


def test_browser_work_happens_off_the_calling_thread(apartment, no_sleep):
    """Цикл событий остаётся при браузере, поток сбора остаётся чистым."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    make_task(competitor)

    fetcher = FakeFetcher({competitor.url: good_page()})
    try:
        run_queue(fetcher, sleep=no_sleep)
    finally:
        fetcher.close()

    assert fetcher.fetch_threads
    assert all(thread is not threading.current_thread()
               for thread in fetcher.fetch_threads)
    assert fetcher.browser.thread is not threading.current_thread()
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


def test_fetch_errors_cross_the_thread_boundary_unchanged(apartment, no_sleep):
    """Тип исключения сохраняется: сборщик по нему отличает сбой обращения."""
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor)

    class Failing(FakeFetcher):
        def _do_fetch(self, url, interval):
            raise RuntimeError('браузер отвалился')

    fetcher = Failing({})
    try:
        with pytest.raises(RuntimeError, match='браузер отвалился'):
            run_queue(fetcher, sleep=no_sleep)
    finally:
        fetcher.close()

    task.refresh_from_db()
    assert task.state == TaskState.PENDING


def test_browser_start_failure_reaches_the_caller():
    """Не поднялся браузер — узнаём об этом сразу, а не по молчанию потока."""

    class Broken(FakeFetcher):
        def _open_browser(self):
            raise RuntimeError('нет chromium')

    fetcher = Broken({})
    with pytest.raises(RuntimeError, match='нет chromium'):
        fetcher.fetch('https://www.avito.ru/moskva/kvartiry/1', None)
    fetcher.close()


def test_close_is_safe_without_a_started_browser():
    BrowserFetcher(headed=False).close()


# ------------------------------------------- страница, которая не догружается

CHECK_PAGE = (
    '<html><head><title>Доступ ограничен</title></head>'
    '<body>Доступ ограничен: проблема с IP</body></html>'
)


class FakeResponse:
    def __init__(self, status):
        self.status = status


class StuckPage:
    """Экран проверки: приходит мгновенно и не догружается никогда.

    Все ожидания отваливаются по таймауту, как у настоящего Playwright.
    Скриншот на такой странице виснет дольше всего остального, поэтому
    попытка его снять здесь — сама по себе провал теста.
    """

    def __init__(self, status=429, html=CHECK_PAGE):
        self.status = status
        self.html = html
        self.waits = 0
        self.screenshots = 0

    def goto(self, url, **kwargs):
        return FakeResponse(self.status)

    def content(self):
        return self.html

    def _timeout(self, *args, **kwargs):
        self.waits += 1
        raise TimeoutError('страница не догружается')

    wait_for_load_state = _timeout
    wait_for_selector = _timeout
    get_by_text = _timeout

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, **kwargs):
        self.screenshots += 1
        raise AssertionError('на экране проверки скриншот снимать нельзя — он виснет')

    def locator(self, selector):
        raise AssertionError('на экране проверки разметку щупать нечего')


class PageFetcher(BrowserFetcher):
    """`BrowserFetcher` с поддельной страницей: весь ход захода настоящий."""

    def __init__(self, page, **kwargs):
        super().__init__(**kwargs)
        self.page = page
        self.browser = FakeBrowser()

    def _open_browser(self):
        self._playwright = self.browser.start()
        self._page = self.page

    def _close_browser(self):
        self._playwright = self._page = None


@pytest.mark.parametrize('status', [403, 429])
def test_refusal_returns_at_once_without_screenshots(status, tmp_path, monkeypatch):
    """403 и 429 разбираются сразу: ни ожиданий, ни скриншотов."""
    monkeypatch.setattr('monitor.collector.browser.DUMPS', tmp_path)
    page = StuckPage(status=status)
    fetcher = PageFetcher(page)
    try:
        result = fetcher.fetch('https://www.avito.ru/moskva/kvartiry/1', None)
    finally:
        fetcher.close()

    assert result.http_status == status
    assert page.waits == 0, 'на отказе ждать нечего'
    assert page.screenshots == 0
    assert result.screenshots == []
    assert 'Доступ ограничен' in result.html


def test_check_page_with_status_200_is_also_caught(tmp_path, monkeypatch):
    """Проверка приходит и с кодом 200 — узнаём её по разметке, а не по коду."""
    monkeypatch.setattr('monitor.collector.browser.DUMPS', tmp_path)
    page = StuckPage(status=200)
    fetcher = PageFetcher(page)
    try:
        result = fetcher.fetch('https://www.avito.ru/moskva/kvartiry/1', None)
    finally:
        fetcher.close()

    assert page.waits == 0
    assert page.screenshots == 0
    assert result.http_status == 200


def test_dump_is_written_even_on_refusal(tmp_path, monkeypatch):
    """Дамп отказа — материал разбора, а не мусор."""
    monkeypatch.setattr('monitor.collector.browser.DUMPS', tmp_path)
    fetcher = PageFetcher(StuckPage())
    try:
        result = fetcher.fetch('https://www.avito.ru/moskva/kvartiry/1', None)
    finally:
        fetcher.close()

    assert pathlib.Path(result.dump_path).exists()
    assert 'Доступ ограничен' in pathlib.Path(result.dump_path).read_text()


def test_never_loading_page_gives_blocked_not_a_hang(apartment, no_sleep,
                                                     tmp_path, monkeypatch):
    """Сквозь весь сбор: экран проверки даёт снимок blocked и останавливает обход."""
    monkeypatch.setattr('monitor.collector.browser.DUMPS', tmp_path)
    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor)

    fetcher = PageFetcher(StuckPage())
    try:
        report = run_queue(fetcher, sleep=no_sleep)
    finally:
        fetcher.close()

    task.refresh_from_db()
    assert report.stopped
    assert task.state == TaskState.DEFERRED
    assert PriceSnapshot.objects.filter(status=SnapshotStatus.BLOCKED).count() == 1


class HangingPage(StuckPage):
    """Страница, на которой виснет сам Playwright: ответа нет вовсе."""

    def __init__(self):
        super().__init__()
        self.released = threading.Event()

    def goto(self, url, **kwargs):
        # Ждём дольше любого разумного предела, но не вечно: иначе поток
        # останется висеть и после теста.
        self.released.wait(timeout=30)
        return FakeResponse(200)


def test_stuck_browser_gives_up_instead_of_hanging(apartment, no_sleep):
    """Последняя защита: Playwright завис — сбор возвращает управление и падает.

    Именно этого не хватало в живом прогоне: браузер открылся, страница
    не грузилась, процесс не завершался сам и его пришлось убивать руками.
    """
    from monitor.collector.browser import BrowserStuck

    competitor = make_competitor(apartment, 'https://www.avito.ru/moskva/kvartiry/1')
    task = make_task(competitor)

    page = HangingPage()
    fetcher = PageFetcher(page, deadline_seconds=1)
    try:
        with pytest.raises(BrowserStuck, match='не ответил'):
            run_queue(fetcher, sleep=no_sleep)
    finally:
        page.released.set()
        fetcher.close()

    task.refresh_from_db()
    # Задание вернулось в очередь целым: зависание — сбой инструмента,
    # а не результат сбора.
    assert task.state == TaskState.PENDING
