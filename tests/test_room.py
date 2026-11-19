import pytest

from room import Room, SIZE_POINTS, Ticket, suggest_size


def test_size_mapping_and_ties():
    assert SIZE_POINTS == {'XS': 1, 'S': 2, 'M': 3, 'L': 5}
    assert suggest_size(['M', 'L', '?']) == 'L'
    assert suggest_size(['?', '?']) is None
    assert suggest_size(['S', 'S', 'L']) == 'S'


def test_expiration_and_handoff():
    now = 0
    room = Room(clock=lambda: now)
    room.join('a', 'Alice')
    room.join('b', 'Bob')
    room.claim('a')
    with pytest.raises(PermissionError):
        room.handoff('b', 'a')
    room.handoff('a', 'b')
    room.merge([Ticket('P-1')], 1, False)
    room.select('P-1')
    room.vote('b', 'S', room.revision)
    now = 30
    room.touch('a')
    assert 'b' in room.participants
    now = 31
    room.touch('a')
    room.join('c', ' Bob ')
    assert 'b' not in room.participants
    assert room.facilitator is None
    assert room.votes == {}
    assert room.participants['c'].name == 'Bob'


def test_round_transitions():
    room = Room()
    room.join('a', 'Alice')
    room.merge([Ticket('P-1', epic_key='E-1'), Ticket('P-2'),
                Ticket('P-3', epic_key='E-1')], 3, False)
    room.set_filter('E-1')
    room.select('P-1')
    first = room.revision
    room.vote('a', 'M', first)
    room.reveal(first)
    room.restart(first)
    assert room.votes == {} and not room.revealed
    with pytest.raises(ValueError):
        room.vote('a', 'M', first)
    room.reveal(room.revision)
    room.finish(room.revision, 'estimated', 'M')
    assert room.statuses['P-1'] == 'estimated'
    assert room.current_key == 'P-3'
    room.finish(room.revision, 'skipped')
    assert room.statuses['P-3'] == 'skipped'
    assert room.current_key is None


def test_filter_and_refresh():
    room = Room()
    a, b, c, d = (Ticket('P-1', epic_key='E-1'), Ticket('P-2'),
                  Ticket('P-3', epic_key='E-1'), Ticket('P-4'))
    room.merge([a, b, c, d], 4, False)
    assert room.visible_tickets() == [a, b, c, d]
    room.set_filter('none')
    assert room.visible_tickets() == [b, d]
    room.set_filter('E-1')
    assert room.visible_tickets() == [a, c]
    room.select('P-1')
    room.reveal(room.revision)
    room.finish(room.revision, 'estimated', 'S')
    room.set_filter('all')
    new = Ticket('P-5')
    room.merge([new, b], 2, False)
    assert room.visible_tickets()[:2] == [new, b]
    assert room.current_key == 'P-3'
    assert 'P-3' in room.tickets and 'P-4' not in room.tickets
    assert room.statuses['P-1'] == 'estimated'
    room.merge([a, new, b], 3, False)
    assert room.statuses['P-1'] == 'estimated'
