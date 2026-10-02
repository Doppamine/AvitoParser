"""Сбор цен по одной квартире — и материал для показа заказчику.

    python manage.py collect --apartment 1 --from 2026-09-20 --to 2026-09-23

По ходу печатает, что происходит и каким источником получено каждое число.
В конце — таблица, которую можно показать человеку, и папка `out/demo/<штамп>/`
со скриншотами страниц: демонстрация строится на том, что число в таблице
сверяется глазами с числом на скриншоте.
"""

import csv
import shutil
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from monitor.collector.browser import BrowserFetcher
from monitor.collector.fetcher import ReplayFetcher
from monitor.collector.runner import run_queue
from monitor.models import (
    Apartment,
    CollectTask,
    PriceKind,
    SnapshotStatus,
    TaskState,
)
from monitor.parsing import ParseStatus
from monitor.services import Interval, format_rubles, nights_word, parse_interval

DEMO_ROOT = Path(settings.BASE_DIR) / 'out' / 'demo'

# Транслитерация для имён файлов в папке показа: заказчик открывает её
# в проводнике, и «01_kvartira-studiya.png» читается, а «01_%D0%9A…» нет.
TRANSLIT = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
    'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya', ' ': '-', ',': '',
    '.': '', '²': '', '«': '', '»': '', '/': '-',
})


def guests_word(count: int) -> str:
    tail, hundred_tail = count % 10, count % 100
    if tail == 1 and hundred_tail != 11:
        word = 'гостя'
    elif tail in (2, 3, 4) and hundred_tail not in (12, 13, 14):
        word = 'гостей'
    else:
        word = 'гостей'
    return f'{count} {word}'


def slugify(text: str, limit: int = 40) -> str:
    slug = text.lower().translate(TRANSLIT)
    slug = ''.join(char for char in slug if char.isalnum() or char == '-')
    return slug.strip('-')[:limit] or 'obyavlenie'


class Command(BaseCommand):
    help = 'Собрать цены по квартире и её активным конкурентам на период.'

    def add_arguments(self, parser):
        parser.add_argument('--apartment', type=int, required=True,
                            help='идентификатор нашей квартиры')
        parser.add_argument('--from', dest='check_in', help='дата заезда, 2026-09-20')
        parser.add_argument('--to', dest='check_out', help='дата выезда, 2026-09-23')
        parser.add_argument('--replay', help='подставной режим: папка с replay.json')
        parser.add_argument('--guests', type=int,
                            help='число гостей; одинаковое для всех объявлений')
        parser.add_argument('--headless', action='store_true',
                            help='без видимого окна (площадка ловит такой режим чаще)')
        parser.add_argument('--warmup', action='store_true',
                            help='сначала открыть главную, чтобы пройти проверку руками')
        parser.add_argument('--profile',
                            help='папка профиля браузера; профиль расходуемый, '
                                 'испорченный проще пересоздать, чем чинить')
        parser.add_argument('--limit', type=int, help='взять не больше N заданий')
        parser.add_argument('--pause', help='паузы между обращениями, «15,30»')
        parser.add_argument('--no-demo', action='store_true',
                            help='не собирать папку показа')

    def handle(self, *args, **options):
        apartment = self._apartment(options['apartment'])
        interval = self._interval(options)
        guests = options['guests'] or settings.COLLECT_GUESTS
        fetcher = self._fetcher(options, guests)
        pause = self._pause(options)

        tasks = self._build_queue(apartment, interval, guests)
        self._say(
            f'Квартира: {apartment.title}. '
            f'Период: {interval or "какой покажет площадка"}. '
            f'Гостей: {guests} (одинаково для всех объявлений). '
            f'Заданий: {len(tasks)}.'
        )
        if not tasks:
            raise CommandError(
                'Собирать нечего: у квартиры нет ни ссылки на своё объявление, '
                'ни активных конкурентов со ссылками.'
            )

        total = len(tasks)
        counter = {'index': 0}

        def progress(outcome):
            counter['index'] += 1
            self._report_task(counter['index'], total, outcome)

        try:
            report = run_queue(
                fetcher, limit=options.get('limit'), pause=pause, on_progress=progress
            )
        finally:
            fetcher.close()

        self._print_table(apartment, interval, report, guests)
        if report.stopped:
            self.stderr.write(self.style.WARNING(f'\n{report.stopped_reason}'))
        if not options['no_demo']:
            self._build_demo(apartment, interval, report, guests)

    # ---------------------------------------------------------------- разбор

    def _apartment(self, pk):
        try:
            return Apartment.objects.get(pk=pk)
        except Apartment.DoesNotExist:
            raise CommandError(f'Квартиры с идентификатором {pk} нет.')

    def _interval(self, options):
        if not options['check_in'] and not options['check_out']:
            self._say(
                'Период не задан: соберём то, что площадка покажет сама '
                '(даты в блоке «Цены по датам» выбирает она).'
            )
            return None
        interval, error = parse_interval(options['check_in'], options['check_out'])
        if error:
            raise CommandError(error)
        return interval

    def _fetcher(self, options, guests):
        if options['replay']:
            self._say(f'Подставной режим: {options["replay"]}. В сеть не ходим.')
            return ReplayFetcher.from_dir(options['replay'])
        fetcher = BrowserFetcher(
            headed=not options['headless'],
            guests=guests,
            profile_dir=Path(options['profile']) if options.get('profile') else None,
        )
        if options['warmup']:
            self._say('Открываю главную.')
            marker = fetcher.warmup()
            if marker:
                self.stderr.write(self.style.WARNING(
                    f'  Проверка площадки на входе: «{marker}». Пройдите её руками '
                    'в окне браузера. Если она не проходится — профиль исчерпан: '
                    'удалите папку профиля и запустите заново. Помните, что на '
                    'исчерпанном за сутки адресе свежий профиль живёт минуты.'
                ))
            else:
                self._say('  Проверки нет, площадка открылась сразу.')
            self._say('  Нажмите Enter, когда можно продолжать.')
            input()
        return fetcher

    def _pause(self, options):
        if not options['pause']:
            return None
        try:
            low, high = (float(part) for part in options['pause'].split(','))
        except ValueError:
            raise CommandError('Паузы задаются как «15,30».')
        return (low, high)

    # -------------------------------------------------------------- очередь

    def _build_queue(self, apartment, interval, guests):
        """Задания: сначала наша квартира, потом активные конкуренты."""
        dates = {'guests': guests}
        dates.update(
            {'check_in': interval.check_in, 'nights': interval.nights}
            if interval else {'check_in': None, 'nights': None}
        )
        tasks = []
        if apartment.avito_url:
            tasks.append(CollectTask.objects.create(apartment=apartment, **dates))
        for competitor in apartment.competitors.filter(is_active=True):
            tasks.append(CollectTask.objects.create(competitor=competitor, **dates))
        return tasks

    # --------------------------------------------------------------- печать

    def _say(self, text):
        self.stdout.write(text)

    def _report_task(self, index, total, outcome):
        task = outcome.task
        owner = task.competitor or task.apartment
        self._say(f'\n[{index}/{total}] {owner} — {task.url or "ссылки нет"}')

        page = outcome.page
        if page is None:
            self.stderr.write(self.style.ERROR(f'      {outcome.error}'))
            return

        if page.listing_price is not None:
            self._say(
                f'      витринная {format_rubles(page.listing_price)} ₽ '
                f'({page.listing_price_source}) — в сравнение не идёт'
            )
        requested = page.requested_period
        if requested is not None:
            # Вся строка блока, а не одно число из трёх: человек сверяет
            # со скриншотом именно её.
            before = (
                f'{format_rubles(requested.base_price)} ₽ {requested.discount_label} → '
                if requested.base_price is not None else ''
            )
            self._say(
                f'      цена на период: {before}'
                f'{format_rubles(requested.total_price)} ₽ за '
                f'{nights_word(requested.nights)} '
                f'→ {format_rubles(requested.price_per_night)} ₽/ночь '
                f'(источник: {requested.source})'
            )
        for period in page.periods:
            if requested is not None and period is requested:
                continue
            self._say(
                f'      соседний период: {format_rubles(period.total_price)} ₽ '
                f'за {nights_word(period.nights)} с {period.check_in:%d.%m} '
                f'→ {format_rubles(period.price_per_night)} ₽/ночь '
                f'(источник: {period.source})'
            )
        if page.status != ParseStatus.OK:
            self.stderr.write(self.style.WARNING(f'      исход: {page.status} — {page.error_note}'))

        dated = len(outcome.dated_snapshots)
        self._say(f'      снимков записано: {len(outcome.snapshots)} (на даты: {dated})')
        if page.cards_found:
            self._say(f'      карточек в блоке дат: {page.cards_found}')
        if outcome.fetch and outcome.fetch.screenshots:
            self._say(f'      кадров снято: {len(outcome.fetch.screenshots)} '
                      f'→ {outcome.fetch.screenshots[0]}')

    def _rows(self, apartment, interval, report):
        """Строки таблицы показа: наша квартира первой, дальше конкуренты.

        Когда период не задавали, объявление даёт столько строк, сколько периодов
        показала площадка: одна строка «первая попавшаяся цена» скрыла бы, что
        остальные две относятся к другим датам.
        """
        rows = []
        for outcome in report.outcomes:
            task = outcome.task
            owner = task.competitor or task.apartment
            common = {
                'row_key': outcome.task.pk,
                'title': str(owner),
                'is_ours': task.apartment_id is not None,
                'min_nights': owner.min_nights,
                'status': outcome.page.status if outcome.page else 'сбой обращения',
                'note': outcome.page.error_note if outcome.page else outcome.error,
                'url': task.url,
                'screenshots': outcome.fetch.screenshots if outcome.fetch else [],
                'screenshot': '',
                'cards_found': outcome.page.cards_found if outcome.page else 0,
            }
            matched = self._matching_snapshots(outcome, interval)
            if not matched:
                rows.append({**common, 'period': '', 'per_night': None,
                             'total': None, 'nights': None})
                continue
            for snapshot in matched:
                rows.append({
                    **common,
                    'period': f'{snapshot.check_in:%d.%m}+{snapshot.nights}',
                    'per_night': format_rubles(snapshot.price_per_night),
                    'total': format_rubles(snapshot.total_price),
                    'nights': snapshot.nights,
                })
        return rows

    def _matching_snapshots(self, outcome, interval):
        """Снимки ровно на запрошенный период. Чужой период не подставляется.

        Цена за две ночи с 20 сентября не заменяет цену за три ночи с того же
        числа: выходные внутри периода дороже будней.
        """
        good = [
            snapshot for snapshot in outcome.snapshots
            if snapshot.price_kind == PriceKind.DATED
            and snapshot.status == SnapshotStatus.OK
        ]
        if interval is None:
            return good
        return [
            snapshot for snapshot in good
            if snapshot.check_in == interval.check_in and snapshot.nights == interval.nights
        ]

    def _print_table(self, apartment, interval, report, guests):
        rows = self._rows(apartment, interval, report)
        period = str(interval) if interval else 'какой покажет площадка'
        self._say('')
        self._say('=' * 78)
        self._say(f'Квартира: {apartment.title}. Период: {period}. '
                  f'Цены на {guests_word(guests)}.')
        self._say(f'Собрано {timezone.localtime():%d.%m.%Y в %H:%M}.')
        self._say('')
        self._say(
            f'  {"объявление":<42}{"период":>10}{"за ночь":>10}'
            f'{"за период":>12}{"мин.":>6}  исход'
        )
        self._say('  ' + '─' * 88)
        previous_key = None
        for row in rows:
            title = ('★ ' if row['is_ours'] else '') + row['title']
            # Второй и третий период того же объявления идут без повтора названия.
            # Сравниваем по заданию, а не по названию: два разных конкурента
            # вполне могут называться одинаково.
            shown = '' if row['row_key'] == previous_key else title
            previous_key = row['row_key']
            per_night = f'{row["per_night"]} ₽' if row['per_night'] else '—'
            total = f'{row["total"]} ₽' if row['total'] else '—'
            min_nights = row['min_nights'] if row['min_nights'] else '—'
            status = row['status'] if shown else ''
            self._say(
                f'  {shown[:42]:<42}{row["period"] or "—":>10}{per_night:>10}'
                f'{total:>12}{min_nights:>6}  {status}'
            )
            if not row['per_night'] and row['note']:
                self._say(f'      причина: {row["note"][:120]}')
        self._say('  ' + '─' * 88)
        self._say('  Витринная цена «от N ₽ за сутки» в этой таблице не показывается:')
        self._say('  по замерам она ниже реальной в 1,25–1,9 раза.')
        for row in rows:
            if row['cards_found'] and not row['screenshots']:
                self._say(f'  Кадров карусели нет у «{row["title"]}»: '
                          f'карточек в блоке {row["cards_found"]}, сверить нечем.')
                break

    # -------------------------------------------------------- папка показа

    def _build_demo(self, apartment, interval, report, guests):
        """Одна папка со всем материалом показа: скриншоты и та же таблица."""
        stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
        folder = DEMO_ROOT / stamp
        folder.mkdir(parents=True, exist_ok=True)
        rows = self._rows(apartment, interval, report)

        seen_keys = {}
        for row in rows:
            # Строк на объявление может быть несколько (по периоду в каждой),
            # а кадры у них одни и те же — копируем один раз.
            if row['row_key'] in seen_keys:
                row['screenshot'] = seen_keys[row['row_key']]
                continue
            index = len(seen_keys) + 1
            names = []
            for shot_index, source in enumerate(row['screenshots'], start=1):
                if not source or not Path(source).exists():
                    continue
                suffix = Path(source).stem.rsplit('_', 1)[-1]
                target = folder / f'{index:02d}_{slugify(row["title"])}_{suffix}.png'
                shutil.copy2(source, target)
                names.append(target.name)
            row['screenshot'] = ', '.join(names)
            seen_keys[row['row_key']] = row['screenshot']

        period = str(interval) if interval else 'какой покажет площадка'
        lines = [
            f'# Цены конкурентов. Период: {period}',
            '',
            f'Квартира: **{apartment.title}**. '
            f'Собрано {timezone.localtime():%d.%m.%Y в %H:%M}.',
            '',
            '| объявление | период | за ночь | за период | ночей | мин. срок | исход | скриншот |',
            '|---|---|---|---|---|---|---|---|',
        ]
        for row in rows:
            title = ('**' + row['title'] + '** (наша)') if row['is_ours'] else row['title']
            lines.append(
                f'| {title} | {row["period"] or "—"} | {row["per_night"] or "—"} | '
                f'{row["total"] or "—"} | '
                f'{row["nights"] or "—"} | {row["min_nights"] or "—"} | '
                f'{row["status"]} | {row["screenshot"] or "—"} |'
            )
        lines += [
            '',
            f'Все цены сняты при **{guests_word(guests)}**: цена от числа гостей зависит,',
            'и снимки на двоих и на четверых сравнивать нельзя.',
            '',
            'Витринная цена «от N ₽ за сутки» в таблицу не входит: по замерам она ниже',
            'реальной в 1,25–1,9 раза, и сравнивать по ней нельзя.',
            '',
            'Прочерк означает, что цену получить не удалось. Причина — в колонке «исход».',
        ]
        misses = [
            f'{row["title"]}: карточек в блоке {row["cards_found"]}'
            for row in rows
            if row['cards_found'] and not row['screenshot']
        ]
        if misses:
            lines += ['', '**Кадров карусели нет, сверить числа глазами нечем:**']
            lines += [f'- {miss}' for miss in misses]
        (folder / 'table.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

        with (folder / 'table.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=['row_key', 'title', 'is_ours', 'period', 'per_night',
                            'total', 'nights', 'min_nights', 'status', 'note', 'url',
                            'screenshot', 'cards_found', 'screenshots'],
            )
            writer.writeheader()
            writer.writerows(rows)

        self._say('')
        self._say(f'Материал показа: {folder}')
