"""Модели: наши квартиры, объявления конкурентов и неизменяемые снимки цен."""

from django.db import models
from django.db.models import Q


class SnapshotIsImmutable(Exception):
    """Попытка изменить или удалить уже записанный снимок цены."""


class Apartment(models.Model):
    """Наша квартира."""

    title = models.CharField('название', max_length=200)
    address = models.CharField('адрес', max_length=300, blank=True)
    avito_url = models.URLField('объявление на Avito', max_length=500, blank=True)
    realtycalendar_id = models.CharField(
        'идентификатор в RealtyCalendar', max_length=100, blank=True
    )
    min_nights = models.PositiveSmallIntegerField('минимальный срок, ночей', default=2)
    is_active = models.BooleanField('в работе', default=True)
    created_at = models.DateTimeField('заведена', auto_now_add=True)

    class Meta:
        verbose_name = 'квартира'
        verbose_name_plural = 'квартиры'
        ordering = ['title']

    def __str__(self):
        return self.title


class Competitor(models.Model):
    """Объявление конкурента, привязанное к одной нашей квартире.

    Список ведёт менеджер вручную. Убирается из работы флагом is_active:
    снимки сохраняются всегда.
    """

    apartment = models.ForeignKey(
        Apartment,
        on_delete=models.CASCADE,
        related_name='competitors',
        verbose_name='квартира',
    )
    url = models.URLField('ссылка на объявление', max_length=500)
    # Заголовок и минимальный срок заполнит парсер. Минимального срока на страницах
    # Avito нет вовсе (разведка фазы 0), поэтому поле может остаться пустым навсегда.
    title = models.CharField('заголовок', max_length=300, blank=True)
    min_nights = models.PositiveSmallIntegerField(
        'минимальный срок, ночей', null=True, blank=True
    )
    is_active = models.BooleanField('в работе', default=True)
    created_at = models.DateTimeField('заведён', auto_now_add=True)

    class Meta:
        verbose_name = 'конкурент'
        verbose_name_plural = 'конкуренты'
        ordering = ['title', 'url']
        constraints = [
            models.UniqueConstraint(
                fields=['apartment', 'url'], name='competitor_unique_url_per_apartment'
            )
        ]

    def __str__(self):
        return self.title or self.url


class PriceKind(models.TextChoices):
    """Витринная цена и цена на даты — разные числа, а не одно поле.

    Витринная («от N ₽ за сутки») ниже реальной в 1,25–1,9 раза по замерам фазы 0.
    В сравнение по датам она не попадает никогда.
    """

    LISTING = 'listing', 'витринная «от N ₽»'
    DATED = 'dated', 'на конкретные даты'


class Source(models.TextChoices):
    AVITO = 'avito', 'Avito'
    REALTYCALENDAR = 'realtycalendar', 'RealtyCalendar'


class SnapshotStatus(models.TextChoices):
    OK = 'ok', 'получено'
    FAILED = 'failed', 'сбой'
    BLOCKED = 'blocked', 'блокировка'
    NOT_AVAILABLE = 'not_available', 'объявление недоступно'
    # Страница загрузилась, но проверка перекрыла блок цен: витринная цена при этом
    # доступна, а цен по датам нет. Отдельный исход, потому что это не полный отказ.
    CAPTCHA_OVERLAY = 'captcha_overlay', 'проверка поверх страницы'


class PriceSnapshot(models.Model):
    """Один замер цены. Никогда не обновляется и не удаляется.

    Каждый сбор создаёт новые строки, включая повторный сбор на ту же дату.
    Историю нельзя восстановить задним числом, а стоит она ничего.
    """

    # PROTECT, а не CASCADE: квартиру или конкурента со снимками не должно быть
    # возможно удалить ничем — ни админкой, ни queryset.delete(), ни каскадом.
    competitor = models.ForeignKey(
        Competitor,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='snapshots',
        verbose_name='конкурент',
    )
    apartment = models.ForeignKey(
        Apartment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='snapshots',
        verbose_name='квартира',
    )

    price_kind = models.CharField('вид цены', max_length=16, choices=PriceKind.choices)
    check_in = models.DateField('дата заезда', null=True, blank=True)
    nights = models.PositiveSmallIntegerField('ночей', null=True, blank=True)

    # При nights > 1 это среднее по диапазону, а не цена конкретной ночи.
    price_per_night = models.DecimalField(
        'цена за ночь', max_digits=10, decimal_places=2, null=True, blank=True
    )
    total_price = models.DecimalField(
        'сумма за период', max_digits=10, decimal_places=2, null=True, blank=True
    )
    # None = не выяснено. Входит ли сервисный сбор площадки в сумму — вопрос
    # заказчику задан, ответа нет. Догадка здесь была бы хуже пустоты.
    fees_included = models.BooleanField('сервисный сбор включён', null=True, blank=True)

    collected_at = models.DateTimeField('собрано', db_index=True)
    source = models.CharField('источник', max_length=20, choices=Source.choices)
    status = models.CharField('исход', max_length=20, choices=SnapshotStatus.choices)
    error_note = models.TextField('причина', blank=True)
    raw_dump_path = models.CharField('дамп страницы', max_length=500, blank=True)

    class Meta:
        verbose_name = 'снимок цены'
        verbose_name_plural = 'снимки цен'
        ordering = ['-collected_at']
        indexes = [
            models.Index(
                fields=['competitor', 'check_in', '-collected_at'],
                name='snapshot_competitor_idx',
            ),
            models.Index(
                fields=['apartment', 'check_in', '-collected_at'],
                name='snapshot_apartment_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(competitor__isnull=False, apartment__isnull=True)
                    | Q(competitor__isnull=True, apartment__isnull=False)
                ),
                name='snapshot_exactly_one_owner',
            ),
            # Витринная цена не привязана к датам. Снимок, у которого вид не сходится
            # с датами, в базу не попадает: подстановка витринной цены вместо реальной
            # — самая опасная ошибка в этом проекте, и ловить её надо на уровне БД.
            models.CheckConstraint(
                condition=(
                    ~Q(price_kind=PriceKind.LISTING)
                    | Q(check_in__isnull=True, nights__isnull=True)
                ),
                name='snapshot_listing_has_no_dates',
            ),
            # Даты обязательны только у успешного снимка на даты. Сбой пишется в любом
            # случае, даже когда неизвестно, на какую дату он случился: пока не закрыт
            # вопрос, управляются ли даты адресом страницы, требовать их нельзя.
            models.CheckConstraint(
                condition=(
                    ~Q(price_kind=PriceKind.DATED, status=SnapshotStatus.OK)
                    | Q(check_in__isnull=False, nights__isnull=False)
                ),
                name='snapshot_dated_ok_has_dates',
            ),
            # Исход «получено» без цены — ровно тот ложный успех, который дал первый
            # прогон разведки. Пустая цена обязана иметь статус ошибки.
            models.CheckConstraint(
                condition=~Q(status=SnapshotStatus.OK) | Q(price_per_night__isnull=False),
                name='snapshot_ok_has_price',
            ),
        ]

    def __str__(self):
        owner = self.competitor or self.apartment
        when = self.check_in.isoformat() if self.check_in else 'без дат'
        return f'{owner} — {when} — {self.get_status_display()}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise SnapshotIsImmutable(
                'Снимок цены изменять нельзя: каждый сбор создаёт новую строку.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise SnapshotIsImmutable('Снимки цен не удаляются.')
