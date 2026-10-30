"""Access control for the two ONIX-facing receivers."""

import hmac
import ipaddress

from rest_framework.permissions import BasePermission

from care_ccn_be.settings import plugin_settings

SECRET_HEADER = "HTTP_X_BECKN_SECRET"


def client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")) or ""


def ip_allowed(ip: str, allowed: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if address in ipaddress.ip_network(str(entry).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


class BecknReceiverPermission(BasePermission):
    """IP allowlist (CIDR) and/or shared secret; with neither configured everything is allowed."""

    message = "Forbidden"

    def has_permission(self, request, view) -> bool:
        allowed = plugin_settings.BECKN_ALLOWED_IPS
        if isinstance(allowed, str):
            allowed = [item for item in allowed.split(",") if item.strip()]
        if allowed and not ip_allowed(client_ip(request), allowed):
            return False
        secret = plugin_settings.BECKN_SHARED_SECRET
        if secret:
            supplied = request.META.get(SECRET_HEADER, "")
            return hmac.compare_digest(supplied.encode(), secret.encode())
        return True
