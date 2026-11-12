# care_ccn_be

Beckn (NFH / DHP) BAP and BPP plugin for [ohcnetwork/care](https://github.com/ohcnetwork/care).

It replaces Care core's `care/beckn` app: a pure protocol layer, a flow framework (BAP and BPP APIs built on CARE's
EMR viewsets, one mixin per Beckn action, Celery delivery) and two flows, `referral` and `appointment`. Transactions
and messages live in plugin tables instead of Redis.

- [Architecture](docs/architecture.md)
- [Frontend API](docs/fe_api.md)

## Local development

1. Clone the plugin next to Care and add it to `plug_config.py`:

```python
from plugs.manager import PlugManager
from plugs.plug import Plug

care_ccn_be_plug = Plug(
    name="care_ccn_be",  # name of the django app in the plugin
    package_name="/app/care_ccn_be",  # /app/ + plugin folder name
    version="",  # keep it empty for local development
    configs={},  # optional overrides of the BECKN_* settings
)
plugs = [care_ccn_be_plug]
manager = PlugManager(plugs)
```

2. In `plugs/manager.py`, install in editable mode (`pip install -e`), then `make re-build && make up`.

> [!IMPORTANT]
> Do not push these Care changes in a PR. They are only for local development.

Care mounts the plugin at `/api/care_ccn_be/`; `GET /api/care_ccn_be/health` returns `OK`.

## Production

```python
care_ccn_be_plug = Plug(
    name="care_ccn_be",
    package_name="git+https://github.com/ohcnetwork/care_ccn_be.git",
    version="@master",
    configs={},
)
```

[Extended docs on plug installation](https://care-be-docs.ohc.network/pluggable-apps/configuration.html)

## Settings

Every setting keeps its `BECKN_*` name. Lookup order: `PLUGIN_CONFIGS["care_ccn_be"]`, then the environment variable,
then the default. See `DEFAULTS` in `src/care_ccn_be/settings.py` for the full list.

BAP/BPP defaults live in `src/care_ccn_be/adapters/__init__.py` and are merged into that list.
`adapters/bap.py` handles BAP identity and caller configuration; `adapters/bpp.py` handles BPP identity,
caller and catalog publishing configuration. The referral coordination-centre settings (`BECKN_CC_BPP_*`)
live in `flows/referral/__init__.py` alongside referral defaults. `flows/referral/config.py` resolves the remote
coordination-centre endpoint, not Care's own BPP. Existing setting keys and lookup precedence remain unchanged.

Each flow has one viewset in `flows/<flow>/api.py` containing both BAP and BPP hooks. Existing
`bap/<flow>/` and `bpp/<flow>/` routes bind the adapter per instance; BAP actions are not exposed on BPP routes.
Contract builders live in
`resources/builders/<flow>.py`; catalog builders live in `resources/catalog/<flow>.py` when the flow supports catalogs.

Shared constants and context builders live in `constants.py` and `context.py`. Flow link models, signal receivers,
and Celery tasks live in `models/<flow>.py`, `signals/<flow>.py`, and `tasks/<flow>.py` respectively.
Shared delivery and housekeeping tasks remain available from `care_ccn_be.tasks`. Flow tasks are registered even
when disabled; signal receivers are loaded only for enabled flows. Existing task names and database tables are unchanged.

## Tests

Tests live in `src/care_ccn_be/tests/` and run inside a Care checkout that has the plugin installed:

```bash
python manage.py test care_ccn_be --keepdb --parallel --shuffle
```

This project was generated from the [ohcnetwork/care-plugin-cookiecutter](https://github.com/ohcnetwork/care-plugin-cookiecutter).
