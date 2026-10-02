"""Получение страницы объявления.

Слой отделён от разбора и от очереди по одной причине: сценарии, ради которых
сбор и пишется осторожно, — блокировка, проверка поверх страницы, снятое
объявление, обрыв на середине очереди — вживую либо не воспроизводятся,
либо воспроизводятся ценой обращений к площадке. Подставной режим
(`ReplayFetcher`) отдаёт заранее заготовленный ответ и позволяет проверить
их все, ни разу никуда не сходив.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from monitor.services import Interval


@dataclass(frozen=True)
class FetchResult:
    """Что вернулось со страницы. Пути к дампу и скриншотам — для сверки глазами."""

    html: str
    http_status: int | None = None
    dump_path: str = ''
    screenshot_path: str = ''
    # Кадры виджета и карусели дат. Полностраничный кадр карусель обрезает
    # по ширине окна, а сверять надо каждое число.
    extra_screenshots: list[str] = field(default_factory=list)
    # Адрес, по которому реально сходили: с датами и числом гостей.
    requested_url: str = ''

    @property
    def screenshots(self) -> list[str]:
        return [path for path in [self.screenshot_path, *self.extra_screenshots] if path]


class Fetcher(Protocol):
    """Способ получить HTML объявления на заданный период."""

    def fetch(self, url: str, interval: Interval | None) -> FetchResult:
        ...

    def close(self) -> None:
        ...


class ReplayFetcher:
    """Подставной режим: отдаёт заготовленный ответ вместо похода на Avito.

    Значение по ссылке — либо HTML, либо `FetchResult`, либо исключение:
    исключением проверяется обрыв на середине очереди.

    Дампы не пишет: дамп — свидетельство настоящего обращения, и подделывать
    его нечем.
    """

    def __init__(self, pages: dict[str, str | FetchResult | Exception],
                 *, default: str | FetchResult | Exception | None = None):
        self._pages = pages
        self._default = default
        self.calls: list[tuple[str, Interval | None]] = []

    @classmethod
    def from_dir(cls, directory: str | Path) -> ReplayFetcher:
        """Папка с `replay.json`: {"<ссылка>": "<файл в этой же папке>"}."""
        directory = Path(directory)
        manifest = json.loads((directory / 'replay.json').read_text(encoding='utf-8'))
        pages = {
            url: (directory / name).read_text(encoding='utf-8', errors='replace')
            for url, name in manifest.items()
        }
        return cls(pages)

    def fetch(self, url: str, interval: Interval | None) -> FetchResult:
        self.calls.append((url, interval))
        response = self._pages.get(url, self._default)
        if response is None:
            raise KeyError(f'В подставном режиме нет заготовки для {url}')
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FetchResult):
            return response
        return FetchResult(html=response, http_status=200)

    def close(self) -> None:
        pass
