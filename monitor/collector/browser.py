"""Получение страницы настоящим браузером.

Разведка фазы 0 показала, что лёгкие HTTP-запросы площадка не пропускает,
а блок цен дорисовывается на клиенте. Отсюда постоянный профиль, видимое окно
и паузы: браузер представляется собой, ничего не подменяется, проверку
площадки при её появлении проходит человек.

Скриншоты снимаются так, чтобы по ним можно было сверить каждое число:
полностраничный кадр обрезает карусель дат по ширине окна, поэтому карусель
дополнительно листается и снимается отдельными кадрами.

**Браузер живёт в отдельном потоке, и это не деталь реализации.** Синхронный
Playwright заводит в вызывающем потоке цикл событий и крутит его гринлетами,
а Django на живой цикл в текущем потоке отвечает `SynchronousOnlyOperation`
при любом обращении к базе. То есть сбор скачивал страницу и падал на записи
снимка. Поток здесь — граница: цикл событий остаётся при браузере, поток сбора
остаётся обычным синхронным кодом, и ни одна будущая работа с базой об этом
знать не обязана.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from datetime import datetime
from pathlib import Path

from django.conf import settings

from monitor.collector.avito_url import with_dates
from monitor.collector.fetcher import FetchResult
from monitor.parsing import BLOCK_MARKERS, visible_text
from monitor.services import Interval

logger = logging.getLogger(__name__)

OUT = Path(settings.BASE_DIR) / 'out'
DUMPS = OUT / 'dumps'
SHOTS = OUT / 'shots'
PROFILE = OUT / 'profile'

HOME = 'https://www.avito.ru/'

# Виджет бронирования: в нём и цена на период, и карусель ближайших дат.
BOOKING_WIDGET = '[data-marker="str-booking-widget"]'
CAROUSEL = '[data-marker="nearest-dates"]'
SCROLL_FORWARD = '[data-marker="nearest-dates/scroll-button-forward"]'

# Цена на выбранный период считается отдельным запросом уже после загрузки
# страницы, поэтому ждём не саму страницу, а появление подписи о периоде.
PERIOD_PRICE_TEXT = 'за весь период'

# Отказ приходит мгновенно и не дозагружается никогда. Ждать полной загрузки
# в таком случае значит превратить явный отказ в таймаут и потерять причину.
REFUSAL_STATUSES = (403, 429)

# Переход: ждём первый ответ, а не полную загрузку.
NAVIGATION_TIMEOUT_MS = 30000
# Скриншот отдельным пределом: на странице, которая не догружается никогда,
# он ждёт «успокоения» страницы и висит дольше всего остального вместе взятого.
SCREENSHOT_TIMEOUT_MS = 15000
# Предел на весь заход целиком, снаружи потока браузера. Последняя защита:
# если Playwright завис внутри, сбор обязан вернуть управление и упасть,
# а не стоять до утра.
FETCH_DEADLINE_SECONDS = 180


class BrowserStuck(RuntimeError):
    """Браузер не ответил за отведённое время. Заход считается несостоявшимся."""


def _slug(text: str, limit: int = 30) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]+', '-', text)[:limit].strip('-') or 'page'


def _check_marker_in(html: str) -> str:
    """Признак проверки площадки в разметке, если он есть."""
    text = visible_text(html).lower()
    for marker in BLOCK_MARKERS:
        if marker in text:
            return marker
    return ''


def sync_playwright():
    """Playwright ввозится лениво: на сервере его может не быть вовсе,
    а модуль читается при импорте приложения. Отдельной функцией — чтобы
    тесты подменяли её и проверяли поток, не поднимая браузера."""
    from playwright.sync_api import sync_playwright as _sync_playwright

    return _sync_playwright()


class BrowserFetcher:
    """Постоянный профиль, своя вкладка, дамп и скриншоты при каждом заходе.

    Профиль хранит пройденную человеком проверку и переживает перезапуск.
    Восстановленные вкладки закрываются: один раз разведка намертво зависла
    на обращении к такой вкладке, и снаружи это было неотличимо от медленной
    работы.
    """

    def __init__(self, *, headed: bool = True, guests: int | None = None,
                 profile_dir: Path | None = None, wait_price_ms: int = 15000,
                 settle_ms: int = 2500,
                 deadline_seconds: int = FETCH_DEADLINE_SECONDS):
        self.headed = headed
        self.guests = settings.COLLECT_GUESTS if guests is None else guests
        self.profile_dir = profile_dir or PROFILE
        self.wait_price_ms = wait_price_ms
        self.settle_ms = settle_ms
        self.deadline_seconds = deadline_seconds
        self._playwright = None
        self._context = None
        self._page = None
        # Всё, что ниже, трогает только рабочий поток.
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        DUMPS.mkdir(parents=True, exist_ok=True)
        SHOTS.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------- рабочий поток

    def _run_in_worker(self, work):
        """Выполнить работу в потоке браузера и дождаться результата.

        Ожидание ограничено по времени. Внутренние пределы Playwright уже стоят
        на каждом шаге, но экран проверки не догружается никогда, и полагаться
        только на них значит однажды простоять всю ночь: процесс не завершится
        сам, и убивать его придётся руками.

        Исключение перебрасывается вызывающему как есть: сборщик отличает сбой
        обращения от сбоя страницы по типу, и подменять его обёрткой нельзя.
        """
        self._ensure_worker()
        answer: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((work, answer))
        try:
            outcome, payload = answer.get(timeout=self.deadline_seconds)
        except queue.Empty:
            # Поток застрял внутри Playwright, штатно его не остановить.
            # Бросаем его демоном и говорим вызывающему правду.
            self._worker = None
            raise BrowserStuck(
                f'Браузер не ответил за {self.deadline_seconds} с. '
                'Похоже на экран проверки, который не догружается никогда.'
            )
        if outcome == 'error':
            raise payload
        return payload

    def _ensure_worker(self):
        if self._worker is not None and self._worker.is_alive():
            return
        started: queue.Queue = queue.Queue(maxsize=1)
        self._worker = threading.Thread(
            target=self._serve, args=(started,), name='avito-browser', daemon=True
        )
        self._worker.start()
        outcome, payload = started.get()
        if outcome == 'error':
            self._worker = None
            raise payload

    def _serve(self, started: queue.Queue):
        """Тело рабочего потока: здесь и только здесь живёт цикл событий."""
        try:
            self._open_browser()
        except Exception as error:
            started.put(('error', error))
            return
        started.put(('ok', None))

        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                work, answer = job
                try:
                    answer.put(('ok', work()))
                except Exception as error:
                    answer.put(('error', error))
        finally:
            # Браузер закрывается при любом выходе из потока. Без этого
            # процесс Chromium переживает сбор и висит до перезагрузки.
            self._close_browser()

    # ------------------------------------------------------------- браузер

    def _open_browser(self):
        """Поднять браузер. Выполняется только в рабочем потоке."""
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=not self.headed,
            locale='ru-RU',
            timezone_id='Europe/Moscow',
            viewport={'width': 1440, 'height': 900},
        )
        page = self._context.new_page()
        for other in self._context.pages:
            if other is not page:
                try:
                    other.close()
                except Exception:
                    logger.debug('не закрылась восстановленная вкладка', exc_info=True)
        self._context.set_default_timeout(45000)
        self._context.set_default_navigation_timeout(60000)
        self._page = page

    def _close_browser(self) -> None:
        """Закрыть браузер. Выполняется только в рабочем потоке."""
        for closer in (self._context, self._playwright):
            if closer is None:
                continue
            try:
                closer.stop() if hasattr(closer, 'stop') else closer.close()
            except Exception:
                logger.debug('браузер не закрылся штатно', exc_info=True)
        self._context = self._playwright = self._page = None

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None or not worker.is_alive():
            return
        self._jobs.put(None)
        worker.join(timeout=30)

    def warmup(self) -> str:
        """Открыть главную и сказать, показала ли площадка проверку.

        Возвращает найденный признак проверки или пустую строку. Профиль
        расходуемый: испорченный отвечает проверкой сразу, и знать это надо
        до того, как пойдут обращения по объявлениям.
        """
        return self._run_in_worker(self._do_warmup)

    def _do_warmup(self) -> str:
        self._page.goto(HOME, wait_until='domcontentloaded', timeout=60000)
        self._page.wait_for_timeout(2000)
        return self.check_marker()

    def check_marker(self) -> str:
        """Признак проверки площадки на текущей странице, если он есть."""
        return _check_marker_in(self._page.content())

    # -------------------------------------------------------------- заход

    def fetch(self, url: str, interval: Interval | None) -> FetchResult:
        return self._run_in_worker(lambda: self._do_fetch(url, interval))

    def _do_fetch(self, url: str, interval: Interval | None) -> FetchResult:
        page = self._page
        target = with_dates(url, interval, self.guests)
        tag = self._tag(url, interval)

        # Ждём первый ответ, а не полную загрузку: страница проверки отдаётся
        # мгновенно и не дозагружается никогда. Ожидание полной загрузки
        # превращает явный отказ в таймаут и теряет причину — на этом уже
        # один раз встала разведка фазы 0.
        response = page.goto(
            target, wait_until='commit', timeout=NAVIGATION_TIMEOUT_MS
        )
        status = response.status if response else None
        refused = status in REFUSAL_STATUSES

        html = page.content()
        marker = _check_marker_in(html)

        # Отказ разбираем сразу и уходим. Скриншот здесь не снимаем осознанно:
        # на странице, которая не догружается, он ждёт её «успокоения» дольше
        # всего остального вместе взятого, а сверять на ней нечего.
        if refused or marker:
            logger.warning(
                'Площадка отказала: ответ %s, признак «%s». Заход прекращён.',
                status, marker or 'по коду ответа',
            )
            return FetchResult(
                html=html,
                http_status=status,
                dump_path=str(self._save_dump(html, tag)),
                requested_url=target,
            )

        self._wait_for_price(page, interval)
        html = page.content()
        shots = self._shoot(page, tag)

        return FetchResult(
            html=html,
            http_status=status,
            dump_path=str(self._save_dump(html, tag)),
            screenshot_path=str(shots[0]) if shots else '',
            extra_screenshots=[str(path) for path in shots[1:]],
            requested_url=target,
        )

    def _save_dump(self, html: str, tag: str) -> Path:
        """Дамп пишется при любом исходе, включая отказ: это материал разбора."""
        path = DUMPS / f'{tag}.html'
        path.write_text(html, encoding='utf-8')
        return path

    def _tag(self, url: str, interval: Interval | None) -> str:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        item = _slug(url.rstrip('/').rsplit('/', 1)[-1].split('?')[0])
        period = (
            f'_{interval.check_in:%Y%m%d}-{interval.nights}n' if interval else '_nodate'
        )
        return f'{stamp}_{item}{period}_g{self.guests}'

    def _wait_for_price(self, page, interval) -> None:
        """Дождаться цены, но не падать, если она не появилась.

        Не появилась — это факт страницы, и разбирать его должен парсер,
        а не исключение из слоя обращения: иначе теряется и дамп, и причина.
        """
        try:
            page.wait_for_load_state('domcontentloaded', timeout=20000)
        except Exception:
            logger.debug('страница не догрузилась до domcontentloaded', exc_info=True)

        wanted = PERIOD_PRICE_TEXT if interval is not None else None
        try:
            page.wait_for_selector(BOOKING_WIDGET, timeout=self.wait_price_ms)
            if wanted:
                page.get_by_text(wanted).first.wait_for(timeout=self.wait_price_ms)
            else:
                page.wait_for_selector(CAROUSEL, timeout=self.wait_price_ms)
        except Exception:
            logger.info('блок цены не дождались — разберём страницу как есть')

        # Цена считается отдельным запросом уже после появления блока.
        page.wait_for_timeout(self.settle_ms)

    # ---------------------------------------------------------- скриншоты

    def _shoot(self, page, tag: str) -> list[Path]:
        """Полностраничный кадр, кадр виджета и карусель по шагам.

        Полностраничный кадр карусель обрезает: она внутри родителя с обрезкой
        по ширине, и уехавшие за край карточки в кадр не попадают. Поэтому
        карусель листается той же кнопкой, что нажал бы человек.
        """
        shots: list[Path] = []

        full = SHOTS / f'{tag}_full.png'
        try:
            page.screenshot(path=str(full), full_page=True,
                            timeout=SCREENSHOT_TIMEOUT_MS)
            shots.append(full)
        except Exception:
            logger.warning('не снялся полностраничный кадр', exc_info=True)

        widget = page.locator(BOOKING_WIDGET).first
        try:
            if widget.count():
                path = SHOTS / f'{tag}_widget.png'
                widget.screenshot(path=str(path),
                                  timeout=SCREENSHOT_TIMEOUT_MS)
                shots.append(path)
        except Exception:
            logger.warning('не снялся кадр виджета бронирования', exc_info=True)

        shots += self._shoot_carousel(page, tag)
        return shots

    def _shoot_carousel(self, page, tag: str) -> list[Path]:
        """Листать карусель дат и снимать её после каждого шага."""
        shots: list[Path] = []
        carousel = page.locator(CAROUSEL).first
        try:
            if not carousel.count():
                return shots
        except Exception:
            return shots

        # Верхняя граница на число шагов: карусель конечна, а вечный цикл
        # в сборе, который и так идёт минутами, отлаживать потом нечем.
        for step in range(1, 7):
            path = SHOTS / f'{tag}_dates_{step}.png'
            try:
                carousel.screenshot(path=str(path),
                                    timeout=SCREENSHOT_TIMEOUT_MS)
                shots.append(path)
            except Exception:
                logger.warning('не снялся кадр карусели дат', exc_info=True)
                break

            forward = page.locator(SCROLL_FORWARD).first
            try:
                if not forward.count() or not forward.is_enabled():
                    break
                forward.click()
                page.wait_for_timeout(600)
            except Exception:
                break
        return shots
