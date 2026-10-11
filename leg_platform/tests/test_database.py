"""Tests of the MariaDB connection settings (D-002, D-028, D-030).

These tests need a MariaDB server and are tagged "mariadb". They run in continuous integration
against MariaDB 10.11 and 12.3. Without a server, exclude them:
``manage.py test --exclude-tag mariadb``.
"""

from django.db import connection
from django.test import TestCase, tag

# Minimum supported MariaDB version (D-030): the oldest long-term support release with
# community support, also the minimum of Django 6.1.
MINIMUM_MARIADB_VERSION = (10, 11)


@tag("mariadb")
class MariaDbConnectionTests(TestCase):
    def _variable(self, name):
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT @@{name}")
            return cursor.fetchone()[0]

    def test_backend_is_mariadb_in_a_supported_version(self):
        self.assertEqual(connection.vendor, "mysql")
        self.assertTrue(connection.mysql_is_mariadb)
        self.assertGreaterEqual(connection.mysql_version[:2], MINIMUM_MARIADB_VERSION)

    def test_connection_uses_utf8mb4(self):
        self.assertEqual(self._variable("character_set_connection"), "utf8mb4")
        self.assertEqual(self._variable("character_set_database"), "utf8mb4")
        self.assertEqual(self._variable("collation_database"), "utf8mb4_uca1400_ai_ci")

    def test_isolation_level_is_read_committed(self):
        # MariaDB 11.1 deprecated tx_isolation in favour of transaction_isolation; 10.11 only
        # has tx_isolation (MariaDB "Server System Variables").
        name = "transaction_isolation" if connection.mysql_version >= (11, 1) else "tx_isolation"
        self.assertEqual(self._variable(name), "READ-COMMITTED")

    def test_strict_mode_is_active(self):
        modes = set(self._variable("sql_mode").split(","))
        self.assertTrue(modes & {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"})
