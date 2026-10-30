"""Cross-flow reactions. Flows never import each other; they emit and receive these signals."""

from django.dispatch import Signal

# sent by the appointment flow when a booking is fulfilled: kwargs ``transaction`` and ``booking``
booking_fulfilled = Signal()
