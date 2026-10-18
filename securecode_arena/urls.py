from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("exams.urls")),
    path("", include("judging.urls")),
    path("", include("results.urls")),
    path("", include("competitions.urls")),
]
