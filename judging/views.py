"""JSON endpoints used by the exam screen.

They reuse the Stage 3 exam wrapper, so every call is tied to the signed-in user's own
session (there is no session id in any URL), is never cached, and answers rule
violations as JSON errors.
"""
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from exams import services as exam_services
from exams.services import ExamError
from exams.views import _api as exam_api

from . import services
from .models import Submission


def _start(request, session, question_id, kind):
    submission = services.create_submission(
        session, question_id, request.POST.get("language", ""), request.POST.get("code", ""), kind
    )
    return JsonResponse(
        {"submission": services.participant_payload(submission), "state": exam_services.state_payload(session)},
        status=201,
    )


@exam_api
@require_GET
def overview(request, session):
    return JsonResponse(services.overview_payload(session))


@exam_api
@require_POST
def run(request, session, question_id):
    return _start(request, session, question_id, Submission.Kind.RUN)


@exam_api
@require_POST
def submit(request, session, question_id):
    return _start(request, session, question_id, Submission.Kind.SUBMIT)


@exam_api
@require_GET
def submission(request, session, submission_id):
    found = Submission.objects.filter(pk=submission_id, session=session).first()
    if found is None:
        raise ExamError("not_found", "Submission not found.", 404)
    found = services.refresh_submission(found)
    return JsonResponse({"submission": services.participant_payload(found), "state": exam_services.state_payload(session)})


@exam_api
@require_GET
def history(request, session):
    qs = session.submissions.select_related("language", "question")
    question = request.GET.get("question", "")
    if question.isdigit():
        qs = qs.filter(question_id=int(question))
    return JsonResponse({"submissions": [services.participant_payload(s, detail=False) for s in qs[:50]]})
