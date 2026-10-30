from care_ccn_be.flows.appointment.flow import AppointmentFlow
from care_ccn_be.urls import build_urlpatterns

urlpatterns = build_urlpatterns([AppointmentFlow])
