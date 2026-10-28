"""Time-based pet behavior, independent of the windowing system."""
import math
import random


class DragMotion:
    """A small damped response to the actual pointer movement, in logical pixels."""
    MAX_ANGLE = 9.0
    LIFT = 8.0

    def __init__(self):
        self.angle = 0.0
        self.target_angle = 0.0
        self.lift = 0.0
        self.held = False
        self.last_time = None
        self.last_pointer = None

    def begin(self, now, pointer_x):
        self.held = True
        self.last_time = now
        self.last_pointer = pointer_x
        self.last_time_pointer = now
        self.target_angle = 0.0

    def move(self, now, pointer_x):
        self.advance(now)
        dt = now - self.last_time_pointer
        velocity = (pointer_x - self.last_pointer) / max(dt, .016)
        self.target_angle = max(-self.MAX_ANGLE, min(self.MAX_ANGLE, -velocity * .025))
        self.last_pointer = pointer_x
        self.last_time_pointer = now

    def end(self, now):
        self.advance(now)
        self.held = False
        self.target_angle = 0.0
        self.last_pointer = None

    def advance(self, now):
        if self.last_time is None:
            self.last_time = now
            return
        dt = max(0.0, min(now - self.last_time, .1))
        self.last_time = now
        response = 1.0 - math.exp(-8.0 * dt)
        self.angle += (self.target_angle - self.angle) * response
        self.lift += ((self.LIFT if self.held else 0.0) - self.lift) * response
        self.target_angle *= math.exp(-3.0 * dt)


class PetBehavior:
    REACTION_SECONDS = 2.8
    WAVE_FRAME_SECONDS = 0.24
    BLINK_SECONDS = 0.18
    LANDING_SECONDS = 0.6
    LANDING_DISTANCE = 3.0  # logical pixels, downward then back to rest
    PATROL_SPEED = 35.0  # logical pixels per second

    def __init__(self, now, rng=None, patrol=True):
        self.rng = rng or random.Random()
        self.last_tick = now
        self.blink_at = now + self.rng.uniform(3.0, 6.0)
        self.blink_until = 0.0
        self.reaction_until = 0.0
        self.dragging = False
        self.drag_pose = 'idle'
        self.patrol = patrol
        self.walk_until = 0.0
        self.walk_at = now + self.rng.uniform(8.0, 13.0)
        self.direction = 1
        self.message = ''
        self.reaction_pose = 'wave'
        self.idle_pose = 'idle'
        self.idle_pose_started = now
        self.idle_pose_until = 0.0
        self.idle_pose_at = now + self.rng.uniform(18.0, 30.0)
        self.landed_at = None
        self.activity = 'idle'

    def set_activity(self, activity, now):
        self.activity = activity
        if activity == 'work':
            self.idle_pose_until = 0.0
        elif activity == 'celebrate':
            self.idle_pose = 'happy'
            self.idle_pose_until = now + 3.0
            self.idle_pose_at = self.idle_pose_until + self.rng.uniform(24.0, 40.0)
            self.activity = 'idle'

    def react(self, now, message, pose='wave'):
        self.message = message
        self.reaction_pose = pose
        self.reaction_until = now + self.REACTION_SECONDS
        self.walk_until = 0.0
        self.walk_at = self.reaction_until + self.rng.uniform(5.0, 9.0)

    def begin_drag(self, now):
        self.drag_pose = self.pose(now)
        if self.drag_pose == 'blink':
            self.drag_pose = 'idle'
        self.dragging = True
        self.landed_at = None
        self.walk_until = 0.0

    def end_drag(self, now):
        self.dragging = False
        self.landed_at = now
        self.last_tick = now
        self.walk_at = now + self.rng.uniform(7.0, 12.0)

    def landing_offset(self, now):
        """Small vertical settle after release, without deforming the tail."""
        if self.dragging or self.landed_at is None:
            return 0.0
        elapsed = now - self.landed_at
        if not 0.0 <= elapsed < self.LANDING_SECONDS:
            return 0.0
        return self.LANDING_DISTANCE * math.sin(math.pi * elapsed / self.LANDING_SECONDS)

    def set_patrol(self, enabled, now):
        self.patrol = enabled
        self.walk_until = 0.0
        self.walk_at = now + self.rng.uniform(4.0, 8.0)
        self.last_tick = now

    def tick(self, now):
        # Bound one integration step so an overloaded or suspended GUI cannot
        # teleport the window when it resumes.
        dt = max(0.0, min(now - self.last_tick, 0.1))
        self.last_tick = now
        if now >= self.blink_at:
            self.blink_until = now + self.BLINK_SECONDS
            self.blink_at = now + self.rng.uniform(3.0, 7.0)
        if (not self.dragging and self.activity != 'work' and now >= self.reaction_until
                and now >= self.idle_pose_at):
            self.idle_pose = self.rng.choice(('tea', 'wave'))
            self.idle_pose_started = now
            self.idle_pose_until = now + self.rng.uniform(2.5, 4.0)
            self.idle_pose_at = self.idle_pose_until + self.rng.uniform(24.0, 40.0)
        if not self.patrol or self.dragging or now < self.reaction_until:
            return 0.0
        if self.walk_until and now >= self.walk_until:
            self.walk_until = 0.0
            self.walk_at = now + self.rng.uniform(8.0, 16.0)
        if not self.walk_until and now >= self.walk_at:
            self.direction = self.rng.choice((-1, 1))
            self.walk_until = now + self.rng.uniform(2.0, 4.0)
        return self.direction * self.PATROL_SPEED * dt if self.walk_until else 0.0

    def at_edge(self):
        self.direction *= -1

    def pose(self, now):
        if self.dragging:
            return self.drag_pose
        if now < self.reaction_until:
            if self.reaction_pose != 'wave':
                return self.reaction_pose
            elapsed = now - (self.reaction_until - self.REACTION_SECONDS)
            return 'wave' if int(elapsed / self.WAVE_FRAME_SECONDS) % 2 == 0 else 'wave2'
        if self.activity == 'work':
            return 'work'
        # A blink is based on the idle illustration. Never replace an active
        # tea or wave pose with it, which would make the held prop disappear.
        if now < self.idle_pose_until:
            if self.idle_pose == 'wave':
                elapsed = now - self.idle_pose_started
                return 'wave' if int(elapsed / self.WAVE_FRAME_SECONDS) % 2 == 0 else 'wave2'
            return self.idle_pose
        if now < self.blink_until:
            return 'blink'
        return 'idle'
