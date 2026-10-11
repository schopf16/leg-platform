"""Tests of the MySQL connection settings (D-026, D-027).

These tests need a MySQL server and are tagged "mysql". They run in continuous integration
against MySQL 8.4 and 9.7. Without a server, exclude them: ``manage.py test --exclude-tag mysql``.
"""

from django.db import connection
from django.test import TestCase, tag


@tag("mysql")
class MySqlConnectionTests(TestCase):
    def _variable(self, name):
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT @@{name}")
            return cursor.fetchone()[0]

    def test_backend_is_mysql_in_a_supported_version(self):
        self.assertEqual(connection.vendor, "mysql")
        self.assertFalse(connection.mysql_is_mariadb)
        self.assertGreaterEqual(connection.mysql_version[:2], (8, 4))

    def test_connection_uses_utf8mb4(self):
        self.assertEqual(self._variable("character_set_connection"), "utf8mb4")
        self.assertEqual(self._variable("character_set_database"), "utf8mb4")

    def test_isolation_level_is_read_committed(self):
        self.assertEqual(self._variable("transaction_isolation"), "READ-COMMITTED")

    def test_strict_mode_is_active(self):
        modes = set(self._variable("sql_mode").split(","))
        self.assertTrue(modes & {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"})
