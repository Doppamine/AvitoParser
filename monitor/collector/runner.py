"""Очередь сбора: задание → страница → разбор → снимок.

Три правила, на которых держится модуль.

**Каждое выполненное задание пишется сразу, своей транзакцией.** Прерывание
на середине очереди не должно терять уже собранное: сбор идёт с паузами
по пятнадцать-тридцать секунд, и сотня конкурентов — это полчаса работы.

**Проверка площадки останавливает обход целиком.** Не пропускаем задание
и идём дальше, а встаём: проверка означает, что площадка нас видит, и следующие
обращения только усугубят. Задание откладывается, остальные ждут в базе.

**Витринная цена не подменяет цену на даты.** Она пишется отдельным снимком
вида `listing` и в сравнение не попадает — это проверяет ещё и база.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from monitor.collector.fetcher import FetchResult, Fetcher
from monitor.models import (
    CollectTask,
    PriceKind,
    PriceSnapshot,
    SnapshotStatus,
    Source,
    TaskState,
)
from monitor.parsing import ParsedPage, ParseStatus, parse_item_page
from monitor.services import Interval

logger = logging.getLogger(__name__)

# Разбор страницы → исход снимка. Всё, что не получено и не отказ площадки,
# это сбой: снимок без цены со статусом «получено» база не примет.
STATUS_MAP = {
    ParseStatus.OK: SnapshotStatus.OK,
    ParseStatus.NO_DATED_PRICES: SnapshotStatus.FAILED,
    ParseStatus.DATES_BLOCK_UNREADABLE: SnapshotStatus.FAILED,
    ParseStatus.PRICE_UNRELIABLE: SnapshotStatus.FAILED,
    ParseStatus.STATE_CONFLICT: SnapshotStatus.FAILED,
    ParseStatus.PRICE_CONFLICT: SnapshotStatus.FAILED,
    ParseStatus.FAILED: SnapshotStatus.FAILED,
    ParseStatus.CAPTCHA_OVERLAY: SnapshotStatus.CAPTCHA_OVERLAY,
    ParseStatus.BLOCKED: SnapshotStatus.BLOCKED,
    ParseStatus.NOT_AVAILABLE: SnapshotStatus.NOT_AVAILABLE,
}

# Исходы, после которых обход останавливается и нужен человек.
NEEDS_HUMAN = (ParseStatus.BLOCKED, ParseStatus.CAPTCHA_OVERLAY)

# Задание считается выполненным, даже если цены не оказалось: объявление снято —
# это результат, а не сбой сбора.
SUCCEEDED = (ParseStatus.OK, ParseStatus.NOT_AVAILABLE)


@dataclass
class TaskOutcome:
    """Что вышло из одного задания — для печати по ходу обхода."""

    task: CollectTask
    page: ParsedPage | None
    snapshots: list[PriceSnapshot] = field(default_factory=list)
    fetch: FetchResult | None = None
    error: str = ''

    @property
    def dated_snapshots(self):
        return [s for s in self.snapshots if s.price_kind == PriceKind.DATED]


@dataclass
class RunReport:
    outcomes: list[TaskOutcome] = field(default_factory=list)
    stopped_reason: str = ''

    @property
    def snapshots(self):
        return [snapshot for outcome in self.outcomes for snapshot in outcome.snapshots]

    @property
    def stopped(self) -> bool:
        return bool(self.stopped_reason)


def next_task(now=None) -> CollectTask | None:
    """Первое задание в очереди: ждущее или отложенное, чей срок вышел."""
    now = now or timezone.now()
    return (
        CollectTask.objects.filter(
            Q(state=TaskState.PENDING)
            | Q(state=TaskState.DEFERRED, deferred_until__lte=now)
        )
        .order_by('created_at', 'pk')
        .first()
    )


def run_queue(
    fetcher: Fetcher,
    *,
    limit: int | None = None,
    pause: tuple[float, float] | None = None,
    sleep=time.sleep,
    on_progress=None,
) -> RunReport:
    """Крутить очередь, пока есть задания.

    `sleep` подменяется в тестах: настоящие паузы там ни к чему, а без них
    проверить последовательность обхода нельзя.
    """
    pause = pause or settings.COLLECT_PAUSE_SECONDS
    report = RunReport()
    processed = 0

    while limit is None or processed < limit:
        task = next_task()
        if task is None:
            break

        outcome = run_task(task, fetcher)
        report.outcomes.append(outcome)
        processed += 1
        if on_progress is not None:
            on_progress(outcome)

        if outcome.page is not None and outcome.page.status in NEEDS_HUMAN:
            report.stopped_reason = (
                f'Площадка показала проверку ({outcome.page.status}). Обход остановлен, '
                f'задание отложено до {timezone.localtime(task.deferred_until):%d.%m %H:%M}. '
                'Нужно пройти проверку руками в том же профиле браузера.'
            )
            logger.warning(report.stopped_reason)
            break

        if limit is None or processed < limit:
            sleep(random.uniform(*pause))

    return report


def run_task(task: CollectTask, fetcher: Fetcher) -> TaskOutcome:
    """Выполнить одно задание и сразу записать результат."""
    now = timezone.now()
    CollectTask.objects.filter(pk=task.pk).update(
        state=TaskState.RUNNING, started_at=now, attempts=task.attempts + 1
    )
    task.refresh_from_db()

    url = task.url
    if not url:
        return _finish(
            task,
            state=TaskState.FAILED,
            note='У объявления нет ссылки — идти некуда.',
            outcome=TaskOutcome(task=task, page=None,
                                error='У объявления нет ссылки.'),
        )

    interval = (
        Interval(check_in=task.check_in, nights=task.nights)
        if task.check_in and task.nights
        else None
    )

    try:
        fetched = fetcher.fetch(url, interval)
    except Exception as error:
        # Сбой инструмента, а не страницы: задание возвращается в очередь
        # нетронутым, уже собранное остаётся в базе, а разбираться идёт человек.
        CollectTask.objects.filter(pk=task.pk).update(
            state=TaskState.PENDING, started_at=None, note=f'Сбой обращения: {error}'
        )
        raise

    # Период передаётся в парсер: по нему читается блок цены на запрошенные даты.
    # Без него разобрать его нечем — число ночей взять неоткуда.
    page = parse_item_page(fetched.html, today=timezone.localdate(), interval=interval)
    snapshots = _write_snapshots(task, page, fetched)
    _fill_competitor_title(task, page)

    outcome = TaskOutcome(task=task, page=page, snapshots=snapshots, fetch=fetched)

    if page.status in NEEDS_HUMAN:
        return _finish(
            task,
            state=TaskState.DEFERRED,
            note=page.error_note,
            outcome=outcome,
            deferred_until=timezone.now() + timedelta(hours=settings.COLLECT_DEFER_HOURS),
        )

    return _finish(
        task,
        state=TaskState.DONE if page.status in SUCCEEDED else TaskState.FAILED,
        note=page.error_note,
        outcome=outcome,
    )


def _finish(task, *, state, note, outcome, deferred_until=None) -> TaskOutcome:
    dated = outcome.dated_snapshots
    CollectTask.objects.filter(pk=task.pk).update(
        state=state,
        note=note,
        finished_at=timezone.now(),
        deferred_until=deferred_until,
        snapshot=dated[0] if dated else None,
    )
    task.refresh_from_db()
    outcome.task = task
    return outcome


@transaction.atomic
def _write_snapshots(task, page: ParsedPage, fetched: FetchResult) -> list[PriceSnapshot]:
    """Снимки по одному заданию. Своя транзакция: прерывание не теряет собранное."""
    owner = {'competitor': task.competitor} if task.competitor_id else {'apartment': task.apartment}
    now = timezone.now()
    common = dict(
        **owner,
        collected_at=now,
        source=Source.AVITO,
        raw_dump_path=fetched.dump_path,
        # Число гостей — часть ключа сопоставимости, как и число ночей.
        guests=task.guests,
    )
    snapshots = []

    # Витринная цена пишется отдельным снимком всегда, когда она извлеклась —
    # включая неудачные заходы. Это признак «объявление живо», и стоит он ничего.
    if page.listing_price is not None:
        snapshots.append(
            PriceSnapshot.objects.create(
                **common,
                price_kind=PriceKind.LISTING,
                price_per_night=page.listing_price,
                status=SnapshotStatus.OK,
                error_note=f'источник: {page.listing_price_source}',
            )
        )

    if page.status == ParseStatus.OK:
        # Пишем все разобранные периоды, а не только запрошенный: снимки
        # неизменяемы и дёшевы, а выброшенное придётся собирать повторно.
        for period in page.periods:
            snapshots.append(
                PriceSnapshot.objects.create(
                    **common,
                    price_kind=PriceKind.DATED,
                    check_in=period.check_in,
                    nights=period.nights,
                    price_per_night=period.price_per_night,
                    total_price=period.total_price,
                    status=SnapshotStatus.OK,
                    error_note=page.error_note,
                )
            )
        return snapshots

    snapshots.append(
        PriceSnapshot.objects.create(
            **common,
            price_kind=PriceKind.DATED,
            check_in=task.check_in,
            nights=task.nights,
            status=STATUS_MAP[page.status],
            error_note=page.error_note,
        )
    )
    return snapshots


def _fill_competitor_title(task, page: ParsedPage) -> None:
    """Заголовок конкурента заполняется один раз и не перетирается.

    Единственная запись сборщика мимо снимков: без заголовка в таблице стоит
    голая ссылка, а менеджер узнаёт объявления по названию.
    """
    if not task.competitor_id or not page.title:
        return
    if task.competitor.title:
        return
    task.competitor.title = page.title[:300]
    task.competitor.save(update_fields=['title'])
