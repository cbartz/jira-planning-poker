import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace

SIZE_POINTS = {'XS': 1, 'S': 2, 'M': 3, 'L': 5}
CARDS = (*SIZE_POINTS, '?')


def suggest_size(votes: Iterable[str]) -> str | None:
    counts = Counter(vote for vote in votes if vote in SIZE_POINTS)
    return max(counts, key=lambda size: (counts[size], SIZE_POINTS[size]), default=None)


@dataclass(frozen=True)
class Ticket:
    key: str
    url: str = ''
    summary: str = ''
    description: str = ''
    issue_type: str = ''
    status: str = ''
    priority: str = ''
    priority_icon: str = ''
    epic_key: str | None = None
    epic_summary: str = ''
    labels: tuple[str, ...] = ()
    points: int | float | None = None


@dataclass
class Participant:
    name: str
    last_seen: float


class Room:
    """Store room state; the caller serializes access across requests."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.participants: dict[str, Participant] = {}
        self.facilitator: str | None = None
        self.tickets: dict[str, Ticket] = {}
        self.statuses: dict[str, str] = {}
        self.current_key: str | None = None
        self.epic_filter = 'all'
        self.revision = 0
        self.votes: dict[str, str] = {}
        self.revealed = False
        self.loaded = False
        self.total: int | None = None
        self.truncated = False

    def join(self, participant_id, name):
        self._expire()
        name = name.strip()
        if not 1 <= len(name) <= 32:
            raise ValueError('Enter a name between 1 and 32 characters.')
        if any(other.name.casefold() == name.casefold() and key != participant_id
               for key, other in self.participants.items()):
            raise ValueError('That name is already in use.')
        self.participants[participant_id] = Participant(name, self.clock())
        return name

    def touch(self, participant_id):
        self._expire()
        if participant_id not in self.participants:
            return False
        self.participants[participant_id].last_seen = self.clock()
        return True

    def claim(self, participant_id):
        if not self.touch(participant_id) or self.facilitator not in (None, participant_id):
            raise PermissionError('Someone else is facilitating.')
        self.facilitator = participant_id

    def handoff(self, participant_id, recipient_id):
        if not self.touch(participant_id) or self.facilitator != participant_id:
            raise PermissionError('Only the facilitator can hand off the role.')
        if recipient_id not in self.participants:
            raise ValueError('Choose an active participant.')
        self.facilitator = recipient_id

    def merge(self, tickets, total, truncated):
        incoming = {ticket.key: ticket for ticket in tickets}
        # Preserve Jira ordering for matching issues; keep the active issue usable
        # even if it no longer matches the configured query.
        if self.current_key is not None and self.current_key not in incoming:
            incoming[self.current_key] = self.tickets[self.current_key]
        self.tickets = incoming
        self.statuses = {key: status for key, status in self.statuses.items()
                         if status != 'pending' or key in incoming}
        for key in incoming:
            self.statuses.setdefault(key, 'pending')
        if self.epic_filter not in self.epic_options():
            self.epic_filter = 'all'
        self.loaded, self.total, self.truncated = True, total, truncated

    def epic_options(self):
        counts = {'all': ('All', 0), 'none': ('No epic', 0)}
        for ticket in self.tickets.values():
            pending = self.statuses[ticket.key] == 'pending'
            counts['all'] = ('All', counts['all'][1] + pending)
            key = ticket.epic_key or 'none'
            label = 'No epic' if key == 'none' else f'{key} — {ticket.epic_summary}'.rstrip(' —')
            counts[key] = (label, counts.get(key, ('', 0))[1] + pending)
        return counts

    def set_filter(self, epic):
        if epic not in self.epic_options():
            raise ValueError('Choose an epic from the ticket snapshot.')
        self.epic_filter = epic

    def select(self, key):
        if self.statuses.get(key) != 'pending' or key not in self.tickets:
            raise ValueError('Choose a pending ticket.')
        if key != self.current_key:
            self._open(key)

    def vote(self, participant_id, size, revision):
        self.check_round(revision)
        if self.revealed or size not in CARDS or participant_id not in self.participants:
            raise ValueError('This vote cannot be accepted.')
        self.votes[participant_id] = size

    def reveal(self, revision):
        self.check_round(revision)
        self.revealed = True

    def restart(self, revision):
        self.check_round(revision)
        self._open(self.current_key)

    def check_round(self, revision):
        if self.current_key is None or self.revision != revision:
            raise ValueError('The round has changed. Try again on the current round.')

    def finish(self, revision, status, size=None):
        self.check_round(revision)
        if status == 'estimated' and self.revealed and size in SIZE_POINTS:
            ticket = self.tickets[self.current_key]
            labels = tuple(label for label in ticket.labels
                           if label not in SIZE_POINTS and label != 'grooming')
            self.tickets[ticket.key] = replace(ticket, labels=(*labels, size), points=SIZE_POINTS[size])
        elif status != 'skipped':
            raise ValueError('Reveal votes and choose a final size first.')
        self.statuses[self.current_key] = status
        next_key = next((ticket.key for ticket in self.visible_tickets()
                         if self.statuses[ticket.key] == 'pending'), None)
        self._open(next_key)

    def visible_tickets(self):
        return [ticket for ticket in self.tickets.values()
                if self.epic_filter in ('all', ticket.epic_key or 'none')]

    def _open(self, key):
        self.current_key = key
        self.revision += 1
        self.votes.clear()
        self.revealed = False

    def _expire(self):
        cutoff = self.clock() - 30
        for key, participant in list(self.participants.items()):
            if participant.last_seen < cutoff:
                del self.participants[key]
                self.votes.pop(key, None)
                if self.facilitator == key:
                    self.facilitator = None
