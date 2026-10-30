"""NFH health vocabulary shared by every health flow."""

from datetime import date

from care_ccn_be.protocol.schema.base import BecknModel, IsoDateTime, JsonLdModel

V21 = "https://schema.beckn.io/{}/v2.1/context.jsonld"


class HealthId(BecknModel):
    system: str
    value: str


class HealthParticipant(JsonLdModel):
    LD_TYPE = "hpa:HealthParticipant"
    LD_CONTEXT = V21.format("HealthParticipant")

    participant_role: str | None = None
    primary_language: str | None = None
    gender: str | None = None
    date_of_birth: date | None = None
    health_ids: list[HealthId] = []


class HealthPerformance(JsonLdModel):
    LD_TYPE = "hpe:HealthPerformance"
    LD_CONTEXT = V21.format("HealthPerformance")

    health_service_type: str | None = None
    appointment_window_start: IsoDateTime | None = None
    appointment_window_end: IsoDateTime | None = None


class HealthContract(JsonLdModel):
    LD_TYPE = "hct:HealthContract"
    LD_CONTEXT = V21.format("HealthContract")

    health_service_type: str | None = None
