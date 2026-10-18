from django.urls import path

from . import views

app_name = "judging"

urlpatterns = [
    path("competitions/<slug:slug>/exam/judging/", views.overview, name="overview"),
    path("competitions/<slug:slug>/exam/solutions/<int:question_id>/run/", views.run, name="run"),
    path("competitions/<slug:slug>/exam/solutions/<int:question_id>/submit/", views.submit, name="submit"),
    path("competitions/<slug:slug>/exam/submissions/", views.history, name="history"),
    path("competitions/<slug:slug>/exam/submissions/<int:submission_id>/", views.submission, name="submission"),
]
