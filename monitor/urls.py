from django.urls import path

from monitor import views

urlpatterns = [
    path('', views.apartment_list, name='apartment-list'),
    path('apartments/<int:pk>/', views.apartment_detail, name='apartment-detail'),
    path('apartments/<int:pk>/competitors/add/', views.competitor_add, name='competitor-add'),
    path(
        'apartments/<int:pk>/competitors/<int:competitor_pk>/retire/',
        views.competitor_retire,
        name='competitor-retire',
    ),
    path(
        'apartments/<int:pk>/competitors/<int:competitor_pk>/restore/',
        views.competitor_restore,
        name='competitor-restore',
    ),
]
