from django.http import HttpResponse
from django.urls import path


def healthy(request):
    return HttpResponse("OK")


# ONIX receivers and one router entry per enabled flow are added in later phases.
urlpatterns = [
    path("health", healthy, name="care_ccn_be-health"),
]
