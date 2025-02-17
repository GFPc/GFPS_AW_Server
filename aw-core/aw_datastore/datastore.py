import logging
from datetime import datetime, timedelta, timezone
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Union,
)

from aw_core.models import Event

from .storages import AbstractStorage

logger = logging.getLogger(__name__)


class Datastore:
    def __init__(
        self,
        storage_strategy: Callable[..., AbstractStorage],
        testing=False,
        **kwargs,
    ) -> None:
        self.logger = logger.getChild("Datastore")
        self.bucket_instances: Dict[str, Bucket] = dict()

        self.storage_strategy = storage_strategy(testing=testing, **kwargs)

    def __repr__(self):
        return f"<Datastore object using {self.storage_strategy.__class__.__name__}>"

    def __getitem__(self, bucket_hash_key: str) -> "Bucket":
        # If this bucket doesn't have a initialized object, create it
        if bucket_hash_key not in self.bucket_instances:
            # If the bucket exists in the database, create an object representation of it
            if bucket_hash_key in self.buckets():
                bucket = Bucket(self, bucket_hash_key)
                self.bucket_instances[bucket_hash_key] = bucket
            else:
                self.logger.error(
                    f"Cannot create a Bucket object for {bucket_hash_key} because it doesn't exist in the database"
                )
                raise KeyError

        return self.bucket_instances[bucket_hash_key]

    def create_bucket(
        self,
        bucket_id: str,
        type: str,
        client: str,
        hostname: str,
        created: Optional[datetime] = None,
        name: Optional[str] = None,
        data: Optional[dict] = None,
        user: Optional[int] = None
    ) -> "Bucket":
        created = created or datetime.now(timezone.utc)
        self.logger.info(f"Creating bucket '{bucket_id}'")
        bucket_hash_key = self.storage_strategy.create_bucket(
            bucket_id, type, client, hostname, created.isoformat(), name=name, data=data, user=user
        )
        return self[bucket_hash_key]

    def update_bucket(self, bucket_hash_key: str, **kwargs):
        self.logger.info(f"Updating bucket '{bucket_hash_key}'")
        return self.storage_strategy.update_bucket(bucket_hash_key, **kwargs)

    def delete_bucket(self, bucket_hash_key: str):
        self.logger.info(f"Deleting bucket '{bucket_hash_key}'")
        if bucket_hash_key in self.bucket_instances:
            del self.bucket_instances[bucket_hash_key]
        return self.storage_strategy.delete_bucket(bucket_hash_key)

    def buckets(self):
        return self.storage_strategy.buckets()

    def get_user_by_uuid(self, uuid):
        return self.storage_strategy.get_user_by_uuid(uuid)

    def update_user(self, uuid, data):
        return self.storage_strategy.update_user(uuid, data)

    def create_user(self, data):
        return self.storage_strategy.create_user(data)

    def get_users(self):
        return self.storage_strategy.get_users()
    def get_buckets_for_user(self, user):
        return self.storage_strategy.get_buckets_for_user(user)

class Bucket:
    def __init__(self, datastore: Datastore, bucket_hash_key: str) -> None:
        self.logger = logger.getChild("Bucket")
        self.ds = datastore
        self.bucket_hash_key = bucket_hash_key

    def metadata(self) -> dict:
        return self.ds.storage_strategy.get_metadata(self.bucket_hash_key)

    def get(
        self,
        limit: int = -1,
        starttime: Optional[datetime] = None,
        endtime: Optional[datetime] = None,
    ) -> List[Event]:
        """Returns events sorted in descending order by timestamp"""
        # Resolution is rounded down since not all datastores like microsecond precision
        if starttime:
            starttime = starttime.replace(
                microsecond=1000 * int(starttime.microsecond / 1000)
            )
        if endtime:
            # Rounding up here in order to ensure events aren't missed
            # second_offset and microseconds modulo required since replace() only takes microseconds up to 999999 (doesn't handle overflow)
            milliseconds = 1 + int(endtime.microsecond / 1000)
            second_offset = int(milliseconds / 1000)  # usually 0, rarely 1
            microseconds = (
                (1000 * milliseconds) % 1000000
            )  # will likely just be 1000 * milliseconds, if it overflows it would become zero
            endtime = endtime.replace(microsecond=microseconds) + timedelta(
                seconds=second_offset
            )

        return self.ds.storage_strategy.get_events(
            self.bucket_hash_key, limit, starttime, endtime
        )

    def get_by_id(self, event_id) -> Optional[Event]:
        """Will return the event with the provided ID, or None if not found."""
        return self.ds.storage_strategy.get_event(self.bucket_hash_key, event_id)

    def get_eventcount(
        self, starttime: Optional[datetime] = None, endtime: Optional[datetime] = None
    ) -> int:
        return self.ds.storage_strategy.get_eventcount(
            self.bucket_hash_key, starttime, endtime
        )

    def insert(self, events: Union[Event, List[Event]]) -> Optional[Event]:
        """
        Inserts one or several events.
        If a single event is inserted, return the event with its id assigned.
        If several events are inserted, returns None. (This is due to there being no efficient way of getting ids out when doing bulk inserts with some datastores such as peewee/SQLite)
        """

        # NOTE: Should we keep the timestamp checking?
        warn_older_event = False

        # Get last event for timestamp check after insert
        if warn_older_event:
            last_event_list = self.get(1)
            last_event = None
            if last_event_list:
                last_event = last_event_list[0]

        now = datetime.now(tz=timezone.utc)

        inserted: Optional[Event] = None

        # Call insert
        if isinstance(events, Event):
            oldest_event: Optional[Event] = events
            if events.timestamp + events.duration > now:
                self.logger.warning(
                    f"Event inserted into bucket {self.bucket_hash_key} reaches into the future. Current UTC time: {str(now)}. Event data: {str(events)}"
                )
            inserted = self.ds.storage_strategy.insert_one(self.bucket_hash_key, events)
            # assert inserted
        elif isinstance(events, list):
            if events:
                oldest_event = sorted(events, key=lambda k: k["timestamp"])[0]
            else:  # pragma: no cover
                oldest_event = None
            for event in events:
                if event.timestamp + event.duration > now:
                    self.logger.warning(
                        f"Event inserted into bucket {self.bucket_hash_key} reaches into the future. Current UTC time: {str(now)}. Event data: {str(event)}"
                    )
            self.ds.storage_strategy.insert_many(self.bucket_hash_key, events)
        else:
            raise TypeError

        # Warn if timestamp is older than last event
        if warn_older_event and last_event and oldest_event:
            if oldest_event.timestamp < last_event.timestamp:  # pragma: no cover
                self.logger.warning(
                    f"""Inserting event that has a older timestamp than previous event!
Previous: {last_event}
Inserted: {oldest_event}"""
                )

        return inserted

    def delete(self, event_id):
        return self.ds.storage_strategy.delete(self.bucket_hash_key, event_id)

    def replace_last(self, event):
        return self.ds.storage_strategy.replace_last(self.bucket_hash_key, event)

    def replace(self, event_id, event):
        return self.ds.storage_strategy.replace(self.bucket_hash_key, event_id, event)
