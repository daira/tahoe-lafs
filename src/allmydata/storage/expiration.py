
import time
from types import NoneType

from allmydata.util.assertutil import precondition
from allmydata.util import time_format
from allmydata.web.common import abbreviate_time


class ExpirationPolicy(object):
    def __init__(self, enabled=False, mode="age", override_lease_duration=None,
                 cutoff_date=None):
        precondition(isinstance(enabled, bool), enabled=enabled)
        precondition(mode in ("age", "cutoff-date"),
                     "GC mode %r must be 'age' or 'cutoff-date'" % (mode,))
        precondition(isinstance(override_lease_duration, (int, NoneType)),
                     override_lease_duration=override_lease_duration)
        precondition(isinstance(cutoff_date, int) or (mode != "cutoff-date" and cutoff_date is None),
                     cutoff_date=cutoff_date)

        self._enabled = enabled
        self._mode = mode
        self._override_lease_duration = override_lease_duration
        self._cutoff_date = cutoff_date

    def should_expire(self, current_time, renewal_time, expiration_time):
        # XXX should reexpress this as an SQL DELETE that deletes all expired shares at once.
        if not self._enabled:
            return False

        if self._mode == "age":
            if self._override_lease_duration is None:
                expiry_time = expiration_time  # from lease
            else:
                expiry_time = renewal_time + self._override_lease_duration
        else:
            expiry_time = self._cutoff_date

        return current_time >= expiry_time

    def get_parameters(self):
        """
        Return the parameters as represented in the "configured-expiration-mode" field
        of a history entry.
        """
        return (self._mode,
                self._override_lease_duration,
                self._cutoff_date,
                self._enabled and ("mutable", "immutable") or ())

    def is_enabled(self):
        return self._enabled

    def describe_enabled(self):
        if self.is_enabled():
            return "Enabled: expired leases will be removed"
        else:
            return "Disabled: scan-only mode, no leases will be removed"

    def describe_expiration(self):
        if self._mode == "age":
            if self._override_lease_duration is None:
                return ("Leases will expire naturally, probably 31 days after "
                        "creation or renewal.")
            else:
                return ("Leases created or last renewed more than %s ago "
                        "will be considered expired."
                        % abbreviate_time(self._override_lease_duration))
        else:
            localizedutcdate = time.strftime("%d-%b-%Y", time.gmtime(self._cutoff_date))
            isoutcdate = time_format.iso_utc_date(self._cutoff_date)
            return ("Leases created or last renewed before %s (%s) UTC "
                    "will be considered expired." % (isoutcdate, localizedutcdate))
