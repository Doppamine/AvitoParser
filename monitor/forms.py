"""Формы списка квартир и страницы квартиры.

Свои квартиры и список конкурентов менеджер ведёт здесь, а не в админке:
админка — инструмент разработчика, и заказчице туда ходить незачем.
"""

from django import forms
from django.core.exceptions import ValidationError

from monitor.models import Apartment, Competitor
from monitor.validators import normalize_avito_url

# Предел на минимальный срок проживания. Ровно та же мысль, что и предел
# на длину периода в services: описка не должна превращаться в «год».
MAX_MIN_NIGHTS = 30


class CompetitorAddForm(forms.ModelForm):
    """Добавление объявления конкурента одной ссылкой.

    Проверка домена живёт в валидаторе поля модели, поэтому она одинакова
    здесь и в админке. Здесь добавляется только запрет дублей: квартира в форме
    не участвует, а без неё Django пару (apartment, url) не проверит.
    """

    class Meta:
        model = Competitor
        fields = ['url']

    def __init__(self, *args, apartment=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.apartment = apartment

    def clean_url(self):
        # Сравниваем нормализованное с нормализованным: ссылка из адресной строки
        # тащит за собой checkIn и гостей, и без этого она пройдёт как новая.
        url = normalize_avito_url(self.cleaned_data['url'])
        if url == self.apartment.avito_url:
            raise ValidationError(
                'Это ваше собственное объявление — оно уже стоит в карточке '
                'квартиры. Сравнивать его с самим собой не с чем.'
            )
        existing = Competitor.objects.filter(apartment=self.apartment, url=url).first()
        if existing is None:
            return url
        if existing.is_active:
            raise ValidationError('Это объявление уже есть в списке.')
        raise ValidationError(
            'Это объявление уже есть в списке, но убрано из работы. '
            'Покажите неактивных и верните его в работу.'
        )

    def save(self, commit=True):
        self.instance.apartment = self.apartment
        return super().save(commit)


class ApartmentForm(forms.ModelForm):
    """Заведение и правка своей квартиры.

    Заказчицу в админку не пускаем, а объекты она заводит сама, поэтому набор
    полей здесь тот же, что в админке, за вычетом служебных: `is_active` и
    `realtycalendar_id` меняются раз в жизни и не её руками.

    Одна форма на оба случая: поля и проверки при заведении и при правке
    совпадают, а две формы разъехались бы на первой же новой проверке.

    Ссылка на своё объявление обязательна. Без неё нет своей цены, а без своей
    цены таблица перестаёт быть сравнением: подсвечивать «дешевле нас» не от
    чего, и сортировка по цене показывает чужие числа без точки отсчёта.
    """

    class Meta:
        model = Apartment
        fields = ['title', 'address', 'min_nights', 'avito_url']
        labels = {
            'min_nights': 'Мин. срок, ночей',
            'avito_url': 'Своё объявление на Avito',
        }
        help_texts = {
            'avito_url': 'Своя цена собирается по ней же, что и цены конкурентов. '
                         'Без неё сравнивать не с чем.',
        }
        widgets = {
            'title': forms.TextInput(
                attrs={'class': 'form-control form-control-sm',
                       'placeholder': 'Студия на Мясницкой'}
            ),
            'address': forms.TextInput(
                attrs={'class': 'form-control form-control-sm',
                       'placeholder': 'Москва, Мясницкая, 1'}
            ),
            'min_nights': forms.NumberInput(
                attrs={'class': 'form-control form-control-sm', 'max': MAX_MIN_NIGHTS}
            ),
            'avito_url': forms.TextInput(
                attrs={'class': 'form-control form-control-sm',
                       'placeholder': 'https://www.avito.ru/moskva/kvartiry/...'}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Поле модели положительное, поэтому Django ставит браузеру min=0.
        # Подсказка браузера должна совпадать с проверкой ниже, иначе форма
        # молча отправляет то, что сервер потом отвергает.
        self.fields['min_nights'].widget.attrs['min'] = 1

        # На модели поле остаётся blank=True: там уже лежат квартиры без ссылки,
        # и запрет на уровне поля сделал бы их несохраняемыми чем угодно, включая
        # админку. Обязательность — правило заведения, и живёт она в форме.
        self.fields['avito_url'].required = True
        self.fields['avito_url'].error_messages['required'] = (
            'Укажите ссылку на своё объявление: без своей цены сравнивать не с чем.'
        )

    def clean_avito_url(self):
        """Та же проверка, что у ссылки конкурента, плюс запрет на дубль.

        Дубль ищется в двух местах, потому что испортить сравнение можно двумя
        способами: одно объявление у двух наших квартир — это одна квартира,
        заведённая дважды; одно объявление у квартиры и у её же конкурента —
        это строка, сравниваемая сама с собой.
        """
        url = normalize_avito_url(self.cleaned_data['avito_url'])

        twins = Apartment.objects.filter(avito_url=url)
        if self.instance.pk:
            twins = twins.exclude(pk=self.instance.pk)
        twin = twins.first()
        if twin is not None:
            raise ValidationError(
                f'Это объявление уже стоит у квартиры «{twin.title}». '
                'У одного объявления одна квартира.'
            )

        # Убранный из работы конкурент не мешает: в таблице его нет, а удалить
        # его нечем — снимки держат строку. Иначе описка запирала бы поле навсегда.
        if self.instance.pk:
            rival = self.instance.competitors.filter(url=url, is_active=True).first()
            if rival is not None:
                raise ValidationError(
                    'Это объявление заведено конкурентом этой же квартиры. '
                    'Уберите его из конкурентов крестиком и сохраните снова.'
                )
        return url

    def clean_min_nights(self):
        """Ноль ночей и «год» — обе описка, и обе портят подпись в таблице."""
        nights = self.cleaned_data['min_nights']
        if not 1 <= nights <= MAX_MIN_NIGHTS:
            raise ValidationError(
                f'Минимальный срок — от одной ночи до {MAX_MIN_NIGHTS}.'
            )
        return nights
