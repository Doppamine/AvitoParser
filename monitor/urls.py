from django.urls import path

from monitor import views

urlpatterns = [
    path('', views.apartment_list, name='apartment-list'),
    path('apartments/<int:pk>/', views.apartment_detail, name='apartment-detail'),
]
