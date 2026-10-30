from django.db import models


class Side(models.TextChoices):
    BAP = "bap"
    BPP = "bpp"


class Direction(models.TextChoices):
    IN = "in"
    OUT = "out"


class Delivery(models.TextChoices):
    QUEUED = "queued"
    ACKED = "acked"
    NACKED = "nacked"
    FAILED = "failed"
    SKIPPED = "skipped"


class Processing(models.TextChoices):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class TxnStatus:
    """Open set: QUEUED, <ACTION>, ON_<ACTION> (= action.upper()), NACK, ERROR."""

    QUEUED = "QUEUED"
    NACK = "NACK"
    ERROR = "ERROR"
