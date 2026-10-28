"""The lookup tables of the address suggestions (H, owner decision of 2026-10-28): ``AddressPlace`` and ``AddressStreet``, the
covering index ``addr_house`` and the opening guard of the directory file.

What is proven: the tables are filled with the addresses (``fill_lookup_tables``: idempotent, by ``save`` and ``bulk_create``);
the places have a centre (``fill_place_centres``); the importer fills them with each chunk in one transaction, removes them with
a generation, and does not take a generation without them as published; the local seed replaces them; the migrations 0020 and
0021 build them in place on a file at migration 0019 without losing an address, and ``setup.prepare_directory`` applies them in
place (guard held, a running import, too little disk, an incomplete schema); ``_schema_matches`` also checks unique constraints;
the opening guard of the file lets readers in side by side, keeps readers and a reset out of each other, releases, turns an
unlock failure into a database error and defines the Windows prototypes once, also under threads."""

import importlib
import io
import os
import sqlite3
import tempfile
import threading
import types
import uuid
from contextlib import contextmanager
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock, skipUnless

from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, OperationalError, connections, models, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase, tag
from django.utils import timezone

from leg_platform.addresses import importer, jobs, storage
from leg_platform.addresses import setup as directory_setup
from leg_platform.addresses.keys import key
from leg_platform.addresses.management.commands import seed_local_address_directory as seed
from leg_platform.addresses.models import (
    PLACE_SAMPLE,
    Address,
    AddressPlace,
    AddressStreet,
    Directory,
    ImportRequest,
    fill_lookup_tables,
    fill_place_centres,
)
from leg_platform.addresses.storage import DIRECTORY_ALIAS, directory_open_guard
from leg_platform.addresses.tests.test_directory import HEADER, archive_bytes

ITTIGEN = ("3063", "Ittigen")
BERN = ("3000", "Bern")


def address(generation, street, number, place, east=None, north=None):
    return Address(
        generation=generation, street=street, house_number=number, postal_code=place[0], locality=place[1],
        easting=None if east is None else Decimal(east), northing=None if north is None else Decimal(north),
    )


def places_of(generation):
    return {(row.postal_code, row.locality) for row in AddressPlace.objects.filter(generation=generation)}


def streets_of(generation):
    return {
        (row.street, row.postal_code, row.locality) for row in AddressStreet.objects.filter(generation=generation)
    }


@tag("mariadb")
class FillTablesTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.generation = uuid.uuid4()

    def test_bulk_create_fills_one_place_and_one_street_row_per_distinct_value(self):
        Address.objects.bulk_create([
            address(self.generation, "Ittigenstrasse", "5", ITTIGEN),
            address(self.generation, "Ittigenstrasse", "7", ITTIGEN),
            address(self.generation, "Ittigenstrasse", "7", BERN),
            address(self.generation, "Hauptstrasse", "1", BERN),
        ])

        self.assertEqual(places_of(self.generation), {ITTIGEN, BERN})
        self.assertEqual(
            streets_of(self.generation),
            {("Ittigenstrasse", "3063", "Ittigen"), ("Ittigenstrasse", "3000", "Bern"), ("Hauptstrasse", "3000", "Bern")},
        )
        row = AddressStreet.objects.get(generation=self.generation, street="Hauptstrasse")
        self.assertEqual((row.street_key, row.locality_key), ("hauptstrasse", "bern"))
        self.assertEqual(AddressPlace.objects.get(generation=self.generation, postal_code="3000").locality_key, "bern")

    def test_save_fills_them_too(self):
        address(self.generation, "Zürichweg", "1", ("8001", "Zürich")).save()

        self.assertEqual(places_of(self.generation), {("8001", "Zürich")})
        self.assertEqual(AddressStreet.objects.get(generation=self.generation).street_key, "zurichweg")

    def test_filling_twice_changes_nothing(self):
        rows = [address(self.generation, "Ittigenstrasse", "5", ITTIGEN), address(self.generation, "Hauptstrasse", "1", BERN)]
        Address.objects.bulk_create(rows)
        before = (AddressPlace.objects.count(), AddressStreet.objects.count())

        fill_lookup_tables(rows)
        fill_lookup_tables(rows)

        self.assertEqual((AddressPlace.objects.count(), AddressStreet.objects.count()), before)

    def test_nothing_to_fill_is_no_error(self):
        fill_lookup_tables([])

        self.assertEqual((AddressPlace.objects.count(), AddressStreet.objects.count()), (0, 0))

    def test_the_rows_of_another_generation_are_separate(self):
        other = uuid.uuid4()
        Address.objects.bulk_create([
            address(self.generation, "Ittigenstrasse", "5", ITTIGEN), address(other, "Ittigenstrasse", "5", ITTIGEN),
        ])

        self.assertEqual(AddressStreet.objects.count(), 2)
        self.assertEqual(streets_of(other), {("Ittigenstrasse", "3063", "Ittigen")})

    def test_the_unique_rows_reject_a_duplicate(self):
        Address.objects.bulk_create([address(self.generation, "Ittigenstrasse", "5", ITTIGEN)])

        with self.assertRaises(IntegrityError), transaction.atomic(using=DIRECTORY_ALIAS):
            AddressPlace.objects.create(
                generation=self.generation, postal_code="3063", locality="Ittigen", locality_key="ittigen",
            )

    def test_the_centre_of_a_place_is_the_mean_of_its_coordinates_and_empty_without_any(self):
        Address.objects.bulk_create([
            address(self.generation, "Ittigenstrasse", "5", ITTIGEN, 2602000, 1203000),
            address(self.generation, "Ittigenstrasse", "7", ITTIGEN, 2603000, 1204000),
            address(self.generation, "Hauptstrasse", "1", BERN),
        ])

        fill_place_centres(self.generation)

        ittigen = AddressPlace.objects.get(generation=self.generation, postal_code="3063")
        bern = AddressPlace.objects.get(generation=self.generation, postal_code="3000")
        self.assertEqual((ittigen.easting, ittigen.northing), (2602500.0, 1203500.0))
        self.assertEqual((bern.easting, bern.northing), (None, None))

    def test_the_centre_ignores_an_address_without_coordinates_and_other_generations(self):
        other = uuid.uuid4()
        Address.objects.bulk_create([
            address(self.generation, "Ittigenstrasse", "5", ITTIGEN, 2602000, 1203000),
            address(self.generation, "Ittigenstrasse", "7", ITTIGEN),
            address(other, "Ittigenstrasse", "5", ITTIGEN, 2700000, 1300000),
        ])

        fill_place_centres(self.generation)

        centre = AddressPlace.objects.get(generation=self.generation)
        self.assertEqual((centre.easting, centre.northing), (2602000.0, 1203000.0))
        untouched = AddressPlace.objects.get(generation=other)
        self.assertEqual((untouched.easting, untouched.northing), (None, None))

    def test_a_sample_of_at_most_the_sample_size_is_read_per_place(self):
        self.assertGreater(PLACE_SAMPLE, 0)
        Address.objects.bulk_create([
            address(self.generation, "Ittigenstrasse", str(number), ITTIGEN, 2602000, 1203000) for number in range(1, 8)
        ])

        with mock.patch("leg_platform.addresses.models.PLACE_SAMPLE", 3):
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connections[DIRECTORY_ALIAS]) as queries:
                fill_place_centres(self.generation)

        self.assertTrue(any("LIMIT 3" in query["sql"] for query in queries.captured_queries))
        self.assertEqual(AddressPlace.objects.get(generation=self.generation).easting, 2602000.0)


@tag("mariadb")
class ImporterLookupTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        disk = mock.patch.object(
            importer.shutil, "disk_usage",
            return_value=types.SimpleNamespace(total=10**12, used=0, free=importer.MIN_FREE_DISK_BYTES * 10),
        )
        disk.start()
        self.addCleanup(disk.stop)
        self.generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=self.generation, data_date=timezone.localdate(), imported_at=timezone.now(),
            checksum="0" * 64, row_count=1, has_egid=True,
        )
        Address.objects.create(
            generation=self.generation, street="Previous Street", house_number="1", postal_code="9999", locality="Example Town",
        )

    CSV = HEADER + "Chunk Street;1;9999 Town;x;x\nChunk Street;2;9999 Town;x;x\nOther Road;4;9998 Other Place;x;x\n"

    def publish(self, text=CSV, checksum="1" * 64):
        importer._publish(io.BytesIO(archive_bytes(text)), checksum, timezone.localdate())
        return Directory.objects.get(pk=1).generation

    def test_a_published_generation_has_its_places_streets_and_centres(self):
        generation = self.publish()

        self.assertNotEqual(generation, self.generation)
        self.assertEqual(places_of(generation), {("9999", "Town"), ("9998", "Other Place")})
        self.assertEqual(streets_of(generation), {("Chunk Street", "9999", "Town"), ("Other Road", "9998", "Other Place")})
        # The fixed position of the test table is the centre of every place (a mean of equal points).
        for place in AddressPlace.objects.filter(generation=generation):
            self.assertEqual((place.easting, place.northing), (2600001.5, 1200001.25))

    def test_the_previous_generation_keeps_its_lookup_rows_until_it_is_pruned(self):
        self.publish()

        self.assertEqual(places_of(self.generation), {("9999", "Example Town")})
        self.assertEqual(streets_of(self.generation), {("Previous Street", "9999", "Example Town")})

    def test_the_lookup_rows_of_a_chunk_and_the_chunk_are_one_transaction(self):
        generation = uuid.uuid4()
        token = importer._acquire_lease()
        rows = [address(generation, "Chunk Street", str(number), ("9999", "Town")) for number in range(3)]
        with mock.patch("leg_platform.addresses.models.fill_lookup_tables", side_effect=DatabaseError("lookup failed")):
            with self.assertRaises(DatabaseError), transaction.atomic(using=DIRECTORY_ALIAS):
                importer._insert_chunk(rows, token)
        self.assertFalse(Address.objects.filter(generation=generation).exists())
        self.assertFalse(AddressStreet.objects.filter(generation=generation).exists())
        # And with a working fill the chunk and its lookup rows arrive together.
        importer._insert_chunk(rows, token)
        self.assertEqual(Address.objects.filter(generation=generation).count(), 3)
        self.assertEqual(streets_of(generation), {("Chunk Street", "9999", "Town")})

    def test_a_failed_import_leaves_no_lookup_row_of_the_unpublished_generation(self):
        text = HEADER + "Chunk Street 1;1;9999 Town;x;x\nBad;1;invalid;x;x\n"
        with mock.patch.object(importer, "CHUNK_ROWS", 1), self.assertRaises(importer.ImportFailure):
            importer._publish(io.BytesIO(archive_bytes(text)), "1" * 64, timezone.localdate())

        self.assertEqual({row.generation for row in AddressStreet.objects.all()}, {self.generation})
        self.assertEqual({row.generation for row in AddressPlace.objects.all()}, {self.generation})

    def test_deleting_a_generation_removes_its_lookup_rows_and_only_theirs(self):
        old = uuid.uuid4()
        Address.objects.bulk_create([address(old, "Old Street", "1", ITTIGEN), address(old, "Old Street", "2", BERN)])

        importer._delete_generation(old)

        self.assertEqual((places_of(old), streets_of(old)), (set(), set()))
        self.assertFalse(Address.objects.filter(generation=old).exists())
        self.assertEqual(streets_of(self.generation), {("Previous Street", "9999", "Example Town")})

    def test_pruning_removes_the_lookup_rows_of_every_generation_not_kept_and_orphans(self):
        old = uuid.uuid4()
        Address.objects.bulk_create([address(old, "Old Street", "1", ITTIGEN)])
        crashed = uuid.uuid4()  # an import that died after the lookup rows but before the addresses
        AddressStreet.objects.create(
            generation=crashed, street="Lost Street", street_key="lost street", postal_code="1000", locality="Lost",
            locality_key="lost",
        )
        AddressPlace.objects.create(generation=crashed, postal_code="1000", locality="Lost", locality_key="lost")

        deleted = importer.prune_old_generations({self.generation})

        self.assertEqual(deleted, 1)
        self.assertEqual({row.generation for row in AddressStreet.objects.all()}, {self.generation})
        self.assertEqual({row.generation for row in AddressPlace.objects.all()}, {self.generation})

    def test_pruning_with_everything_kept_removes_no_lookup_row(self):
        importer.prune_old_generations({self.generation})

        self.assertEqual(AddressPlace.objects.count(), 1)
        self.assertEqual(AddressStreet.objects.count(), 1)

    def test_a_generation_without_lookup_rows_is_not_published(self):
        directory = Directory.objects.get(pk=1)
        self.assertTrue(importer._is_published(directory, "0" * 64, repair=False))

        AddressStreet.objects.filter(generation=self.generation).delete()
        self.assertFalse(importer._is_published(directory, "0" * 64, repair=False))
        fill_lookup_tables(list(Address.objects.filter(generation=self.generation)))
        self.assertTrue(importer._is_published(directory, "0" * 64, repair=False))
        AddressPlace.objects.filter(generation=self.generation).delete()
        self.assertFalse(importer._is_published(directory, "0" * 64, repair=False))

    def test_a_directory_without_lookup_rows_is_imported_again_although_the_checksum_is_unchanged(self):
        AddressPlace.objects.all().delete()
        AddressStreet.objects.all().delete()

        generation = self.publish(checksum="0" * 64)

        self.assertNotEqual(generation, self.generation)
        self.assertTrue(places_of(generation))

    def test_a_database_error_while_asking_for_the_lookup_rows_means_not_published(self):
        directory = Directory.objects.get(pk=1)
        with mock.patch.object(AddressPlace.objects, "filter", side_effect=DatabaseError("broken")):
            self.assertFalse(importer._has_lookup_rows(directory))


@tag("mariadb")
class SeedLookupTests(TestCase):
    databases = {"default", "addresses"}

    def test_replacing_the_directory_replaces_the_lookup_rows(self):
        old = uuid.uuid4()
        Address.objects.bulk_create([address(old, "Old Street", "1", ITTIGEN)])
        AddressPlace.objects.create(generation=uuid.uuid4(), postal_code="1000", locality="Lost", locality_key="lost")

        count = seed.replace_directory()

        generation = Directory.objects.get(pk=1).generation
        self.assertEqual(count, len(seed.SYNTHETIC_ROWS))
        self.assertEqual({row.generation for row in AddressPlace.objects.all()}, {generation})
        self.assertEqual({row.generation for row in AddressStreet.objects.all()}, {generation})
        self.assertIn(("Example Street", "9999", "Example Town"), streets_of(generation))
        self.assertEqual(places_of(generation), {(row[2], row[3]) for row in seed.SYNTHETIC_ROWS})

    def test_a_second_replacement_leaves_no_row_of_the_first(self):
        seed.replace_directory()
        first = Directory.objects.get(pk=1).generation
        seed.replace_directory(rows=(("Only Street", "1", "9990", "Only Town"),))

        self.assertFalse(AddressStreet.objects.filter(generation=first).exists())
        self.assertEqual(AddressStreet.objects.count(), 1)
        self.assertEqual(AddressPlace.objects.count(), 1)


def raw_directory_at_0019(alias, rows):
    """Put a directory row and ``rows`` (street, number, postal code, town, easting, northing) into a file at migration 0019."""
    generation = uuid.uuid4()
    with alias.cursor() as cursor:
        cursor.execute(
            "INSERT INTO addresses_directory (id, generation, data_date, imported_at, checksum, row_count, has_egid) "
            "VALUES (1, %s, %s, %s, %s, %s, 1)",
            [generation.hex, "2026-10-28", "2026-10-28 08:00:00", "c" * 64, len(rows)],
        )
        for street, number, code, town, east, north in rows:
            cursor.execute(
                "INSERT INTO addresses_address (generation, street, house_number, postal_code, locality, street_key, "
                "house_number_key, locality_key, easting, northing) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [generation.hex, street, number, code, town, key(street), key(number), key(town), east, north],
            )
    return generation


ROWS_AT_0019 = (
    ("Ittigenstrasse", "5", "3063", "Ittigen", 2602000.0, 1203000.0),
    ("Ittigenstrasse", "7", "3063", "Ittigen", 2603000.0, 1204000.0),
    ("Hauptstrasse", "1", "3000", "Bern", None, None),
    ("Hauptstrasse", "2", "3000", "Bern", None, None),
)


@contextmanager
def directory_file(path):
    alias = connections[DIRECTORY_ALIAS]
    alias.close()
    with mock.patch.dict(alias.settings_dict, {"NAME": str(path)}):
        try:
            yield alias
        finally:
            alias.close()


def migration_module(name):
    return importlib.import_module(f"leg_platform.addresses.migrations.{name}")


@tag("mariadb")
class InPlaceMigrationTests(TransactionTestCase):
    """Migrations 0020 and 0021 on a file at 0019, and ``setup.prepare_directory`` for them (H4, H5)."""

    databases = {"default", "addresses"}

    @contextmanager
    def file_at_0019(self, rows=ROWS_AT_0019):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "address-directory.sqlite3"
            with directory_file(path) as alias:
                call_command("migrate", "addresses", "0019", database=DIRECTORY_ALIAS, verbosity=0)
                generation = raw_directory_at_0019(alias, rows) if rows is not None else None
                yield path, alias, generation

    def prepare(self):
        out, err = StringIO(), StringIO()
        result = directory_setup.prepare_directory(stdout=out, stderr=err)
        return result, out.getvalue(), err.getvalue()

    def tables(self, alias):
        with alias.cursor() as cursor:
            return set(alias.introspection.table_names(cursor))

    def indexes(self, alias, table):
        with alias.cursor() as cursor:
            return set(alias.introspection.get_constraints(cursor, table))

    def test_the_migrations_are_marked_as_additive_and_the_older_ones_are_not(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for name in ("0020_lookup_tables_and_house_index", "0021_addressplace_centre"):
            self.assertTrue(loader.get_migration("addresses", name).in_place, name)
        for name in ("0010_address_coordinates", "0019_importrequest_progress_units_and_more"):
            self.assertFalse(getattr(loader.get_migration("addresses", name), "in_place", False), name)

    def test_the_setup_applies_them_in_place_and_keeps_every_address_row(self):
        with self.file_at_0019() as (path, alias, generation):
            self.assertNotIn("addresses_addressstreet", self.tables(alias))
            with alias.cursor() as cursor:
                cursor.execute("SELECT id, street, house_number, postal_code, locality FROM addresses_address ORDER BY id")
                before = cursor.fetchall()

            (count, queued), _out, err = self.prepare()

            self.assertEqual(err, "")
            self.assertEqual(queued, False)
            self.assertEqual(count, 2)
            with alias.cursor() as cursor:
                cursor.execute("SELECT id, street, house_number, postal_code, locality FROM addresses_address ORDER BY id")
                self.assertEqual(cursor.fetchall(), before)
                cursor.execute("SELECT id, generation, checksum, row_count FROM addresses_directory")
                self.assertEqual(cursor.fetchall(), [(1, generation.hex, "c" * 64, 4)])
            self.assertEqual(places_of(generation), {ITTIGEN, BERN})
            self.assertEqual(
                streets_of(generation), {("Ittigenstrasse", "3063", "Ittigen"), ("Hauptstrasse", "3000", "Bern")}
            )
            self.assertIn("addr_house", self.indexes(alias, "addresses_address"))
            self.assertFalse(directory_setup.reset_marker(path).exists())

    def test_the_setup_queues_no_import_and_changes_no_job_for_them(self):
        with self.file_at_0019():
            ImportRequest.objects.update_or_create(pk=1, defaults={"state": ImportRequest.State.SUCCEEDED})

            (_count, queued), out, _err = self.prepare()

            self.assertFalse(queued)
            self.assertEqual(ImportRequest.objects.get(pk=1).state, ImportRequest.State.SUCCEEDED)
            self.assertNotIn("queued", out)

    def test_the_centres_of_the_places_are_filled_by_the_second_migration(self):
        with self.file_at_0019() as (_path, _alias, generation):
            self.prepare()

            ittigen = AddressPlace.objects.get(generation=generation, postal_code="3063")
            bern = AddressPlace.objects.get(generation=generation, postal_code="3000")
            self.assertEqual((ittigen.easting, ittigen.northing), (2602500.0, 1203500.0))
            self.assertEqual((bern.easting, bern.northing), (None, None))

    def test_a_second_setup_changes_nothing(self):
        with self.file_at_0019() as (_path, alias, generation):
            self.prepare()
            counts = (Address.objects.count(), AddressPlace.objects.count(), AddressStreet.objects.count())

            (count, queued), _out, err = self.prepare()

            self.assertEqual((count, queued, err), (0, False, ""))
            self.assertEqual((Address.objects.count(), AddressPlace.objects.count(), AddressStreet.objects.count()), counts)

    def test_filling_the_tables_again_is_idempotent(self):
        with self.file_at_0019() as (_path, alias, generation):
            self.prepare()
            counts = (AddressPlace.objects.count(), AddressStreet.objects.count())
            module = migration_module("0020_lookup_tables_and_house_index")
            editor = types.SimpleNamespace(connection=alias)

            module.fill_tables(None, editor)
            module.fill_tables(None, editor)
            migration_module("0021_addressplace_centre").fill_centres(None, editor)

            self.assertEqual((AddressPlace.objects.count(), AddressStreet.objects.count()), counts)
            ittigen = AddressPlace.objects.get(generation=generation, postal_code="3063")
            self.assertEqual((ittigen.easting, ittigen.northing), (2602500.0, 1203500.0))

    def test_a_file_without_a_published_directory_gets_the_tables_and_no_rows(self):
        with self.file_at_0019(rows=None) as (_path, alias, _generation):
            (_count, queued), _out, err = self.prepare()

            self.assertEqual((queued, err), (False, ""))
            self.assertIn("addresses_addressplace", self.tables(alias))
            self.assertEqual((AddressPlace.objects.count(), AddressStreet.objects.count()), (0, 0))

    def test_the_street_rows_are_built_in_ranges_of_the_postal_code_without_losing_one(self):
        rows = tuple(
            (f"Teststrasse {number}", "1", f"{first}{number:03d}", f"Ort{first}", None, None)
            for first in range(1, 10) for number in (0, 1)
        )
        with self.file_at_0019(rows=rows) as (_path, _alias, generation):
            self.prepare()

            self.assertEqual(AddressStreet.objects.filter(generation=generation).count(), len(rows))
            self.assertEqual(AddressPlace.objects.filter(generation=generation).count(), len(rows))

    def test_a_held_guard_stops_the_in_place_migration_with_a_database_error(self):
        with self.file_at_0019() as (path, alias, _generation):
            with directory_open_guard(path):
                with self.assertRaises(OperationalError):
                    self.prepare()
            self.assertNotIn("addresses_addressstreet", self.tables(alias))
            self.assertEqual(Address.objects.count(), 4)

    def test_the_guard_failure_inside_the_in_place_path_is_a_database_error_without_the_path(self):
        with self.file_at_0019() as (path, alias, _generation):
            with mock.patch.object(directory_setup, "directory_open_guard", side_effect=OSError(str(path))):
                with self.assertRaises(OperationalError) as caught:
                    self.prepare()
            self.assertNotIn(str(path), str(caught.exception))
            self.assertNotIn("addresses_addressstreet", self.tables(alias))

    def test_a_running_import_stops_the_in_place_migration(self):
        with self.file_at_0019() as (_path, alias, _generation):
            ImportRequest.objects.update_or_create(
                pk=1, defaults={
                    "state": ImportRequest.State.RUNNING, "started_at": timezone.now(), "claim_token": uuid.uuid4(),
                },
            )
            self.assertTrue(jobs.is_alive(ImportRequest.objects.get(pk=1)))

            with self.assertRaisesMessage(OperationalError, "still running"):
                self.prepare()

            self.assertNotIn("addresses_addressstreet", self.tables(alias))
            self.assertEqual(Address.objects.count(), 4)

    def test_too_little_free_disk_skips_the_migration_with_a_message_and_does_not_fail(self):
        with self.file_at_0019() as (path, alias, _generation):
            with mock.patch.object(directory_setup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1024)):
                (count, queued), _out, err = self.prepare()

            self.assertEqual((count, queued), (2, False))
            self.assertIn("migration was skipped", err)
            self.assertIn("free the space", err.lower())
            self.assertNotIn("addresses_addressstreet", self.tables(alias))
            self.assertEqual(Address.objects.count(), 4)
            self.assertTrue(path.is_file())

    def test_the_space_needed_grows_with_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            small, large = Path(folder) / "small", Path(folder) / "large"
            small.write_bytes(b"x" * 1000)
            large.write_bytes(b"x" * 10_000_000)

            self.assertEqual(
                directory_setup._in_place_space_needed(small),
                int(1000 * directory_setup.IN_PLACE_FILE_SHARE) * 2 + directory_setup.IN_PLACE_MARGIN,
            )
            self.assertGreater(directory_setup._in_place_space_needed(large), directory_setup._in_place_space_needed(small))
            self.assertEqual(directory_setup._in_place_space_needed(Path(folder) / "missing"), 0)

    def test_an_incomplete_schema_after_the_migration_raises_and_leaves_the_file_as_it_is(self):
        with self.file_at_0019() as (path, alias, _generation):
            with mock.patch.object(directory_setup, "_schema_matches", return_value=False):
                with mock.patch.object(directory_setup, "_remove_catalogue") as remove:
                    with self.assertRaisesMessage(OperationalError, "incomplete after the in-place migration"):
                        self.prepare()

            remove.assert_not_called()
            self.assertTrue(path.is_file())
            self.assertFalse(directory_setup.reset_marker(path).exists())
            self.assertEqual(Address.objects.count(), 4)
            self.assertEqual(Directory.objects.get(pk=1).checksum, "c" * 64)

    def test_a_pending_migration_that_is_not_additive_still_rebuilds_the_file(self):
        with self.file_at_0019() as (path, alias, _generation):
            plain = types.SimpleNamespace(in_place=False)
            real = directory_setup._changes_directory

            def changes(migration):
                return True if migration is plain else real(migration)

            executor = directory_setup.MigrationExecutor(alias)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes("addresses"))
            with mock.patch.object(directory_setup, "_remove_catalogue") as remove:
                with mock.patch.object(directory_setup.MigrationExecutor, "migration_plan", return_value=[*plan, (plain, False)]):
                    with mock.patch.object(directory_setup, "_changes_directory", side_effect=changes):
                        with mock.patch.object(directory_setup, "call_command"), mock.patch.object(
                            directory_setup, "_schema_matches", return_value=True
                        ):
                            try:
                                self.prepare()
                            except Exception:  # the mocked rebuild may stop anywhere after the decision
                                pass

            remove.assert_called_once()

    def test_the_schema_check_fails_when_a_unique_constraint_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            with directory_file(Path(folder) / "address-directory.sqlite3") as alias:
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, verbosity=0)
                self.assertTrue(directory_setup._schema_matches(alias))
                with alias.cursor() as cursor:
                    cursor.execute("DROP TABLE addresses_addressplace")
                    cursor.execute(
                        "CREATE TABLE addresses_addressplace (id integer NOT NULL PRIMARY KEY AUTOINCREMENT, "
                        "generation char(32) NOT NULL, postal_code varchar(4) NOT NULL, locality varchar(255) NOT NULL, "
                        "locality_key varchar(255) NOT NULL, easting real NULL, northing real NULL)"
                    )
                    cursor.execute("CREATE INDEX place_town ON addresses_addressplace (generation, locality_key, postal_code)")

                self.assertFalse(directory_setup._schema_matches(alias))

    def test_the_schema_check_fails_when_a_declared_index_or_a_table_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            with directory_file(Path(folder) / "address-directory.sqlite3") as alias:
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, verbosity=0)
                with alias.cursor() as cursor:
                    cursor.execute("DROP INDEX addr_house")
                self.assertFalse(directory_setup._schema_matches(alias))
            with directory_file(Path(folder) / "second.sqlite3") as alias:
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, verbosity=0)
                with alias.cursor() as cursor:
                    cursor.execute("DROP TABLE addresses_addressstreet")
                self.assertFalse(directory_setup._schema_matches(alias))

    def test_a_fresh_file_has_every_table_index_and_constraint_of_the_models(self):
        with tempfile.TemporaryDirectory() as folder:
            with directory_file(Path(folder) / "address-directory.sqlite3") as alias:
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, verbosity=0)

                self.assertTrue(directory_setup._schema_matches(alias))
                self.assertTrue(
                    {"place_postal_town", "place_town", "street_place", "street_postal", "street_town"}
                    <= self.indexes(alias, "addresses_addressplace") | self.indexes(alias, "addresses_addressstreet")
                )

    def test_the_models_with_a_unique_constraint_are_the_two_lookup_tables(self):
        for model, name in ((AddressPlace, "place_postal_town"), (AddressStreet, "street_place")):
            with self.subTest(model=model.__name__):
                self.assertIn(name, {c.name for c in model._meta.constraints if isinstance(c, models.UniqueConstraint)})

    def test_a_rebuilt_file_via_a_whole_migration_run_has_the_lookup_tables_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            with directory_file(Path(folder) / "address-directory.sqlite3"):
                call_command("migrate", "addresses", database=DIRECTORY_ALIAS, verbosity=0)

                self.assertEqual((AddressPlace.objects.count(), AddressStreet.objects.count()), (0, 0))

    def test_a_copy_of_the_file_can_be_checked_with_sqlite_directly(self):
        # What the operator does by hand (docs/OPERATIONS.md): open the file read-only and count the rows.
        with self.file_at_0019() as (path, alias, _generation):
            self.prepare()
            alias.close()
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                places = connection.execute("SELECT COUNT(*) FROM addresses_addressplace").fetchone()[0]
                addresses = connection.execute("SELECT COUNT(*) FROM addresses_address").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual((places, addresses), (2, 4))


class OpeningGuardTests(SimpleTestCase):
    """``storage.directory_open_guard``: the OS mutex of the directory file (D-075, owner walkthrough 2026-10-28)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "directory.sqlite3"

    def test_any_number_of_shared_holders_at_the_same_time(self):
        with directory_open_guard(self.path, shared=True):
            with directory_open_guard(self.path, shared=True):
                with directory_open_guard(self.path, shared=True):
                    pass

    def test_a_shared_holder_keeps_an_exclusive_one_out(self):
        with directory_open_guard(self.path, shared=True):
            with self.assertRaises(OSError):
                with directory_open_guard(self.path):
                    self.fail("The reset entered while a reader was inside.")

    def test_an_exclusive_holder_keeps_a_shared_one_out(self):
        with directory_open_guard(self.path):
            with self.assertRaises(OSError):
                with directory_open_guard(self.path, shared=True):
                    self.fail("A reader entered during the reset.")

    def test_two_exclusive_holders_exclude_each_other(self):
        with directory_open_guard(self.path):
            with self.assertRaises(OSError):
                with directory_open_guard(self.path):
                    self.fail("Two resets entered.")

    def test_the_lock_is_released_after_each_kind_and_after_a_failure(self):
        for shared in (True, False):
            with self.subTest(shared=shared):
                with directory_open_guard(self.path, shared=shared):
                    pass
                with directory_open_guard(self.path):
                    pass
                with self.assertRaises(RuntimeError):
                    with directory_open_guard(self.path, shared=shared):
                        raise RuntimeError("test failure")
                with directory_open_guard(self.path):
                    pass

    def test_the_lock_file_stays_and_is_a_private_regular_file(self):
        with directory_open_guard(self.path, shared=True):
            pass
        lock = Path(str(self.path) + ".open-lock")

        self.assertTrue(lock.is_file())
        self.assertEqual(lock.stat().st_size, 0)

    def test_a_failure_to_unlock_a_shared_lock_is_a_database_error(self):
        target = "_unlock_shared_windows" if os.name == "nt" else None
        if target is None:
            import fcntl

            patcher = mock.patch.object(fcntl, "flock", side_effect=[None, OSError("unlock failed")])
        else:
            patcher = mock.patch.object(storage, target, side_effect=OSError("unlock failed"))
        with patcher:
            with self.assertRaises(OperationalError):
                with directory_open_guard(self.path, shared=True):
                    pass
        # The descriptor was closed all the same: the lock can be taken again.
        with directory_open_guard(self.path):
            pass

    @skipUnless(os.name == "nt", "the ctypes prototypes are a Windows matter")
    def test_a_failure_to_unlock_an_exclusive_lock_is_a_database_error_on_windows(self):
        import msvcrt

        real = msvcrt.locking

        def locking(descriptor, mode, size):
            if mode == msvcrt.LK_UNLCK:
                raise OSError("unlock failed")
            return real(descriptor, mode, size)

        with mock.patch.object(msvcrt, "locking", side_effect=locking):
            with self.assertRaises(OperationalError):
                with directory_open_guard(self.path):
                    pass
        with directory_open_guard(self.path):
            pass

    @skipUnless(os.name == "nt", "the ctypes prototypes are a Windows matter")
    def test_the_windows_prototypes_are_defined_once_on_a_private_library(self):
        import ctypes

        kernel32, overlapped = storage._windows()

        self.assertIs(storage._windows()[0], kernel32)
        self.assertIsNot(kernel32, ctypes.windll.kernel32)
        self.assertEqual(len(kernel32.LockFileEx.argtypes), 6)
        self.assertEqual(len(kernel32.UnlockFileEx.argtypes), 5)
        self.assertTrue(issubclass(overlapped, ctypes.Structure))

    @skipUnless(os.name == "nt", "the ctypes prototypes are a Windows matter")
    def test_eight_threads_take_the_shared_guard_at_once_and_the_prototypes_are_built_once(self):
        import ctypes

        original = storage._windows_api
        storage._windows_api = None
        built = []
        real = ctypes.WinDLL

        def counting(*args, **kwargs):
            built.append(args)
            return real(*args, **kwargs)

        entered = threading.Barrier(8, timeout=20)
        errors = []

        def reader():
            try:
                with directory_open_guard(self.path, shared=True):
                    entered.wait()  # all eight hold the shared lock at the same time
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        try:
            with mock.patch.object(ctypes, "WinDLL", side_effect=counting):
                threads = [threading.Thread(target=reader) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
        finally:
            storage._windows_api = original

        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(built), 1)

    def test_a_symbolic_link_as_the_lock_file_is_refused(self):
        if os.name != "posix":
            self.skipTest("Creating symbolic links needs extra privileges on Windows.")
        target = Path(str(self.path) + ".target")
        target.write_bytes(b"protected")
        Path(str(self.path) + ".open-lock").symlink_to(target)

        with self.assertRaises(OSError):
            with directory_open_guard(self.path):
                pass
        self.assertEqual(target.read_bytes(), b"protected")

    def test_two_connections_open_the_file_at_the_same_time_through_the_shared_guard(self):
        # The backend takes the shared guard to open a connection and to run a statement: a slow request and a check overlap.
        from leg_platform.addresses.sqlite.base import DatabaseWrapper

        settings_dict = {
            **storage.directory_database(str(self.path)), "TIME_ZONE": None, "CONN_MAX_AGE": 0, "CONN_HEALTH_CHECKS": False,
            "AUTOCOMMIT": True, "ATOMIC_REQUESTS": False, "OPTIONS": {**storage.directory_database(str(self.path))["OPTIONS"]},
        }
        first = DatabaseWrapper(settings_dict, alias="first")
        second = DatabaseWrapper(settings_dict, alias="second")
        try:
            with first.cursor() as one, second.cursor() as two:
                one.execute("CREATE TABLE IF NOT EXISTS probe (value integer)")
                two.execute("SELECT COUNT(*) FROM probe")
                self.assertEqual(two.fetchone(), (0,))
        finally:
            first.close()
            second.close()
