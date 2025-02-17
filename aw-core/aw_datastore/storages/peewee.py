import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

import iso8601
from aw_core.dirs import get_data_dir
from aw_core.models import Event
from playhouse.migrate import SqliteMigrator, migrate
from playhouse.sqlite_ext import SqliteExtDatabase, JSONField

import peewee
from peewee import (
    AutoField,
    CharField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    IntegerField,
    Model,
)

from .abstract import AbstractStorage

logger = logging.getLogger(__name__)

# Prevent debug output from propagating
peewee_logger = logging.getLogger("peewee")
peewee_logger.setLevel(logging.INFO)

# Init'd later in the PeeweeStorage constructor.
#   See: http://docs.peewee-orm.com/en/latest/peewee/database.html#run-time-database-configuration
# Another option would be to use peewee's Proxy.
#   See: http://docs.peewee-orm.com/en/latest/peewee/database.html#dynamic-db
_db = SqliteExtDatabase(None)


LATEST_VERSION = 2


def auto_migrate(path: str) -> None:
    db = SqliteExtDatabase(path)
    migrator = SqliteMigrator(db)

    # check if bucketmodel has datastr field
    info = db.execute_sql("PRAGMA table_info(bucketmodel)")
    has_datastr = any(row[1] == "datastr" for row in info)

    if not has_datastr:
        datastr_field = CharField(default="{}")
        with db.atomic():
            migrate(migrator.add_column("bucketmodel", "datastr", datastr_field))

    db.close()


def chunks(ls, n):
    """Yield successive n-sized chunks from ls.
    From: https://stackoverflow.com/a/312464/965332"""
    for i in range(0, len(ls), n):
        yield ls[i : i + n]


def dt_plus_duration(dt, duration):
    # See peewee docs on datemath: https://docs.peewee-orm.com/en/latest/peewee/hacks.html#date-math
    return peewee.fn.strftime(
        "%Y-%m-%d %H:%M:%f+00:00",
        (peewee.fn.julianday(dt) - 2440587.5) * 86400.0 + duration,
        "unixepoch",
    )

def calculate_bucket_hash_key(name,user):
    return hashlib.md5((str(name)+str(user)).encode("utf-8")).hexdigest()

class BaseModel(Model):
    class Meta:
        database = _db

class UserModel(BaseModel):
    id = AutoField(primary_key=True)
    username = CharField()
    uuid = CharField(unique=True)
    created = DateTimeField()
    data = CharField()
    def json(self):
        return {
            "id": self.id,
            "username": self.username,
            "created": iso8601.parse_date(self.created)
            .astimezone(timezone.utc)
            .isoformat(),
            "data": json.loads(self.data),
        }

class BucketModel(BaseModel):
    key = IntegerField(primary_key=True)
    id = CharField(unique=True)
    created = DateTimeField(default=datetime.now)
    name = CharField(null=True)
    type = CharField()
    client = CharField()
    hostname = CharField()
    datastr = CharField(null=True)  # JSON-encoded object
    user = ForeignKeyField(UserModel, null=True)
    hash_key = CharField()

    def json(self):
        return {
            "id": self.id,
            "created": iso8601.parse_date(self.created)
            .astimezone(timezone.utc)
            .isoformat(),
            "name": self.name,
            "type": self.type,
            "client": self.client,
            "hostname": self.hostname,
            "data": json.loads(self.datastr) if self.datastr else {},
            "user": self.user,
            "hash_key": self.hash_key,
        }


class EventModel(BaseModel):
    id = AutoField()
    bucket = ForeignKeyField(BucketModel, backref="events", index=True)
    timestamp = DateTimeField(index=True, default=datetime.now)
    duration = DecimalField()
    datastr = CharField()

    @classmethod
    def from_event(cls, bucket_key, event: Event):
        return cls(
            bucket=bucket_key,
            id=event.id,
            timestamp=event.timestamp,
            duration=event.duration.total_seconds(),
            datastr=json.dumps(event.data),
        )

    def json(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "duration": float(self.duration),
            "data": json.loads(self.datastr),
        }

class PeeweeStorage(AbstractStorage):
    sid = "peewee"

    def __init__(self, testing: bool = True, filepath: Optional[str] = None) -> None:
        #data_dir = get_data_dir("aw-server")
        data_dir = os.getcwd() + "/data"

        if not filepath:
            filename = (
                "peewee-sqlite"
                + ("-testing" if testing else "")
                + f".v{LATEST_VERSION}"
                + ".db"
            )
            filepath = os.path.join(data_dir, filename)
        self.db = _db
        self.db.init(filepath)
        logger.info(f"Using database file: {filepath}")
        self.db.connect()

        self.bucket_hash_keys: Dict[str, int] = {}
        BucketModel.create_table(safe=True)
        EventModel.create_table(safe=True)
        UserModel.create_table(safe=True)

        # Migrate database if needed, requires closing the connection first
        self.db.close()
        auto_migrate(filepath)
        self.db.connect()

        # Update bucket keys
        self.update_bucket_hash_keys()

    def update_bucket_hash_keys(self) -> None:
        buckets = BucketModel.select()
        self.bucket_hash_keys = {bucket.hash_key: bucket.key for bucket in buckets}

    def buckets(self) -> Dict[str, Dict[str, Any]]:
        return {bucket.hash_key: bucket.json() for bucket in BucketModel.select()}

    def create_bucket(
        self,
        bucket_id: str,
        type_id: str,
        client: str,
        hostname: str,
        created: str,
        name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        user: Optional[int] = None
    ):
        user_id = user
        BucketModel.create(
            id=bucket_id,
            type=type_id,
            client=client,
            hostname=hostname,
            created=created,
            name=name,
            datastr=json.dumps(data or {}),
            user=user_id,
            hash_key=calculate_bucket_hash_key(bucket_id,user_id)
        )
        self.update_bucket_hash_keys()
        return calculate_bucket_hash_key(bucket_id,user_id)

    def update_bucket(
        self,
        bucket_hash_key: str,
        type_id: Optional[str] = None,
        client: Optional[str] = None,
        hostname: Optional[str] = None,
        name: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> None:
        if bucket_hash_key in self.bucket_hash_keys:
            bucket = BucketModel.get(BucketModel.key == self.bucket_hash_keys[bucket_hash_key])

            if type_id is not None:
                bucket.type = type_id
            if client is not None:
                bucket.client = client
            if hostname is not None:
                bucket.hostname = hostname
            if name is not None:
                bucket.name = name
            if data is not None:
                bucket.datastr = json.dumps(data)  # Encoding data dictionary to JSON

            bucket.save()
        else:
            raise ValueError("Bucket did not exist, could not update")

    def delete_bucket(self, bucket_hash_key: str) -> None:
        if bucket_hash_key in self.bucket_hash_keys:
            EventModel.delete().where(
                EventModel.bucket == self.bucket_hash_keys[bucket_hash_key]
            ).execute()
            BucketModel.delete().where(
                BucketModel.key == self.bucket_hash_keys[bucket_hash_key]
            ).execute()
            self.update_bucket_hash_keys()
        else:
            raise ValueError("Bucket did not exist, could not delete")

    def get_metadata(self, bucket_hash_key: str):
        if bucket_hash_key in self.bucket_hash_keys:
            bucket = BucketModel.get(
                BucketModel.key == self.bucket_hash_keys[bucket_hash_key]
            ).json()
            return bucket
        else:
            raise ValueError("Bucket did not exist, could not get metadata")

    def insert_one(self, bucket_hash_key: str, event: Event) -> Event:
        e = EventModel.from_event(self.bucket_hash_keys[bucket_hash_key], event)
        e.save()
        event.id = e.id
        return event

    def insert_many(self, bucket_hash_key: str, events: List[Event]) -> None:
        # NOTE: Events need to be handled differently depending on
        #       if they're upserts or inserts (have id's or not).

        # These events are updates which need to be applied one by one
        events_updates = [e for e in events if e.id is not None]
        for e in events_updates:
            self.insert_one(bucket_hash_key, e)

        # These events can be inserted with insert_many
        events_dictlist = [
            {
                "bucket": self.bucket_hash_keys[bucket_hash_key],
                "timestamp": event.timestamp,
                "duration": event.duration.total_seconds(),
                "datastr": json.dumps(event.data),
            }
            for event in events
            if event.id is None
        ]

        # Chunking into lists of length 100 is needed here due to SQLITE_MAX_COMPOUND_SELECT
        # and SQLITE_LIMIT_VARIABLE_NUMBER under Windows.
        # See: https://github.com/coleifer/peewee/issues/948
        for chunk in chunks(events_dictlist, 100):
            EventModel.insert_many(chunk).execute()

    def _get_event(self, bucket_hash_key, event_id) -> Optional[EventModel]:
        try:
            return (
                EventModel.select()
                .where(EventModel.id == event_id)
                .where(EventModel.bucket == self.bucket_hash_keys[bucket_hash_key])
                .get()
            )
        except peewee.DoesNotExist:
            return None

    def _get_last(self, bucket_hash_key) -> EventModel:
        return (
            EventModel.select()
            .where(EventModel.bucket == self.bucket_hash_keys[bucket_hash_key])
            .order_by(EventModel.timestamp.desc())
            .get()
        )

    def replace_last(self, bucket_hash_key, event):
        e = self._get_last(bucket_hash_key)
        e.timestamp = event.timestamp
        e.duration = event.duration.total_seconds()
        e.datastr = json.dumps(event.data)
        e.save()
        event.id = e.id
        return event

    def delete(self, bucket_hash_key, event_id):
        return (
            EventModel.delete()
            .where(EventModel.id == event_id)
            .where(EventModel.bucket == self.bucket_hash_keys[bucket_hash_key])
            .execute()
        )

    def replace(self, bucket_hash_key, event_id, event):
        e = self._get_event(bucket_hash_key, event_id)
        e.timestamp = event.timestamp
        e.duration = event.duration.total_seconds()
        e.datastr = json.dumps(event.data)
        e.save()
        event.id = e.id
        return event

    def get_event(
        self,
        bucket_hash_key: str,
        event_id: int,
    ) -> Optional[Event]:
        """
        Fetch a single event from a bucket.
        """
        res = self._get_event(bucket_hash_key, event_id)
        return Event(**EventModel.json(res)) if res else None

    def get_events(
        self,
        bucket_hash_key: str,
        limit: int,
        starttime: Optional[datetime] = None,
        endtime: Optional[datetime] = None,
    ):
        """
        Fetch events from a certain bucket, optionally from a given range of time.

        Example raw query:

            SELECT strftime(
              "%Y-%m-%d %H:%M:%f+00:00",
              ((julianday(timestamp) - 2440587.5) * 86400),
              'unixepoch'
            )
            FROM eventmodel
            WHERE eventmodel.timestamp > '2021-06-20'
            LIMIT 10;

        """
        if limit == 0:
            return []
        bucket_key = BucketModel.get(BucketModel.hash_key == bucket_hash_key).key
        q = (
            EventModel.select()
            .where(EventModel.bucket == bucket_key)
            .order_by(EventModel.timestamp.desc())
            .limit(limit)
        )

        q = self._where_range(q, starttime, endtime)

        res = q.execute()

        events = [Event(**e) for e in list(map(EventModel.json, res))]

        # Trim events that are out of range (as done in aw-server-rust)
        # TODO: Do the same for the other storage methods
        for e in events:
            if starttime:
                if e.timestamp < starttime:
                    e_end = e.timestamp + e.duration
                    e.timestamp = starttime
                    e.duration = e_end - e.timestamp
            if endtime:
                if e.timestamp + e.duration > endtime:
                    e.duration = endtime - e.timestamp
        return events

    def get_eventcount(
        self,
        bucket_hash_key: str,
        starttime: Optional[datetime] = None,
        endtime: Optional[datetime] = None,
    ) -> int:
        q = EventModel.select().where(EventModel.bucket == self.bucket_hash_keys[bucket_hash_key])
        q = self._where_range(q, starttime, endtime)
        return q.count()

    def _where_range(
        self,
        q,
        starttime: Optional[datetime] = None,
        endtime: Optional[datetime] = None,
    ):
        # Important to normalize datetimes to UTC, otherwise any UTC offset will be ignored
        if starttime:
            starttime = starttime.astimezone(timezone.utc)
        if endtime:
            endtime = endtime.astimezone(timezone.utc)

        if starttime:
            # Faster WHERE to speed up slow query below, leads to ~2-3x speedup
            # We'll assume events aren't >24h
            q = q.where(starttime - timedelta(hours=24) <= EventModel.timestamp)

            # This can be slow on large databases...
            # Tried creating various indexes and using SQLite's unlikely() function, but it had no effect
            q = q.where(
                starttime <= dt_plus_duration(EventModel.timestamp, EventModel.duration)
            )
        if endtime:
            q = q.where(EventModel.timestamp <= endtime)

        return q

    def get_user_by_uuid(self, user_uuid: int) -> Optional[dict]:
        user = {}
        try:
            user = UserModel.select().where(UserModel.uuid == user_uuid).get().json()
            user = {"user": user}
        except UserModel.DoesNotExist:
            pass
        return user
    def update_user(self, user_uuid: int, data):
        UserModel.update(**data).where(UserModel.uuid == user_uuid).execute()

    def create_user(self, data):
        UserModel.create(**data)

    def get_users(self):
        return [user.json() for user in UserModel.select()]

    def get_buckets_for_user(self, user):
        buckets = {}
        if user == "all":
            buckets = {b.hash_key: {**b.json(),'events_count': self.get_eventcount(b.hash_key, starttime=datetime.strptime('2020-01-01 00:00:00', '%Y-%m-%d %H:%M:%S'),endtime=datetime.now())} for b in BucketModel.select()}
        else:
            buckets = {b.hash_key: {**b.json(),'events_count': self.get_eventcount(b.hash_key, starttime=datetime.strptime('2020-01-01 00:00:00', '%Y-%m-%d %H:%M:%S'),endtime=datetime.now())} for b in BucketModel.select().where(BucketModel.user == user)}
        for i in buckets:
            buckets[i]['estimated_size'] = buckets[i]['events_count'] * 150
        return buckets