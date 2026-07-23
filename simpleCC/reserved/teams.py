from dataclasses import dataclass


@dataclass(frozen=True)
class TeamMember:
    name: str
    role: str = "worker"


class DisabledTeamCoordinator:
    enabled = False

    def start(self, *_args, **_kwargs):
        return "Agent teams are reserved for a future release and are disabled."

    def send(self, *_args, **_kwargs):
        return "Agent teams are reserved for a future release and are disabled."
