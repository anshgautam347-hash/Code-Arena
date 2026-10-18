from django.urls import path

from . import views

app_name = "results"

urlpatterns = [
    path("competitions/<slug:slug>/results/", views.mine, name="mine"),
    path("competitions/<slug:slug>/results/submissions/<int:submission_id>/", views.submission_detail, name="submission"),
    path("competitions/<slug:slug>/leaderboard/", views.leaderboard, name="leaderboard"),
]
