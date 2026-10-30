import pkgutil
from importlib import import_module
from operator import itemgetter

from django.apps import apps

from care_ccn_be import flows
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.settings import plugin_settings


def import_optional(path: str) -> None:
    try:
        import_module(path)
    except ModuleNotFoundError as exc:
        if exc.name != path:
            raise


class FlowRegistry:
    _flows: dict[str, type[BecknFlow]] = {}
    _aliases: dict[str, str] = {}

    @classmethod
    def register(cls, flow: type[BecknFlow]) -> type[BecknFlow]:
        cls._flows[flow.name] = flow
        cls._aliases.update(dict.fromkeys(flow.aliases, flow.name))
        return flow

    @classmethod
    def get(cls, name: str) -> type[BecknFlow]:
        return cls._flows[cls._aliases.get(name, name)]

    @classmethod
    def enabled(cls) -> list[type[BecknFlow]]:
        if apps.is_installed("care.beckn"):  # core Beckn still active: the plugin stays passive
            return []
        return [cls._flows[name] for name in plugin_settings.BECKN_ENABLED_FLOWS if name in cls._flows]

    @classmethod
    def is_enabled(cls, name: str) -> bool:
        flow = cls._flows.get(cls._aliases.get(name, name))
        return flow is not None and flow in cls.enabled()

    @classmethod
    def match(cls, request) -> type[BecknFlow] | None:
        score, flow = max(((f.match(request), f) for f in cls.enabled()), key=itemgetter(0), default=(0, None))
        return flow if score > 0 else None

    @classmethod
    def autodiscover(cls) -> None:
        for pkg in pkgutil.iter_modules(flows.__path__):
            if not pkg.ispkg:
                continue
            import_module(f"care_ccn_be.flows.{pkg.name}.flow")
            import_optional(f"care_ccn_be.flows.{pkg.name}.tasks")  # Celery tasks, also for disabled flows
        for flow in cls.enabled():  # disabled flows don't react to Care events
            for base in flow.__mro__:  # a subclass flow keeps its parent's receivers
                if issubclass(base, BecknFlow) and base is not BecknFlow:
                    import_optional(f"{base.__module__.rpartition('.')[0]}.signals")
