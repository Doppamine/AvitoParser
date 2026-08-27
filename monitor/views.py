"""Страница сравнения цен. Решения о ценах принимает человек: страница только показывает."""

from django.contrib import messages
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from monitor.forms import CompetitorAddForm
from monitor.models import Apartment, Competitor, PriceKind, PriceSnapshot, SnapshotStatus
from monitor.services import latest_prices, parse_interval, sort_rows

# Параметры вида, которые надо сохранить после добавления или удаления конкурента:
# менеджер должна вернуться к тому же периоду и той же сортировке.
VIEW_PARAMS = ('from', 'to', 'sort', 'show_inactive')


def apartment_list(request):
    # Успешным считается сбор, давший цену на дату: только такие цены видит менеджер.
    # Одна витринная цена — не повод писать, что данные свежие.
    last_success = (
        PriceSnapshot.objects.filter(status=SnapshotStatus.OK, price_kind=PriceKind.DATED)
        .filter(Q(apartment=OuterRef('pk')) | Q(competitor__apartment=OuterRef('pk')))
        .order_by('-collected_at')
        .values('collected_at')[:1]
    )
    apartments = Apartment.objects.filter(is_active=True).annotate(
        active_competitors=Count(
            'competitors', filter=Q(competitors__is_active=True), distinct=True
        ),
        last_success=Subquery(last_success),
    )
    return render(request, 'monitor/apartment_list.html', {'apartments': apartments})


def apartment_detail(request, pk):
    apartment = get_object_or_404(Apartment, pk=pk)

    interval, error = parse_interval(request.GET.get('from'), request.GET.get('to'))
    if error:
        messages.warning(request, error)

    table = latest_prices(apartment, interval)

    sort_by = 'price' if request.GET.get('sort') == 'price' else 'title'
    show_inactive = request.GET.get('show_inactive') == '1'

    rows = table.active_competitors
    if show_inactive:
        rows = rows + table.inactive_competitors
    rows = sort_rows(rows, sort_by)

    sort_param = 'price' if sort_by == 'price' else None
    return render(
        request,
        'monitor/apartment_detail.html',
        {
            'apartment': apartment,
            'table': table,
            'interval': interval,
            'rows': rows,
            'sort_by': sort_by,
            'show_inactive': show_inactive,
            'inactive_count': len(table.inactive_competitors),
            'links': {
                'by_title': _view_url(apartment, interval, show_inactive=show_inactive),
                'by_price': _view_url(
                    apartment, interval, sort='price', show_inactive=show_inactive
                ),
                'with_inactive': _view_url(
                    apartment, interval, sort=sort_param, show_inactive=True
                ),
                'without_inactive': _view_url(apartment, interval, sort=sort_param),
            },
        },
    )


def _view_url(apartment, interval, *, sort=None, show_inactive=False):
    """Адрес страницы с этим же периодом: такую ссылку можно переслать."""
    params = QueryDict(mutable=True)
    params['from'] = interval.check_in.isoformat()
    params['to'] = interval.check_out.isoformat()
    if sort:
        params['sort'] = sort
    if show_inactive:
        params['show_inactive'] = '1'
    path = reverse('apartment-detail', args=[apartment.pk])
    return f'{path}?{params.urlencode()}'


@require_POST
def competitor_add(request, pk):
    apartment = get_object_or_404(Apartment, pk=pk)
    form = CompetitorAddForm(request.POST, apartment=apartment)
    if form.is_valid():
        competitor = form.save()
        messages.success(
            request,
            f'Объявление добавлено: {competitor}. '
            'Заголовок и цены появятся после ближайшего сбора.',
        )
    else:
        for error in form.errors.get('url', []):
            messages.error(request, error)
    return redirect(_back_url(request, apartment))


@require_POST
def competitor_retire(request, pk, competitor_pk):
    """Мягкое удаление: конкурент уходит из таблицы, снимки остаются в истории."""
    apartment = get_object_or_404(Apartment, pk=pk)
    competitor = get_object_or_404(Competitor, pk=competitor_pk, apartment=apartment)
    competitor.is_active = False
    competitor.save(update_fields=['is_active'])
    messages.success(request, f'{competitor} убран из работы. Снимки сохранены.')
    return redirect(_back_url(request, apartment))


@require_POST
def competitor_restore(request, pk, competitor_pk):
    apartment = get_object_or_404(Apartment, pk=pk)
    competitor = get_object_or_404(Competitor, pk=competitor_pk, apartment=apartment)
    competitor.is_active = True
    competitor.save(update_fields=['is_active'])
    messages.success(request, f'{competitor} снова в работе.')
    return redirect(_back_url(request, apartment))


def _back_url(request, apartment):
    """Вернуть менеджера туда же, где она была: с той же сортировкой и раскрытием.

    Из присланной строки берутся только known параметры вида — что угодно другое
    в адрес не попадает.
    """
    url = reverse('apartment-detail', args=[apartment.pk])
    sent = QueryDict(request.POST.get('back', ''))
    kept = QueryDict(mutable=True)
    for name in VIEW_PARAMS:
        if sent.get(name):
            kept[name] = sent[name]
    return f'{url}?{kept.urlencode()}' if kept else url
