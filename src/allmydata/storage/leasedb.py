
"""
This file manages the lease database, and runs the crawler which recovers
from lost-db conditions (both initial boot, DB failures, and shares being
added/removed out-of-band) by adding temporary 'starter leases'. It queries
the storage backend to enumerate existing shares (for each one it needs SI,
shnum, and used space). It can also instruct the storage backend to delete
a share that has expired.
"""

import os, time, simplejson

from twisted.python.filepath import FilePath

from allmydata.util.assertutil import _assert
from allmydata.util import dbutil
from allmydata.util.fileutil import get_used_space
from allmydata.storage.crawler import ShareCrawler
from allmydata.storage.common import si_a2b, si_b2a


class BadAccountName(Exception):
    pass


class ShareAlreadyInDatabaseError(Exception):
    def __init__(self, si_s, shnum):
        Exception.__init__(self, si_s, shnum)
        self.si_s = si_s
        self.shnum = shnum

    def __str__(self):
        return "SI=%r shnum=%r is already in `shares` table" % (self.si_s, self.shnum)


class NonExistentShareError(Exception):
    def __init__(self, si_s, shnum):
        Exception.__init__(self, si_s, shnum)
        self.si_s = si_s
        self.shnum = shnum

    def __str__(self):
        return "can't find SI=%r shnum=%r in `shares` table" % (self.si_s, self.shnum)


class NonExistentLeaseError(Exception):
    # FIXME not used
    pass


class LeaseInfo(object):
    def __init__(self, storage_index, shnum, owner_num, renewal_time, expiration_time):
        self.storage_index = storage_index
        self.shnum = shnum
        self.owner_num = owner_num
        self.renewal_time = renewal_time
        self.expiration_time = expiration_time


def int_or_none(s):
    if s is None:
        return s
    return int(s)


SHARETYPE_IMMUTABLE  = 0
SHARETYPE_MUTABLE    = 1
SHARETYPE_CORRUPTED  = 2
SHARETYPE_UNKNOWN    = 3

SHARETYPES = { SHARETYPE_IMMUTABLE: 'immutable',
               SHARETYPE_MUTABLE:   'mutable',
               SHARETYPE_CORRUPTED: 'corrupted',
               SHARETYPE_UNKNOWN:   'unknown' }

STATE_COMING = 0
STATE_STABLE = 1
STATE_GOING  = 2


LEASE_SCHEMA_V1 = """
CREATE TABLE `version`
(
 version INTEGER -- contains one row, set to 1
);

CREATE TABLE `shares`
(
 `storage_index` VARCHAR(26) not null,
 `shnum` INTEGER not null,
 `prefix` VARCHAR(2) not null,
 `backend_key` VARCHAR,         -- not used by current backends; NULL means '$prefix/$storage_index/$shnum'
 `used_space` INTEGER not null,
 `sharetype` INTEGER not null,  -- SHARETYPE_*
 `state` INTEGER not null,      -- STATE_*
 PRIMARY KEY (`storage_index`, `shnum`)
);

CREATE INDEX `prefix` ON `shares` (`prefix`);
-- CREATE UNIQUE INDEX `share_id` ON `shares` (`storage_index`,`shnum`);

CREATE TABLE `leases`
(
 `id` INTEGER PRIMARY KEY AUTOINCREMENT,
 `storage_index` VARCHAR(26) not null,
 `shnum` INTEGER not null,
 `account_id` INTEGER not null,
 `renewal_time` INTEGER not null, -- duration is implicit: expiration-renewal
 `expiration_time` INTEGER not null, -- seconds since epoch
 FOREIGN KEY (`storage_index`, `shnum`) REFERENCES `shares` (`storage_index`, `shnum`),
 FOREIGN KEY (`account_id`) REFERENCES `accounts` (`id`)
);

CREATE INDEX `account_id` ON `leases` (`account_id`);
CREATE INDEX `expiration_time` ON `leases` (`expiration_time`);

CREATE TABLE accounts
(
 `id` INTEGER PRIMARY KEY AUTOINCREMENT,
 `pubkey_vs` VARCHAR(52),
 `creation_time` INTEGER
);
CREATE UNIQUE INDEX `pubkey_vs` ON `accounts` (`pubkey_vs`);

CREATE TABLE account_attributes
(
 `id` INTEGER PRIMARY KEY AUTOINCREMENT,
 `account_id` INTEGER,
 `name` VARCHAR(20),
 `value` VARCHAR(20) -- actually anything: usually string, unicode, integer
);
CREATE UNIQUE INDEX `account_attr` ON `account_attributes` (`account_id`, `name`);

INSERT INTO `accounts` VALUES (0, "anonymous", 0);
INSERT INTO `accounts` VALUES (1, "starter", 0);

CREATE TABLE crawler_history
(
 `cycle` INTEGER,
 `json` TEXT
);
CREATE UNIQUE INDEX `cycle` ON `crawler_history` (`cycle`);
"""

DAY = 24*60*60
MONTH = 30*DAY

class LeaseDB:
    ANONYMOUS_ACCOUNTID = 0
    STARTER_LEASE_ACCOUNTID = 1
    STARTER_LEASE_DURATION = 2*MONTH

    # for all methods that start by setting self._dirty=True, be sure to call
    # .commit() when you're done

    def __init__(self, dbfile):
        (self._sqlite,
         self._db) = dbutil.get_db(dbfile, create_version=(LEASE_SCHEMA_V1, 1))
        self._cursor = self._db.cursor()
        self._dirty = False
        self.debug = False
        self.retained_history_entries = 10

    # share management

    def get_shares_for_prefix(self, prefix):
        """
        Returns a dict mapping (si_s, shnum) pairs to (used_space, sharetype) pairs.
        """
        self._cursor.execute("SELECT `storage_index`,`shnum`, `used_space`, `sharetype`"
                             " FROM `shares`"
                             " WHERE `prefix` == ?",
                             (prefix,))
        db_shares = dict([((str(si_s), int(shnum)), (int(used_space), int(sharetype)))
                          for (si_s, shnum, used_space, sharetype) in self._cursor.fetchall()])
        return db_shares

    def add_new_share(self, storage_index, shnum, used_space, sharetype):
        si_s = si_b2a(storage_index)
        prefix = si_s[:2]
        if self.debug: print "ADD_NEW_SHARE", prefix, si_s, shnum, used_space, sharetype
        self._dirty = True
        try:
            self._cursor.execute("INSERT INTO `shares`"
                                 " VALUES (?,?,?,?,?,?,?)",
                                 (si_s, shnum, prefix, None, used_space, sharetype, STATE_COMING))
        except dbutil.IntegrityError:
            # XXX: when test_repairer.Repairer.test_repair_from_deletion_of_1
            # runs, it deletes the share from disk, then the repairer replaces it
            # (in the same place). The add_new_share() code needs to tolerate
            # surprises like this: the share might have been manually deleted,
            # and the crawler may not have noticed it yet, so test for an existing
            # entry and use it if present (and check the code paths carefully to
            # make sure that doesn't get too weird).
            # FIXME: check that the IntegrityError is really due to the share already existing.
            raise ShareAlreadyInDatabaseError(si_s, shnum)

    def add_starter_lease(self, storage_index, shnum):
        si_s = si_b2a(storage_index)
        if self.debug: print "ADD_STARTER_LEASE", si_s, shnum
        self._dirty = True
        renewal_time = time.time()
        self._cursor.execute("INSERT INTO `leases`"
                             " VALUES (?,?,?,?,?,?)",
                             (None, si_s, shnum, self.STARTER_LEASE_ACCOUNTID,
                              int(renewal_time), int(renewal_time + self.STARTER_LEASE_DURATION)))

    def mark_share_as_stable(self, storage_index, shnum, used_space, backend_key=None):
        """
        Call this method after adding a share to backend storage.
        """
        si_s = si_b2a(storage_index)
        if self.debug: print "MARK_SHARE_AS_STABLE", si_s, shnum, used_space
        self._dirty = True
        self._cursor.execute("UPDATE `shares` SET `state`=?, `used_space`=?, `backend_key`=?"
                             " WHERE `storage_index`=? AND `shnum`=? AND `state`!=?",
                             (STATE_STABLE, used_space, backend_key, si_s, shnum, STATE_GOING))
        if self._cursor.rowcount < 1:
            raise NonExistentShareError(si_s, shnum)

    def mark_share_as_going(self, storage_index, shnum):
        """
        Call this method and commit before deleting a share from backend storage,
        then call remove_deleted_share.
        """
        si_s = si_b2a(storage_index)
        if self.debug: print "MARK_SHARE_AS_GOING", si_s, shnum
        self._dirty = True
        self._cursor.execute("UPDATE `shares` SET `state`=?"
                             " WHERE `storage_index`=? AND `shnum`=? AND `state`!=?",
                             (STATE_GOING, si_s, shnum, STATE_COMING))
        if self._cursor.rowcount < 1:
            raise NonExistentShareError(si_s, shnum)

    def remove_deleted_share(self, storage_index, shnum):
        si_s = si_b2a(storage_index)
        if self.debug: print "REMOVE_DELETED_SHARE", si_s, shnum
        self._dirty = True
        # delete leases first to maintain integrity constraint
        self._cursor.execute("DELETE FROM `leases`"
                             " WHERE `storage_index`=? AND `shnum`=?",
                             (si_s, shnum))
        self._cursor.execute("DELETE FROM `shares`"
                             " WHERE `storage_index`=? AND `shnum`=?",
                             (si_s, shnum))

    def change_share_space(self, storage_index, shnum, used_space):
        si_s = si_b2a(storage_index)
        if self.debug: print "CHANGE_SHARE_SPACE", si_s, shnum, used_space
        self._dirty = True
        self._cursor.execute("UPDATE `shares` SET `used_space`=?"
                             " WHERE `storage_index`=? AND `shnum`=?",
                             (used_space, si_s, shnum))
        if self._cursor.rowcount < 1:
            raise NonExistentShareError(si_s, shnum)

    # lease management

    def add_or_renew_leases(self, storage_index, shnum, ownerid,
                            renewal_time, expiration_time):
        """
        shnum=None means renew leases on all shares; do nothing if there are no shares for this storage_index in the `shares` table.

        Raises NonExistentShareError if a specific shnum is given and that share does not exist in the `shares` table.
        """
        si_s = si_b2a(storage_index)
        if self.debug: print "ADD_OR_RENEW_LEASES", si_s, shnum, ownerid, renewal_time, expiration_time
        self._dirty = True
        if shnum is None:
            self._cursor.execute("SELECT `storage_index`, `shnum` FROM `shares`"
                                 " WHERE `storage_index`=?",
                                 (si_s,))
            rows = self._cursor.fetchall()
        else:
            self._cursor.execute("SELECT `storage_index`, `shnum` FROM `shares`"
                                 " WHERE `storage_index`=? AND `shnum`=?",
                                 (si_s, shnum))
            rows = self._cursor.fetchall()
            if not rows:
                raise NonExistentShareError(si_s, shnum)

        for (found_si_s, found_shnum) in rows:
            _assert(si_s == found_si_s, si_s=si_s, found_si_s=found_si_s)
            # XXX can we simplify this by using INSERT OR REPLACE?
            self._cursor.execute("SELECT `id` FROM `leases`"
                                 " WHERE `storage_index`=? AND `shnum`=? AND `account_id`=?",
                                 (si_s, found_shnum, ownerid))
            row = self._cursor.fetchone()
            if row:
                # Note that unlike the pre-LeaseDB code, this allows leases to be backdated.
                # There is currently no way for a client to specify lease duration, and so
                # backdating can only happen in normal operation if there is a timequake on
                # the server and time goes backward by more than 31 days. This needs to be
                # revisited for ticket #1816, which would allow the client to request a lease
                # duration.
                leaseid = row[0]
                self._cursor.execute("UPDATE `leases` SET `renewal_time`=?, `expiration_time`=?"
                                     " WHERE `id`=?",
                                     (renewal_time, expiration_time, leaseid))
            else:
                self._cursor.execute("INSERT INTO `leases` VALUES (?,?,?,?,?,?)",
                                     (None, si_s, shnum, ownerid, renewal_time, expiration_time))

    def get_leases(self, storage_index, ownerid):
        si_s = si_b2a(storage_index)
        self._cursor.execute("SELECT `shnum`, `account_id`, `renewal_time`, `expiration_time` FROM `leases`"
                             " WHERE `storage_index`=? AND `account_id`=?",
                             (si_s, ownerid))
        rows = self._cursor.fetchall()
        def _to_LeaseInfo(row):
            print "row:", row
            (shnum, account_id, renewal_time, expiration_time) = tuple(row)
            return LeaseInfo(storage_index, int(shnum), int(account_id), float(renewal_time), float(expiration_time))
        return map(_to_LeaseInfo, rows)

    def get_lease_ages(self, storage_index, shnum, now):
        si_s = si_b2a(storage_index)
        self._cursor.execute("SELECT `renewal_time` FROM `leases`"
                             " WHERE `storage_index`=? AND `shnum`=?",
                             (si_s, shnum))
        rows = self._cursor.fetchall()
        def _to_age(row):
            return now - float(row[0])
        return map(_to_age, rows)

    def get_unleased_shares(self, limit=None):
        # This would be simpler, but it doesn't work because 'NOT IN' doesn't support multiple columns.
        #query = ("SELECT `storage_index`, `shnum` FROM `shares`"
        #         " WHERE (`storage_index`, `shnum`) NOT IN (SELECT DISTINCT `storage_index`, `shnum` FROM `leases`)")

        # This "negative join" should be equivalent.
        query = ("SELECT DISTINCT s.storage_index, s.shnum, s.sharetype FROM `shares` s LEFT JOIN `leases` l"
                 " ON (s.storage_index = l.storage_index AND s.shnum = l.shnum)"
                 " WHERE l.storage_index IS NULL")

        if limit is None:
            self._cursor.execute(query)
        else:
            self._cursor.execute(query + " LIMIT ?", (limit,))

        rows = self._cursor.fetchall()
        return map(tuple, rows)

    def remove_expired_leases(self, expiration_policy):
        raise NotImplementedError

    # history

    def add_history_entry(self, cycle, entry):
        if self.debug: print "ADD_HISTORY_ENTRY", cycle, entry
        json = simplejson.dumps(entry)
        self._cursor.execute("SELECT `cycle` FROM `crawler_history`")
        rows = self._cursor.fetchall()
        if len(rows) >= self.retained_history_entries:
            first_cycle_to_retain = list(sorted(rows))[-(self.retained_history_entries - 1)][0]
            self._cursor.execute("DELETE FROM `crawler_history` WHERE `cycle` < ?",
                                 (first_cycle_to_retain,))

        self._cursor.execute("INSERT OR REPLACE INTO `crawler_history` VALUES (?,?)",
                             (cycle, json))
        self.commit(always=True)

    def get_history(self):
        self._cursor.execute("SELECT `cycle`,`json` FROM `crawler_history`")
        rows = self._cursor.fetchall()
        decoded = [(row[0], simplejson.loads(row[1])) for row in rows]
        return dict(decoded)

    def get_account_creation_time(self, owner_num):
        self._cursor.execute("SELECT `creation_time` from `accounts`"
                             " WHERE `id`=?",
                             (owner_num,))
        row = self._cursor.fetchone()
        if row:
            return row[0]
        return None

    def get_all_accounts(self):
        self._cursor.execute("SELECT `id`,`pubkey_vs`"
                             " FROM `accounts` ORDER BY `id` ASC")
        return self._cursor.fetchall()

    def commit(self, always=False):
        if self._dirty or always:
            self._db.commit()
            self._dirty = False


class AccountingCrawler(ShareCrawler):
    """
    I perform the following functions:
    - Remove leases that are past their expiration time.
    - Delete objects containing unleased shares.
    - Discover shares that have been manually added to storage.
    - Discover shares that are present when a storage server is upgraded from
      a pre-leasedb version, and give them "starter leases".
    - Recover from a situation where the leasedb is lost or detectably
      corrupted. This is handled in the same way as upgrading.
    - Detect shares that have unexpectedly disappeared from storage.
    """

    slow_start = 300 # don't start crawling for 5 minutes after startup
    minimum_cycle_time = 12*60*60 # not more than twice per day

    def __init__(self, server, statefile, leasedb):
        ShareCrawler.__init__(self, server, statefile)
        self._leasedb = leasedb

    def process_prefixdir(self, cycle, prefix, prefixdir, buckets, start_slice):
        # assume that we can list every prefixdir in this prefix quickly.
        # Otherwise we have to retain more state between timeslices.

        # we define "shareid" as (SI string, shnum)
        disk_shares = set() # shareid
        for si_s in buckets:
            bucketdir = os.path.join(prefixdir, si_s)
            for sharefile in os.listdir(bucketdir):
                try:
                    shnum = int(sharefile)
                except ValueError:
                    continue # non-numeric means not a sharefile
                shareid = (si_s, shnum)
                disk_shares.add(shareid)

        # now check the database for everything in this prefix
        db_sharemap = self._leasedb.get_shares_for_prefix(prefix)
        db_shares = set(db_sharemap)
        if db_sharemap:
            print prefix, db_sharemap

        rec = self.state["cycle-to-date"]["space-recovered"]
        sharesets = [set() for st in xrange(len(SHARETYPES))]

        # The lease crawler used to calculate the lease age histogram while
        # crawling shares, and tests currently rely on that, but it would be
        # more efficient to maintain the histogram as leases are added,
        # updated, and removed.
        for key, value in db_sharemap.iteritems():
            (si_s, shnum) = key
            (used_space, sharetype) = value

            sharesets[sharetype].add(si_s)

            for age in self._leasedb.get_lease_ages(si_a2b(si_s), shnum, start_slice):
                self.add_lease_age_to_histogram(age)

            self.increment(rec, "examined-shares", 1)
            self.increment(rec, "examined-sharebytes", used_space)
            self.increment(rec, "examined-shares-" + SHARETYPES[sharetype], 1)
            self.increment(rec, "examined-sharebytes-" + SHARETYPES[sharetype], used_space)

        self.increment(rec, "examined-buckets", sum([len(s) for s in sharesets]))
        for st in SHARETYPES:
            self.increment(rec, "examined-buckets-" + SHARETYPES[st], len(sharesets[st]))

        # add new shares to the DB
        new_shares = disk_shares - db_shares
        for (si_s, shnum) in new_shares:
            fp = FilePath(prefixdir).child(si_s).child(str(shnum))
            used_space = get_used_space(fp)
            # FIXME
            sharetype = SHARETYPE_UNKNOWN
            try:
                self._leasedb.add_new_share(si_a2b(si_s), shnum, used_space, sharetype)
            except ShareAlreadyInDatabaseError:
                # XXX log and ignore
                raise
            else:
                self._leasedb.add_starter_lease(si_s, shnum)

        # remove deleted shares
        deleted_shares = db_shares - disk_shares
        for (si_s, shnum, sharetype) in deleted_shares:
            self._leasedb.remove_deleted_share(si_a2b(si_s), shnum)

        self._leasedb.commit()


    # these methods are for outside callers to use

    def set_expiration_policy(self, policy):
        self._expiration_policy = policy

    def get_expiration_policy(self):
        return self._expiration_policy

    def is_expiration_enabled(self):
        return self._expiration_policy.is_enabled()

    def db_is_incomplete(self):
        # don't bother looking at the sqlite database: it's certainly not
        # complete.
        return self.state["last-cycle-finished"] is None

    def increment(self, d, k, delta=1):
        if k not in d:
            d[k] = 0
        d[k] += delta

    def add_lease_age_to_histogram(self, age):
        print "ADD_LEASE_AGE", age
        bin_interval = 24*60*60
        bin_number = int(age/bin_interval)
        bin_start = bin_number * bin_interval
        bin_end = bin_start + bin_interval
        k = (bin_start, bin_end)
        self.increment(self.state["cycle-to-date"]["lease-age-histogram"], k, 1)

    def convert_lease_age_histogram(self, lah):
        print "lah =", lah
        # convert { (minage,maxage) : count } into [ (minage,maxage,count) ]
        # since the former is not JSON-safe (JSON dictionaries must have
        # string keys).
        json_safe_lah = []
        for k in sorted(lah):
            (minage,maxage) = k
            json_safe_lah.append( (minage, maxage, lah[k]) )
        return json_safe_lah

    def add_initial_state(self):
        # we fill ["cycle-to-date"] here (even though they will be reset in
        # self.started_cycle) just in case someone grabs our state before we
        # get started: unit tests do this
        so_far = self.create_empty_cycle_dict()
        self.state.setdefault("cycle-to-date", so_far)
        # in case we upgrade the code while a cycle is in progress, update
        # the keys individually
        for k in so_far:
            self.state["cycle-to-date"].setdefault(k, so_far[k])

    def create_empty_cycle_dict(self):
        recovered = self.create_empty_recovered_dict()
        so_far = {"corrupt-shares": [],
                  "space-recovered": recovered,
                  "lease-age-histogram": {}, # (minage,maxage)->count
                  "leases-per-share-histogram": {}, # leasecount->numshares
                  }
        return so_far

    def create_empty_recovered_dict(self):
        recovered = {}
        for a in ("actual", "examined"):
            for b in ("buckets", "shares", "diskbytes"):
                recovered["%s-%s" % (a, b)] = 0
                for st in SHARETYPES:
                    recovered["%s-%s-%s" % (a, b, SHARETYPES[st])] = 0
        return recovered

    def started_cycle(self, cycle):
        self.state["cycle-to-date"] = self.create_empty_cycle_dict()

    def finished_cycle(self, cycle):
        print "FINISHED_CYCLE!"
        # add to our history state, prune old history
        h = {}

        start = self.state["current-cycle-start-time"]
        now = time.time()
        h["cycle-start-finish-times"] = (start, now)
        ep = self.get_expiration_policy()
        h["expiration-enabled"] = ep.is_enabled()
        h["configured-expiration-mode"] = ep.get_parameters()

        s = self.state["cycle-to-date"]

        # state["lease-age-histogram"] is a dictionary (mapping
        # (minage,maxage) tuple to a sharecount), but we report
        # self.get_state()["lease-age-histogram"] as a list of
        # (min,max,sharecount) tuples, because JSON can handle that better.
        # We record the list-of-tuples form into the history for the same
        # reason.
        lah = self.convert_lease_age_histogram(s["lease-age-histogram"])
        h["lease-age-histogram"] = lah
        h["leases-per-share-histogram"] = s["leases-per-share-histogram"].copy()
        h["corrupt-shares"] = s["corrupt-shares"][:]
        # note: if ["shares-recovered"] ever acquires an internal dict, this
        # copy() needs to become a deepcopy
        h["space-recovered"] = s["space-recovered"].copy()

        self._leasedb.add_history_entry(cycle, h)

    def get_state(self):
        """In addition to the crawler state described in
        ShareCrawler.get_state(), I return the following keys which are
        specific to the lease-checker/expirer. Note that the non-history keys
        (with 'cycle' in their names) are only present if a cycle is currently
        running. If the crawler is between cycles, it is appropriate to show
        the latest item in the 'history' key instead. Also note that each
        history item has all the data in the 'cycle-to-date' value, plus
        cycle-start-finish-times.

         cycle-to-date:
          expiration-enabled
          configured-expiration-mode
          lease-age-histogram (list of (minage,maxage,sharecount) tuples)
          leases-per-share-histogram
          corrupt-shares (list of (si_b32,shnum) tuples, minimal verification)
          space-recovered

         estimated-remaining-cycle:
          # Values may be None if not enough data has been gathered to
          # produce an estimate.
          space-recovered

         estimated-current-cycle:
          # cycle-to-date plus estimated-remaining. Values may be None if
          # not enough data has been gathered to produce an estimate.
          space-recovered

         history: maps cyclenum to a dict with the following keys:
          cycle-start-finish-times
          expiration-enabled
          configured-expiration-mode
          lease-age-histogram
          leases-per-share-histogram
          corrupt-shares
          space-recovered

         The 'space-recovered' structure is a dictionary with the following
         keys:
          # 'examined' is what was looked at
          examined-buckets,     examined-buckets-$SHARETYPE
          examined-shares,      examined-shares-$SHARETYPE
          examined-diskbytes,   examined-diskbytes-$SHARETYPE

          # 'actual' is what was deleted
          actual-buckets,       actual-buckets-$SHARETYPE
          actual-shares,        actual-shares-$SHARETYPE
          actual-diskbytes,     actual-diskbytes-$SHARETYPE

        Note that the preferred terminology has changed since these keys
        were defined; "buckets" refers to what are now called sharesets,
        and "diskbytes" refers to bytes of used space on the storage backend,
        which is not necessarily the disk backend.

        The 'original-*' and 'configured-*' keys that were populated in
        pre-leasedb versions are no longer supported.
        """
        progress = self.get_progress()

        state = ShareCrawler.get_state(self) # does a shallow copy
        state["history"] = self._leasedb.get_history()

        if not progress["cycle-in-progress"]:
            del state["cycle-to-date"]
            return state

        so_far = state["cycle-to-date"].copy()
        state["cycle-to-date"] = so_far

        lah = so_far["lease-age-histogram"]
        so_far["lease-age-histogram"] = self.convert_lease_age_histogram(lah)
        so_far["expiration-enabled"] = self._expiration_policy.is_enabled()
        so_far["configured-expiration-mode"] = self._expiration_policy.get_parameters()

        so_far_sr = so_far["space-recovered"]
        remaining_sr = {}
        remaining = {"space-recovered": remaining_sr}
        cycle_sr = {}
        cycle = {"space-recovered": cycle_sr}

        if progress["cycle-complete-percentage"] > 0.0:
            pc = progress["cycle-complete-percentage"] / 100.0
            m = (1-pc)/pc
            for a in ("actual", "examined"):
                for b in ("buckets", "shares", "diskbytes"):
                    k = "%s-%s" % (a, b)
                    remaining_sr[k] = m * so_far_sr[k]
                    cycle_sr[k] = so_far_sr[k] + remaining_sr[k]
                    for st in SHARETYPES:
                        k = "%s-%s-%s" % (a, b, SHARETYPES[st])
                        remaining_sr[k] = m * so_far_sr[k]
                        cycle_sr[k] = so_far_sr[k] + remaining_sr[k]
        else:
            for a in ("actual", "examined"):
                for b in ("buckets", "shares", "diskbytes"):
                    k = "%s-%s" % (a, b)
                    remaining_sr[k] = None
                    cycle_sr[k] = None
                    for st in SHARETYPES:
                        k = "%s-%s-%s" % (a, b, SHARETYPES[st])
                        remaining_sr[k] = None
                        cycle_sr[k] = None

        state["estimated-remaining-cycle"] = remaining
        state["estimated-current-cycle"] = cycle
        return state
