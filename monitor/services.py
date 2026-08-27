"""Сборка картины цен на выбранный интервал.

Два правила, на которых держится модуль.

Первое: в сравнение попадают только снимки `price_kind='dated'`. Витринная цена
сюда не проникает ни как цена, ни как запасное значение — подстановка её вместо
реальной выглядит рабочей системой и каждый день занижает картину на треть.

Второе: снимок ищется по паре (дата заезда, число ночей), а не по одной дате.
Цена за три ночи с 20 сентября и цена за две ночи с того же числа — разные числа,
и подставлять одно вместо другого нельзя.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from monitor.models import PriceKind, PriceSnapshot, SnapshotStatus

NO_DATA = 'данных нет'

# Предел на длину периода: опечатка в годе не должна превращаться в запрос на год.
MAX_NIGHTS = 30

FRIDAY = 4


def nights_word(count):
    """«1 ночь», «2 ночи», «5 ночей» — иначе подписи читаются коряво."""
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


def format_rubles(value):
    """Рубли без копеек: копейки в этой таблице — шум."""
    if value is None:
        return None
    rubles = value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return f'{rubles:,}'.replace(',', ' ')


@dataclass(frozen=True)
class Interval:
    """Период проживания: дата заезда и число ночей.

    Менеджер смотрит произвольный интервал в будущем и делит сумму за него
    на число ночей. Это ровно то, что хранит снимок.
    """

    check_in: date
    nights: int

    @property
    def check_out(self):
        return self.check_in + timedelta(days=self.nights)

    @property
    def nights_label(self):
        return nights_word(self.nights)

    def __str__(self):
        return (
            f'{self.check_in.strftime("%d.%m")} — '
            f'{self.check_out.strftime("%d.%m")}, {self.nights_label}'
        )


def default_interval(today=None):
    """Ближайшие выходные: заезд в пятницу, выезд в воскресенье."""
    today = today or timezone.localdate()
    friday = today + timedelta(days=(FRIDAY - today.weekday()) % 7)
    return Interval(check_in=friday, nights=2)


def parse_interval(check_in_raw, check_out_raw, today=None):
    """Разобрать период из адреса страницы.

    Возвращает пару (интервал, текст ошибки). При ошибке интервал — период
    по умолчанию: пустая страница из-за опечатки в дате хуже, чем выходные.
    """
    if not check_in_raw and not check_out_raw:
        return default_interval(today), None
    try:
        check_in = date.fromisoformat(check_in_raw)
        check_out = date.fromisoformat(check_out_raw)
    except (TypeError, ValueError):
        return default_interval(today), 'Даты не разобрать, показан ближайший уикенд.'

    nights = (check_out - check_in).days
    if nights < 1:
        return default_interval(today), 'Выезд должен быть позже заезда.'
    if nights > MAX_NIGHTS:
        return (
            default_interval(today),
            f'Период длиннее {nights_word(MAX_NIGHTS)} не показываем.',
        )
    return Interval(check_in=check_in, nights=nights), None


@dataclass
class Cell:
    """Цена одного объявления на выбранный интервал."""

    interval: Interval
    price_per_night: Decimal | None = None
    total_price: Decimal | None = None
    nights: int | None = None
    collected_at: datetime | None = None
    is_stale: bool = False
    # Почему цены нет. Заполняется из последнего снимка любого исхода на этот интервал.
    miss_reason: str = NO_DATA
    is_cheaper: bool = False

    @property
    def has_price(self):
        return self.price_per_night is not None

    @property
    def price_display(self):
        return format_rubles(self.price_per_night)

    @property
    def total_display(self):
        return format_rubles(self.total_price)

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
    cell: Cell
    pk: int | None = None

    @property
    def min_nights_label(self):
        return nights_word(self.min_nights)


@dataclass
class PriceTable:
    interval: Interval
    ours: Row
    competitors: list[Row]

    @property
    def active_competitors(self):
        return [row for row in self.competitors if row.is_active]

    @property
    def inactive_competitors(self):
        return [row for row in self.competitors if not row.is_active]


def horizon_dates(start=None, days=None):
    """Даты фонового сбора. К странице отношения не имеет: она показывает период.

    Число дат — настройка PRICE_HORIZON_DAYS.
    """
    start = start or timezone.localdate()
    days = days if days is not None else settings.PRICE_HORIZON_DAYS
    return [start + timedelta(days=offset) for offset in range(days)]


def latest_prices(apartment, interval):
    """Последняя известная цена по каждому конкуренту на этот интервал.

    «Последняя известная», а не «свежая»: ручного ввода в проекте нет, и если
    сегодняшний сбор не удался, показать нечего, кроме вчерашнего числа с меткой
    времени. Возраст данных возвращается вместе с ценой.
    """
    competitors = list(apartment.competitors.all())

    snapshots = (
        PriceSnapshot.objects.filter(
            price_kind=PriceKind.DATED, check_in=interval.check_in
        )
        .filter(Q(apartment=apartment) | Q(competitor__in=competitors))
        .order_by('-collected_at')
    )

    best = {}          # свежайший успешный снимок ровно на этот интервал
    latest_any = {}    # свежайший любой — из него берётся причина, когда цены нет
    other_nights = {}  # какие ещё периоды с этой датой заезда собраны
    for snapshot in snapshots:
        key = (snapshot.competitor_id, snapshot.apartment_id)
        if snapshot.nights == interval.nights:
            latest_any.setdefault(key, snapshot)
            if snapshot.status == SnapshotStatus.OK:
                best.setdefault(key, snapshot)
        elif snapshot.status == SnapshotStatus.OK and snapshot.nights:
            other_nights.setdefault(key, set()).add(snapshot.nights)

    now = timezone.now()
    stale_after = timedelta(hours=settings.STALE_AFTER_HOURS)

    def build_row(*, competitor_id, apartment_id, title, url, min_nights, is_active, is_ours):
        key = (competitor_id, apartment_id)
        snapshot = best.get(key)
        if snapshot is not None:
            cell = Cell(
                interval=interval,
                price_per_night=snapshot.price_per_night,
                total_price=snapshot.total_price,
                nights=snapshot.nights,
                collected_at=snapshot.collected_at,
                is_stale=now - snapshot.collected_at > stale_after,
            )
        else:
            cell = Cell(
                interval=interval,
                miss_reason=_miss_reason(latest_any.get(key), other_nights.get(key)),
            )
        return Row(
            title=title,
            url=url,
            min_nights=min_nights,
            is_active=is_active,
            is_ours=is_ours,
            pk=competitor_id or apartment_id,
            cell=cell,
        )

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

    _mark_cheaper(ours, competitor_rows)
    return PriceTable(interval=interval, ours=ours, competitors=competitor_rows)


def _miss_reason(snapshot, nights_available):
    """Почему в ячейке прочерк.

    Если на эту дату собран другой период, об этом сказано — но без числа:
    цена за две ночи не заменяет цену за три, и показывать её здесь нельзя.
    """
    if snapshot is not None:
        reason = snapshot.get_status_display()
        if snapshot.error_note:
            reason = f'{reason}: {snapshot.error_note}'
        collected = timezone.localtime(snapshot.collected_at).strftime('%d.%m %H:%M')
        return f'{reason} ({collected})'
    if nights_available:
        periods = ', '.join(nights_word(count) for count in sorted(nights_available))
        return f'На этот период цены нет. С той же датой заезда собрано: {periods}.'
    return NO_DATA


def _mark_cheaper(ours, competitor_rows):
    """Подсветить конкурентов, которые на этот период дешевле нас."""
    our_price = ours.cell.price_per_night
    if our_price is None:
        return
    for row in competitor_rows:
        row.cell.is_cheaper = row.cell.has_price and row.cell.price_per_night < our_price


def sort_rows(rows, by='title'):
    """Сортировка конкурентов. Без цены — вниз."""
    if by != 'price':
        return sorted(rows, key=lambda row: row.title.lower())
    return sorted(
        rows,
        key=lambda row: (
            not row.cell.has_price,
            row.cell.price_per_night or Decimal('0'),
            row.title.lower(),
        ),
    )
