"""Schedule optional white-dragon idles without interrupting active actions."""
import random


class QIdleDirector:
    """Play catalogue idles only after the manager confirms the pet is free.

    ``available`` must be false when hidden, dragged, conversing with the other
    pet, displaying a user-facing message, or otherwise busy. The director also
    checks the actual player state and never replaces a running animation.
    """
    CANDIDATES = ('idle2', '01', '04', '08', '14', '17', '20', '25')
    MIN_IDLE_SECONDS = 10.0
    FIRST_DELAY = (25.0, 45.0)
    ACTION_DELAY = (45.0, 85.0)
    RETRY_DELAY = (8.0, 15.0)

    def __init__(self, now, rng=None):
        self.rng = rng or random.Random()
        self.next_action = now + self.rng.uniform(*self.FIRST_DELAY)
        self.last_tick = now
        self.idle_since = None
        self.last_action = None

    def tick(self, now, available, player):
        """Return the ID started, or None. Call every second or so."""
        if now < self.last_tick:
            self.next_action = now + self.rng.uniform(*self.FIRST_DELAY)
            self.idle_since = None
        self.last_tick = now
        if not available or player.current != 'idle':
            self.idle_since = None
            return None
        if self.idle_since is None:
            self.idle_since = now
        if now < self.next_action or now - self.idle_since < self.MIN_IDLE_SECONDS:
            return None
        choices = [ident for ident in self.CANDIDATES if ident in player.entries]
        if not choices:
            self.next_action = now + self.rng.uniform(*self.ACTION_DELAY)
            return None
        if len(choices) > 1 and self.last_action in choices:
            choices.remove(self.last_action)
        chosen = self.rng.choice(choices)
        if not player.play(chosen):
            self.next_action = now + self.rng.uniform(*self.RETRY_DELAY)
            return None
        self.last_action = chosen
        self.idle_since = None
        self.next_action = now + self.rng.uniform(*self.ACTION_DELAY)
        return chosen


class QWorkDirector:
    """Occasional visual variety during a task; animation is not tool telemetry."""
    CANDIDATES = ('work03', 'work04', 'work05')
    ACTION_DELAY = (30.0, 60.0)
    RETRY_DELAY = (8.0, 15.0)

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.next_action = None
        self.last_action = None

    def start(self, now):
        self.next_action = now + self.rng.uniform(*self.ACTION_DELAY)
        self.last_action = None

    def stop(self):
        self.next_action = None

    def tick(self, now, available, player):
        if self.next_action is None or now < self.next_action or not available or player.current != 'work02':
            return None
        choices = [ident for ident in self.CANDIDATES if ident in player.entries]
        if len(choices) > 1 and self.last_action in choices:
            choices.remove(self.last_action)
        if not choices:
            self.next_action = now + self.rng.uniform(*self.ACTION_DELAY)
            return None
        chosen = self.rng.choice(choices)
        if not player.play(chosen):
            self.next_action = now + self.rng.uniform(*self.RETRY_DELAY)
            return None
        self.last_action = chosen
        self.next_action = now + self.rng.uniform(*self.ACTION_DELAY)
        return chosen
