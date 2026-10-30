"""Core Beckn v2 objects. A contract is assembled from small ``Has*`` parts."""

from care_ccn_be.protocol.schema.base import Attributes, BecknModel


class Descriptor(BecknModel):
    code: str | None = None
    name: str | None = None
    short_desc: str | None = None


class Status(BecknModel):
    code: str | None = None
    descriptor: Descriptor | None = None


class Quantity(BecknModel):
    count: int | None = None


class Price(BecknModel):
    currency: str | None = None
    value: str | None = None


class Resource(BecknModel):
    id: str
    quantity: Quantity | None = None
    resource_attributes: Attributes | None = None


class Offer(BecknModel):
    id: str
    resource_ids: list[str] = []
    offer_attributes: Attributes | None = None


class Commitment(BecknModel):
    id: str | None = None
    status: Status | None = None
    resources: list[Resource] = []
    offer: Offer | None = None
    commitment_attributes: Attributes | None = None


class Participant(BecknModel):
    id: str | None = None
    descriptor: Descriptor | None = None
    participant_attributes: Attributes | None = None


class Performance(BecknModel):
    id: str | None = None
    performance_attributes: Attributes | None = None


class Consideration(BecknModel):
    id: str | None = None
    price: Price | None = None
    consideration_attributes: Attributes | None = None


class HasCommitments(BecknModel):
    commitments: list[Commitment] = []


class HasParticipants(BecknModel):
    participants: list[Participant] = []

    def participant(self, role: str) -> Participant | None:
        for item in self.participants:
            if getattr(item.participant_attributes, "participant_role", None) == role:
                return item
        return None


class HasPerformance(BecknModel):
    performance: list[Performance] = []


class HasConsideration(BecknModel):
    consideration: list[Consideration] = []


class Contract(HasCommitments, HasParticipants, HasPerformance):
    id: str | None = None
    descriptor: Descriptor | None = None
    status: Status | None = None
    contract_attributes: Attributes | None = None
