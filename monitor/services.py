"""Сборка картины цен для страницы сравнения.

Главное правило этого модуля: в сравнение по датам попадают только снимки
price_kind='dated'. Витринная цена сюда не проникает ни при каких условиях —
подстановка её вместо реальной цены выглядит рабочей системой и каждый день
занижает картину на треть.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from monitor.models import PriceKind, PriceSnapshot, SnapshotStatus

NO_DATA = 'данных нет'


def nights_word(count):
    """«1 ночь», «2 ночи», «5 ночей» — иначе пометка про среднее читается коряво."""
    if count is None:
        return ''
    tail, hundred_tail = count % 10, count % 100
    if tail == 1 and hundred_tail != 11:
        word = 'ночь'
    elif tail in (2, 3, 4) and hundred_tail not in (12, 13, 14):
        word = 'ночи'
    else:
        word = 'ночей'
    return f'{count} {word}'



@dataclass
class Cell:
    """Одна ячейка таблицы: цена конкурента (или наша) на одну дату заезда."""

    check_in: date
    price_per_night: Decimal | None = None
    nights: int | None = None
    collected_at: datetime | None = None
    is_stale: bool = False
    # Почему цены нет. Заполняется из последнего снимка любого исхода на эту дату.
    miss_reason: str = NO_DATA
    is_cheaper: bool = False

    @property
    def has_price(self):
        return self.price_per_night is not None

    @property
    def is_averaged(self):
        """При nights > 1 цена за ночь — среднее по диапазону, а не цена этой ночи."""
        return self.nights is not None and self.nights > 1

    @property
    def price_display(self):
        """Цена в рублях, без копеек: копейки в этой таблице — шум."""
        if self.price_per_night is None:
            return None
        rubles = self.price_per_night.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return f'{rubles:,}'.replace(',', '\u00a0')

    @property
    def nights_label(self):
        return nights_word(self.nights)


@dataclass
class Row:
    """Строка таблицы: наша квартира или конкурент."""

    title: str
    url: str
    min_nights: int | None
    is_active: bool
    is_ours: bool
    pk: int | None = None
    cells: list[Cell] = field(default_factory=list)

    @property
    def min_nights_label(self):
        return nights_word(self.min_nights)

    def price_on(self, check_in):
        for cell in self.cells:
            if cell.check_in == check_in:
                return cell.price_per_night
        return None


@dataclass
class PriceTable:
    dates: list[date]
    ours: Row
    competitors: list[Row]

    @property
    def active_competitors(self):
        return [row for row in self.competitors if row.is_active]

    @property
    def inactive_competitors(self):
        return [row for row in self.competitors if not row.is_active]


def horizon_dates(start=None, days=None):
    """Ближайшие даты заезда. Число дат — настройка PRICE_HORIZON_DAYS."""
    start = start or timezone.localdate()
    days = days if days is not None else settings.PRICE_HORIZON_DAYS
    return [start + timedelta(days=offset) for offset in range(days)]


def latest_prices(apartment, dates):
    """Последняя известная цена по каждому конкуренту на каждую дату.

    «Последняя известная», а не «свежая»: ручного ввода в проекте нет, и если
    сегодняшний сбор не удался, показать нечего, кроме вчерашнего числа с меткой
    времени. Возраст данных возвращается вместе с ценой, чтобы менеджер видела,
    насколько число устарело.
    """
    dates = list(dates)
    competitors = list(apartment.competitors.all())

    snapshots = (
        PriceSnapshot.objects.filter(price_kind=PriceKind.DATED, check_in__in=dates)
        .filter(Q(apartment=apartment) | Q(competitor__in=competitors))
        .order_by('-collected_at')
    )

    # По ключу (владелец, дата) нужен свежайший успешный снимок — он даёт цену,
    # и свежайший любой — он даёт причину, когда цены нет.
    best = {}
    latest_any = {}
    for snapshot in snapshots:
        key = (snapshot.competitor_id, snapshot.apartment_id, snapshot.check_in)
        latest_any.setdefault(key, snapshot)
        if snapshot.status == SnapshotStatus.OK:
            best.setdefault(key, snapshot)

    now = timezone.now()
    stale_after = timedelta(hours=settings.STALE_AFTER_HOURS)

    def build_row(*, competitor_id, apartment_id, title, url, min_nights, is_active, is_ours):
        row = Row(
            title=title,
            url=url,
            min_nights=min_nights,
            is_active=is_active,
            is_ours=is_ours,
            pk=competitor_id or apartment_id,
        )
        for check_in in dates:
            key = (competitor_id, apartment_id, check_in)
            snapshot = best.get(key)
            if snapshot is not None:
                row.cells.append(
                    Cell(
                        check_in=check_in,
                        price_per_night=snapshot.price_per_night,
                        nights=snapshot.nights,
                        collected_at=snapshot.collected_at,
                        is_stale=now - snapshot.collected_at > stale_after,
                    )
                )
                continue
            failed = latest_any.get(key)
            row.cells.append(Cell(check_in=check_in, miss_reason=_miss_reason(failed)))
        return row

    ours = build_row(
        competitor_id=None,
        apartment_id=apartment.pk,
        title=apartment.title,
        url=apartment.avito_url,
        min_nights=apartment.min_nights,
        is_active=apartment.is_active,
        is_ours=True,
    )
    competitor_rows = [
        build_row(
            competitor_id=competitor.pk,
            apartment_id=None,
            title=competitor.title or competitor.url,
            url=competitor.url,
            min_nights=competitor.min_nights,
            is_active=competitor.is_active,
            is_ours=False,
        )
        for competitor in competitors
    ]

    _mark_cheaper(ours, competitor_rows, dates)
    return PriceTable(dates=dates, ours=ours, competitors=competitor_rows)


def _miss_reason(snapshot):
    if snapshot is None:
        return NO_DATA
    reason = snapshot.get_status_display()
    if snapshot.error_note:
        reason = f'{reason}: {snapshot.error_note}'
    collected = timezone.localtime(snapshot.collected_at).strftime('%d.%m %H:%M')
    return f'{reason} ({collected})'


def _mark_cheaper(ours, competitor_rows, dates):
    """Подсветить конкурентов, которые на эту дату дешевле нас."""
    for index, check_in in enumerate(dates):
        our_price = ours.cells[index].price_per_night
        if our_price is None:
            continue
        for row in competitor_rows:
            cell = row.cells[index]
            cell.is_cheaper = cell.has_price and cell.price_per_night < our_price


def sort_rows(rows, check_in):
    """Сортировка конкурентов по цене на выбранную дату. Без цены — вниз."""
    if check_in is None:
        return rows
    return sorted(
        rows,
        key=lambda row: (
            row.price_on(check_in) is None,
            row.price_on(check_in) or Decimal('0'),
            row.title.lower(),
        ),
    )
