"""What a light was last told, until the hub has confirmed it.

After a command the hub's events for that light do not start with the new
state: it may report the old level first, and values on the way to the target.
Applied as they come, those overwrite the value that was just sent, and Home
Assistant shows the light jumping back before it settles.

The bulb therefore keeps the values it sent, per attribute, from before the
PATCH goes out. While such a value is pending, a hub value for that attribute
that differs from it is not applied. The wait for an attribute ends

* when the hub reports the value that was sent (the confirmation),
* when the PATCH fails, or
* ECHO_WINDOW_S after the hub accepted the PATCH: whatever the hub says by
  then is the state of the light, not an echo.

Attributes that were not part of the command are never held back.

Kept free of Home Assistant imports so the tests can use it directly.
"""
import time

ECHO_WINDOW_S = 2.0

_now = time.monotonic


class _Sent:
    __slots__ = ("value", "deadline")

    def __init__(self, value):
        self.value = value
        self.deadline = None  # set once the hub has accepted the command


class PendingCommand:
    def __init__(self):
        self._sent = {}  # model attribute -> _Sent

    def __bool__(self):
        return bool(self._sent)

    def begin(self, values: dict) -> dict:
        """Call before the PATCH is sent, with the values it carries.

        Returns the command, to hand to accepted() or failed(). A newer
        command takes over the attributes it sends."""
        command = {key: _Sent(value) for key, value in values.items()}
        self._sent.update(command)
        return command

    def _own(self, command):
        return [(key, sent) for key, sent in command.items() if self._sent.get(key) is sent]

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
        return pending

    def failed(self, command: dict):
        """The PATCH did not go through: nothing of it is expected back."""
        for key, _ in self._own(command):
            del self._sent[key]

    def holds_back(self, key: str, value) -> bool:
        """True when the hub's value for this attribute must not be applied."""
        sent = self._sent.get(key)
        if sent is None:
            return False
        if value == sent.value or (sent.deadline is not None and _now() >= sent.deadline):
            del self._sent[key]
            return False
        return True
