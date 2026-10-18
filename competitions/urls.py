from django.urls import path
from . import views

app_name = "competitions"
urlpatterns = [
    path("", views.home, name="home"),
    path("competitions/<slug:slug>/", views.detail, name="detail"),
]
