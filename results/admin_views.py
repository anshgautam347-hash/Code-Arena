"""Administrator pages for results. Wired into the Competition admin."""
import csv

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from competitions.models import Competition
from judging import services as judging_services
from judging.models import Submission

from .services import build_standings


def set_published(queryset, published):
    return queryset.update(results_published_at=timezone.now() if published else None)


def _safe_cell(value):
    """Stop spreadsheet formula injection when a CSV is opened in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def standings(request, pk):
    if not request.user.has_perm("competitions.view_competition"):
        raise PermissionDenied
    competition = get_object_or_404(Competition, pk=pk)

    if request.method == "POST":
        if not request.user.has_perm("competitions.change_competition"):
            raise PermissionDenied
        action = request.POST.get("action")
        if action in ("publish", "unpublish"):
            set_published(Competition.objects.filter(pk=pk), action == "publish")
            messages.success(request, "Results published." if action == "publish" else "Results unpublished.")
        return redirect(request.path)

    judging_services.reconcile(Submission.objects.filter(session__competition=competition), limit=100)
    rows, questions = build_standings(competition)

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{competition.slug}-standings.csv"'
        writer = csv.writer(response)
        writer.writerow(["Rank", "Username", "Email", "Exam status", "Total"] + [q.title for q in questions]
                        + ["Time to final score", "Scored submissions", "Counted violation indicators",
                           "Review status", "Excluded from leaderboard"])
        for r in rows:
            writer.writerow([_safe_cell(v) for v in (
                r["rank"] or "", r["username"], r["email"], r["status"], r["total"],
                *[r["scores"][q.pk] for q in questions], r["finish_display"], r["attempts"], r["violations"],
                r["review_status"], "yes" if r["excluded"] else "no")])
        return response

    context = {
        **admin.site.each_context(request),
        "title": f"Standings: {competition.title}",
        "competition": competition, "rows": rows, "questions": questions,
        "opts": Competition._meta, "can_change": request.user.has_perm("competitions.change_competition"),
    }
    return render(request, "admin/results/standings.html", context)
