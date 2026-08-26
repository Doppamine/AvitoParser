"""Формы страницы квартиры. Менеджер ведёт список конкурентов здесь, а не в админке."""

from django import forms
from django.core.exceptions import ValidationError

from monitor.models import Competitor


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
        url = self.cleaned_data['url']
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
