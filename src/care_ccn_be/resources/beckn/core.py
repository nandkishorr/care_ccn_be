"""Core Beckn v2 objects. A contract is assembled from small ``Has*`` parts."""

from care_ccn_be.resources.beckn.base import Attributes, BecknModel


class Document(BecknModel):
    """A ``Descriptor.docs[]`` entry; DHP examples identify it by ``label`` or ``id``, so both are sent."""

    id: str | None = None
    label: str | None = None
    name: str | None = None
    url: str | None = None
    mime_type: str | None = None
    standard: str | None = None

    @property
    def ref(self) -> str | None:
        return self.label or self.id


class Descriptor(BecknModel):
    code: str | None = None
    name: str | None = None
    short_desc: str | None = None
    docs: list[Document] | None = None


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
    descriptor: Descriptor | None = None
    quantity: Quantity | None = None
    resource_attributes: Attributes | None = None


class Offer(BecknModel):
    id: str
    descriptor: Descriptor | None = None
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


class Provider(BecknModel):
    id: str
    descriptor: Descriptor | None = None


class Catalog(BecknModel):
    """One entry of ``message.catalogs`` (``on_discover`` and ``catalog/publish``)."""

    id: str
    descriptor: Descriptor | None = None
    provider: Provider | None = None
    bpp_id: str | None = None
    bpp_uri: str | None = None
    bap_id: str | None = None
    bap_uri: str | None = None
    resources: list[Resource] = []
    offers: list[Offer] = []
