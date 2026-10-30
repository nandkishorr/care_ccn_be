"""The seven Beckn actions and their callbacks."""

from dataclasses import dataclass

DISCOVER = "discover"
SELECT = "select"
INIT = "init"
CONFIRM = "confirm"
STATUS = "status"
UPDATE = "update"
CANCEL = "cancel"

ACTIONS = (DISCOVER, SELECT, INIT, CONFIRM, STATUS, UPDATE, CANCEL)

CALLBACK_PREFIX = "on_"


def callback_of(action: str) -> str:
    return action if action.startswith(CALLBACK_PREFIX) else f"{CALLBACK_PREFIX}{action}"


def is_callback(action: str) -> bool:
    return action.startswith(CALLBACK_PREFIX)


def request_of(callback_action: str) -> str:
    return callback_action.removeprefix(CALLBACK_PREFIX)


@dataclass(frozen=True)
class ActionSpec:
    action: str
    expects_callback: bool = True

    @property
    def callback(self) -> str:
        return callback_of(self.action)


SPECS = {action: ActionSpec(action) for action in ACTIONS}

CONTRACT_DRAFT = "DRAFT"
CONTRACT_ACTIVE = "ACTIVE"
CONTRACT_CANCELLED = "CANCELLED"
CONTRACT_COMPLETED = "COMPLETED"
