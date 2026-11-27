"""What a light was last told, until the hub has confirmed it.

After a command the hub's events for that light do not start with the new
state: it may report the old level first, and values on the way to the target.
Applied as they come, those overwrite the value that was just sent, and Home
Assistant shows the light jumping back before it settles.

The bulb therefore keeps the values it sent, per attribute, from the moment
the PATCH goes out. While such a value is pending, a hub value for that
attribute that differs from it is not applied but kept: the last one the hub
reported. The wait for an attribute ends

* when the hub reports the value that was sent (the confirmation): what was
  kept is older than that and is forgotten;
* when the PATCH fails: what was kept is the state of the light;
* ECHO_WINDOW_S after the hub accepted the PATCH. If the hub has not
  confirmed by then, the last value it did report is the state of the light
  (someone dimmed it with a remote right after the command), not an echo.

In the last two cases the kept values are handed to `expired`, which applies
them. The end of the window is a timer made with `call_later(delay, fn, *args)`
(the event loop's), cancelled as soon as nothing of its command is pending.

Attributes that were not part of the command are never held back.

Kept free of Home Assistant imports so the tests can use it directly.
"""
import time

ECHO_WINDOW_S = 2.0

_now = time.monotonic
_NOTHING = object()


class _Sent:
    __slots__ = ("value", "deadline", "kept", "command")

    def __init__(self, value, command):
        self.value = value
        self.deadline = None  # set once the hub has accepted the command
        self.kept = _NOTHING  # the last differing value the hub reported
        self.command = command


class _Command(dict):
    """model attribute -> _Sent, for one PATCH."""
    timer = None
    __hash__ = object.__hash__  # commands are told apart by identity


class PendingCommand:
    def __init__(self, call_later=None, expired=None):
        """call_later(delay, fn, *args) returns a handle with cancel();
        expired(values) gets the hub values that were kept when a wait ends
        without a confirmation. Without them a window only ends at the next
        hub value after it, which then is the one applied."""
        self._sent = {}  # model attribute -> _Sent
        self._call_later = call_later
        self._expired = expired

    def __bool__(self):
        return bool(self._sent)

    def begin(self, values: dict) -> dict:
        """Call when the PATCH goes out, with the values it carries.

        Returns the command, to hand to accepted() or failed(). A newer
        command takes over the attributes it sends; what the hub reported for
        them before is older than the new command and is forgotten."""
        command = _Command()
        for key, value in values.items():
            command[key] = _Sent(value, command)
        replaced = {self._sent[key].command for key in command if key in self._sent}
        self._sent.update(command)
        for older in replaced:
            self._settle(older)
        return command

    def _own(self, command):
        return [(key, sent) for key, sent in command.items() if self._sent.get(key) is sent]

    def _settle(self, command):
        """Nothing of this command is pending any more: its window is over."""
        if command.timer is not None and not self._own(command):
            command.timer.cancel()
            command.timer = None

    def _end(self, command) -> dict:
        """End the wait for what is left of this command. Returns what the
        hub reported for it in the meantime."""
        kept = {}
        for key, sent in self._own(command):
            del self._sent[key]
            if sent.kept is not _NOTHING:
                kept[key] = sent.kept
        self._settle(command)
        return kept

    def accepted(self, command: dict) -> dict:
        """The hub accepted the PATCH: the window starts.

        Returns the values of this command that are still pending. Those are
        the ones to assume in the model; what the hub confirmed in the
        meantime is already there, and what a newer command took over is no
        longer what the light was told."""
        deadline = _now() + ECHO_WINDOW_S
        pending = {}
        for key, sent in self._own(command):
            sent.deadline = deadline
            pending[key] = sent.value
        if pending and self._call_later is not None:
            command.timer = self._call_later(ECHO_WINDOW_S, self._window_ended, command)
        return pending

    def _window_ended(self, command):
        command.timer = None
        kept = self._end(command)
        if kept and self._expired is not None:
            self._expired(kept)

    def failed(self, command: dict) -> dict:
        """The PATCH did not go through: nothing of it is expected back.

        Returns what the hub reported for its attributes in the meantime;
        that is the state of the light."""
        return self._end(command)

    def clear(self):
        """Forget everything, e.g. when the light is removed."""
        for command in {sent.command for sent in self._sent.values()}:
            self._end(command)

    def holds_back(self, key: str, value) -> bool:
        """True when the hub's value for this attribute must not be applied."""
        sent = self._sent.get(key)
        if sent is None:
            return False
        if value == sent.value or (sent.deadline is not None and _now() >= sent.deadline):
            # The confirmation, or a value after the window: either way the
            # newest the hub has, and what it reported before is older.
            del self._sent[key]
            self._settle(sent.command)
            return False
        sent.kept = value
        return True
