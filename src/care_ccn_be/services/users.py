"""The system user that owns rows the plugin creates on behalf of the network."""

from care_ccn_be.settings import plugin_settings


def system_user():
    from care.users.models import User

    return User.objects.filter(username=plugin_settings.BECKN_SYSTEM_USERNAME).first()
