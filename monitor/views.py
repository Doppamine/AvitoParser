"""Страница сравнения цен. Решения о ценах принимает человек: страница только показывает."""

from datetime import date

from django.shortcuts import get_object_or_404, render

from monitor.models import Apartment
from monitor.services import horizon_dates, latest_prices, sort_rows


def apartment_list(request):
    apartments = Apartment.objects.filter(is_active=True)
    return render(request, 'monitor/apartment_list.html', {'apartments': apartments})


def apartment_detail(request, pk):
    apartment = get_object_or_404(Apartment, pk=pk)
    dates = horizon_dates()
    table = latest_prices(apartment, dates)

    sort_date = _parse_sort_date(request.GET.get('sort'), dates)
    show_inactive = request.GET.get('show_inactive') == '1'

    rows = table.active_competitors
    if show_inactive:
        rows = rows + table.inactive_competitors
    rows = sort_rows(rows, sort_date)

    return render(
        request,
        'monitor/apartment_detail.html',
        {
            'apartment': apartment,
            'table': table,
            'rows': rows,
            'sort_date': sort_date,
            'show_inactive': show_inactive,
            'inactive_count': len(table.inactive_competitors),
        },
    )


def _parse_sort_date(value, dates):
    """Сортировать можно только по колонке, которая есть на экране."""
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed in dates else None
