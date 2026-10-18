from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "exams"

urlpatterns = [
    # Participant accounts
    path("accounts/login/", views.ParticipantLoginView.as_view(), name="login"),
    path("accounts/register/", views.register, name="register"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    # Exam pages. The participant is always the signed-in user; there is no session id in any URL.
    path("competitions/<slug:slug>/exam/start/", views.start, name="start"),
    path("competitions/<slug:slug>/exam/", views.exam, name="exam"),
    # Exam JSON endpoints used by exam.js
    path("competitions/<slug:slug>/exam/state/", views.state, name="state"),
    path("competitions/<slug:slug>/exam/begin/", views.begin, name="begin"),
    path("competitions/<slug:slug>/exam/questions/", views.questions, name="questions"),
    path("competitions/<slug:slug>/exam/answers/<int:question_id>/", views.answer, name="answer"),
    path("competitions/<slug:slug>/exam/violations/", views.violation, name="violation"),
    path("competitions/<slug:slug>/exam/submit/", views.submit, name="submit"),
]
