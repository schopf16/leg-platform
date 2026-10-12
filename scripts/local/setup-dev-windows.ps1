#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Sets up MariaDB and Python for local LEG-Platform development on Windows (D-027, D-030).

.DESCRIPTION
    For local development only. Never use this script for stage or production.

    1. Stops without changing anything if a local MySQL installation of an earlier version of
       this project, a service named MySQL or MariaDB, or a MariaDB installation exists. For
       the earlier MySQL installation it prints the commands for the manual removal.
    2. Asks for the new password of the local MariaDB 'root' accounts and validates it before
       anything is installed.
    3. Installs Python (default version: 3.14.8) with Chocolatey, unless it is installed.
    4. Installs MariaDB (Chocolatey package mariadb.install, default version 12.3.3, newest
       long-term support release) with the MSI property SKIPNETWORKING=1: the server first
       listens on no TCP port, only on the named pipe of its service.
    5. Verifies the installation: program folder below %ProgramFiles%, no folder or file
       there writable by ordinary users, no symbolic links or junctions, service command line
       with --defaults-file=<data directory>\my.ini, service account. Restricts the data
       directory to Administrators, SYSTEM and the service account.
    6. Over the named pipe (whose server process must be the service): removes anonymous
       accounts, root accounts other than localhost, 127.0.0.1 and ::1, and the 'test'
       database with its grants to everybody, sets the root password and verifies a login
       with it, then creates the database 'leg_platform' and the account
       'leg_platform'@'127.0.0.1' with only the privileges Django needs, on 'leg_platform'
       and on Django's test database 'test_leg_platform'.
    7. Switches the server to TCP on 127.0.0.1:3306 only and disables the named pipe (my.ini),
       verifies the listening sockets and the server variables.
    8. Stores the local configuration as user environment variables (DB_NAME, DB_USER,
       DB_PASSWORD, DB_HOST, DB_PORT, DJANGO_SECRET_KEY, APP_ENV=local). Generated values are
       never printed.

    Until the setup has succeeded, the service has the start type Manual. If a step after the
    installation fails or the script is interrupted (e.g. with Ctrl+C), the script stops the
    service, sets its start type to Disabled, verifies both and prints recovery instructions
    (docs/INSTALLATION.md). Closing the console window may end the script without this
    cleanup; check the service afterwards in that case.

    The root password is entered interactively and is not stored by this script; keep it in
    your password manager. Passwords are passed to the client via standard input or a
    temporary option file readable only by the current user, never on the command line.

.PARAMETER MariaDbVersion
    Chocolatey package version of mariadb.install to install.

.PARAMETER PythonVersion
    Chocolatey package version of the python314 package to install.

.PARAMETER RemoveFailedInstallation
    Removes the local MariaDB installation left behind by a failed run of this script before
    the normal setup starts, after a list of everything that will be removed and a typed
    confirmation (DELETE). All local MariaDB databases are lost. For local development
    machines only. Never removes the earlier MySQL installation (manual steps).
#>
[CmdletBinding()]
param(
    [string]$MariaDbVersion = '12.3.3',
    [string]$PythonVersion = '3.14.8',
    [switch]$RemoveFailedInstallation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Service name: the package mariadb.install passes SERVICENAME=MySQL to the MSI (its
# chocolateyInstall.ps1); the MSI default without the property is no service at all.
$ServiceName = 'MySQL'
$ReservedServiceNames = @('MySQL', 'MariaDB')
$DatabaseName = 'leg_platform'
$TestDatabaseName = 'test_leg_platform'
$AppUser = 'leg_platform'
$DatabasePort = 3306
$MinimumRootPasswordLength = 15
$MariaDbPackage = 'mariadb.install'
$MariaDbMetaPackage = 'mariadb'
$PythonPackage = 'python314'
$RetryCommand = '.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation'

# MSI property appended to the package's own MSI arguments (choco --install-arguments, which
# are "appended to the silentArgs in the package"). SKIPNETWORKING makes the MSI call
# mariadb-install-db.exe with --skip-networking, which writes skip-networking=ON,
# named-pipe=ON and socket=<service name> to my.ini (MariaDB sources win/packaging/extra.wxs.in
# and sql/mysql_install_db.cc). The root password is never passed to the MSI: it would appear
# in the command line and in Chocolatey's log.
$MsiInstallArguments = 'SKIPNETWORKING=1'

# Earlier local MySQL installation of this project (Chocolatey package 'mysql' with its
# default folders); never removed by this script, only reported with the manual commands.
$LegacyMySqlPackage = 'mysql'
$OldDataParent = 'C:\ProgramData\MySQL'
$OldDataDirectory = 'C:\ProgramData\MySQL\data'
# PATH entry that the earlier installation added for the current user (Install-ChocolateyPath).
$LegacyPathEntry = 'C:\tools\mysql\current\bin'

# Global option files that MariaDB programs read unless --defaults-file or --no-defaults is
# given (MariaDB documentation, "Configuring MariaDB with Option Files"). The service uses
# --defaults-file and the client calls of this script use --defaults-file or --no-defaults;
# the files are refused so that MariaDB tools started later without these options behave
# like the server.
$GlobalOptionFiles = @(
    (Join-Path $env:WINDIR 'my.ini'), (Join-Path $env:WINDIR 'my.cnf'), 'C:\my.ini', 'C:\my.cnf'
)

# Environment variables that change the behaviour of MariaDB client programs, the option
# files they read or the client plugins they load (MariaDB documentation "MariaDB Environment
# Variables" and "Configuring MariaDB with Option Files"; MariaDB Connector/C sources
# libmariadb/ma_default.c and ma_client_plugin.c.in), plus MySQL-only variables; removed from
# the script's process before the first client call.
$ClientEnvironmentVariables = @(
    'MYSQL_HOST', 'MYSQL_TCP_PORT', 'MYSQL_PWD', 'MYSQL_UNIX_PORT', 'MYSQL_HOME', 'MARIADB_HOME',
    'MYSQL_GROUP_SUFFIX', 'MYSQL_DEBUG', 'MYSQL_HISTFILE', 'MYSQL_PS1', 'MARIADB_PLUGIN_DIR',
    'LIBMYSQL_PLUGINS', 'LIBMYSQL_PLUGIN_DIR', 'LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN',
    'MYSQL_TEST_LOGIN_FILE'
)

# Accounts that may exist after the hardening (user@host as in mysql.global_priv): the root
# accounts that mariadb-secure-installation keeps (localhost, 127.0.0.1, ::1), the locked
# definer account mariadb.sys@localhost and the PUBLIC grant recipient (stored with an empty
# host). Sources: MariaDB sources scripts/mariadb_system_tables.sql,
# scripts/mariadb_system_tables_data.sql, scripts/mariadb_test_db.sql and
# scripts/mysql_secure_installation.sh (branch 12.3).
$ReservedAccounts = @('mariadb.sys@localhost', 'PUBLIC@', 'root@127.0.0.1', 'root@::1', 'root@localhost')

# Well-known SIDs (Microsoft Learn, "Security identifiers"); names are localised, SIDs are not.
$SidAdministrators = 'S-1-5-32-544'
$SidSystem = 'S-1-5-18'
$SidUsers = 'S-1-5-32-545'
$SidCreatorOwner = 'S-1-3-0'

# Rights that allow changing a file or directory, its attributes, permissions or owner.
$WriteRights = [Security.AccessControl.FileSystemRights](
    [int][Security.AccessControl.FileSystemRights]::WriteData -bor
    [int][Security.AccessControl.FileSystemRights]::AppendData -bor
    [int][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
    [int][Security.AccessControl.FileSystemRights]::WriteAttributes -bor
    [int][Security.AccessControl.FileSystemRights]::Delete -bor
    [int][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [int][Security.AccessControl.FileSystemRights]::TakeOwnership
)

# Privileges Django needs: data access, and schema changes by migrations (CREATE, DROP, ALTER,
# INDEX). On the test database, CREATE and DROP also allow the test runner to create and
# remove the database itself. MariaDB documents REFERENCES as "Unused" (GRANT statement), so
# it is not granted.
$AppPrivileges = 'SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX'

# Server option groups and client option groups of my.ini (MariaDB documentation,
# "Configuring MariaDB with Option Files"). client-server is read by both.
$ServerGroupPattern = '^(mysqld|server|mariadb|mariadbd|client-server|galera|embedded)(-[0-9][0-9.]*)?$'
$ClientGroupPattern = '^(client|client-server|client-mariadb|mysql|mariadb-client)$'
# Server options that decide where the server listens; removed from server groups before the
# block of this script is appended. Prefixes skip-, enable-, disable- and loose- are handled.
# shared-memory stays listed although MariaDB 12.3 has no such option (D-030): removing a
# leftover line is fail-safe, and an unknown option would stop the server from starting.
$ListenerOptionPattern = '^((skip|enable|disable)-)?(named-pipe|skip-networking|bind-address|port|socket|shared-memory|event-scheduler)$'

# DisplayName of MariaDB server products in the uninstall registry key: the MSI product name
# "MariaDB <major>.<minor>" (x86) or "MariaDB <major>.<minor> (<arch>)" (CPACK_WIX_PACKAGE_NAME
# in win/packaging/CMakeLists.txt of the MariaDB sources). Other MariaDB products (e.g.
# connectors) do not match.
$MariaDbProductPattern = '^MariaDB [0-9]+\.[0-9]+( \([A-Za-z0-9]+\))?$'
# Key name of the hidden uninstall entry that the MSI registers under its ProductCode.
$ProductCodePattern = '^\{[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}\z'

# Server variables checked after the switch to TCP and the restart, which also resets every
# runtime change (SET GLOBAL) made during the phase without a root password (MariaDB has no
# persisted system variables): bind_address|skip_networking|named_pipe|port|feedback plugin|
# shared_memory|general_log|slow_query_log|log_output|general_log_file|slow_query_log_file|
# event_scheduler (default OFF; set to DISABLED in my.ini, which cannot be changed at runtime).
# Defaults (MariaDB documentation, "Server System Variables"): general_log 0, slow_query_log 0,
# log_output FILE, general_log_file host_name.log. Log files must be plain file names (in the
# data directory). MariaDB 12.3 has no shared_memory variable (sql/sys_vars.cc, D-030), and
# @@GLOBAL.shared_memory fails there with ERROR 1193; it is therefore read from
# information_schema.GLOBAL_VARIABLES (upper-case names, booleans as ON/OFF: sql/sql_show.cc)
# and reported as ABSENT if missing. Only OFF and ABSENT are accepted.
$ServerCheckSql = "SELECT CONCAT(COALESCE(@@GLOBAL.bind_address, 'NULL'), '|', @@GLOBAL.skip_networking, '|', @@GLOBAL.named_pipe, '|', @@GLOBAL.port, '|', COALESCE((SELECT UPPER(PLUGIN_STATUS) FROM information_schema.PLUGINS WHERE UPPER(PLUGIN_NAME) = 'FEEDBACK'), 'NONE'), '|', COALESCE((SELECT COALESCE(VARIABLE_VALUE, 'NULL') FROM information_schema.GLOBAL_VARIABLES WHERE VARIABLE_NAME = 'SHARED_MEMORY'), 'ABSENT'), '|', @@GLOBAL.general_log, '|', @@GLOBAL.slow_query_log, '|', @@GLOBAL.log_output, '|', COALESCE(@@GLOBAL.general_log_file, ''), '|', COALESCE(@@GLOBAL.slow_query_log_file, ''), '|', @@GLOBAL.event_scheduler);"

# Other client connections: every thread except the current one and background threads, which
# MariaDB shows with COMMAND 'Daemon' (MariaDB documentation, "Information Schema PROCESSLIST
# Table": example row "system user", COMMAND "Daemon").
$OtherConnectionsSql = "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() AND COMMAND <> 'Daemon';"

# Checks after the hardening and before the application account is created; each returns one
# line per problem. Expected state from the bootstrap SQL of the MariaDB sources (branch 12.3:
# scripts/mariadb_system_tables.sql, mariadb_system_tables_data.sql, mariadb_test_db.sql,
# sys_schema) after the hardening: system schemas only; no routines, events, triggers or views
# (only the bootstrap routines and views, $BootstrapObjects; no events or triggers at all);
# mysql.db (only the application grants), columns_priv, procs_priv, roles_mapping, plugin, func and
# servers empty (the Windows bootstrap installs no plugin: auth_root_socket is not set); tables_priv
# only mariadb.sys@localhost with Select,Delete on mysql.global_priv; proxies_priv only the
# root accounts kept; mariadb.sys locked; PUBLIC without global privileges; no account without
# a password; no other client connection; general log off; shared memory off if the server has
# that variable (see $ServerCheckSql). MariaDB stores
# the account data as JSON in mysql.global_priv.Priv (authentication_string, account_locked,
# access).
$AccountListSql = "SELECT CONCAT(User, '@', Host) FROM mysql.global_priv ORDER BY 1;"
$HardeningProblemSql = @'
SELECT CONCAT('account without password: ', User, '@', Host) FROM mysql.global_priv WHERE User NOT IN ('mariadb.sys', 'PUBLIC') AND COALESCE(JSON_VALUE(Priv, '$.authentication_string'), '') = '';
SELECT CONCAT('unexpected authentication: ', User, '@', Host) FROM mysql.global_priv WHERE User NOT IN ('mariadb.sys', 'PUBLIC') AND (COALESCE(JSON_VALUE(Priv, '$.plugin'), '') <> 'mysql_native_password' OR JSON_EXISTS(Priv, '$.auth_or') = 1);
SELECT 'root accounts have different passwords' FROM mysql.global_priv WHERE User = 'root' HAVING COUNT(DISTINCT JSON_VALUE(Priv, '$.authentication_string')) <> 1;
SELECT 'mariadb.sys is not locked' FROM mysql.global_priv WHERE User = 'mariadb.sys' AND Host = 'localhost' AND (COALESCE(JSON_VALUE(Priv, '$.account_locked'), '0') <> '1' OR COALESCE(JSON_TYPE(JSON_EXTRACT(Priv, '$.account_locked')), '') <> 'BOOLEAN');
SELECT CONCAT('global privileges: ', User, '@', Host) FROM mysql.global_priv WHERE User <> 'root' AND COALESCE(JSON_VALUE(Priv, '$.access'), '0') <> '0';
SELECT CONCAT('database grant: ', User, '@', Host, ' on ', Db) FROM mysql.db WHERE NOT (User = 'leg_platform' AND Host = '127.0.0.1' AND Db IN ('leg\\_platform', 'test\\_leg\\_platform'));
SELECT CONCAT('table grant: ', User, '@', Host, ' on ', Db, '.', Table_name) FROM mysql.tables_priv WHERE NOT (Host = 'localhost' AND User = 'mariadb.sys' AND Db = 'mysql' AND Table_name = 'global_priv' AND Table_priv = 'Select,Delete' AND Column_priv = '');
SELECT CONCAT('column grant: ', User, '@', Host) FROM mysql.columns_priv;
SELECT CONCAT('routine grant: ', User, '@', Host) FROM mysql.procs_priv;
SELECT CONCAT('role mapping: ', User, '@', Host, ' ', Role) FROM mysql.roles_mapping;
SELECT CONCAT('proxy grant: ', User, '@', Host, ' for ', Proxied_user, '@', Proxied_host) FROM mysql.proxies_priv WHERE NOT (User = 'root' AND Host IN ('localhost', '127.0.0.1', '::1') AND Proxied_user = '' AND Proxied_host = '');
SELECT CONCAT('installed plugin: ', name) FROM mysql.plugin;
SELECT CONCAT('user-defined function: ', name) FROM mysql.func;
SELECT CONCAT('federated server: ', Server_name) FROM mysql.servers;
SELECT CONCAT('schema: ', SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys', 'leg_platform');
SELECT CONCAT('event: ', EVENT_SCHEMA, '.', EVENT_NAME) FROM information_schema.EVENTS;
SELECT CONCAT('trigger: ', TRIGGER_SCHEMA, '.', TRIGGER_NAME) FROM information_schema.TRIGGERS;
SELECT CONCAT('event scheduler is ', @@GLOBAL.event_scheduler) FROM DUAL WHERE @@GLOBAL.event_scheduler NOT IN ('OFF', 'DISABLED');
SELECT CONCAT('other client connection: ', ID, ' ', USER, '@', HOST) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() AND COMMAND <> 'Daemon';
SELECT 'general log is on' FROM DUAL WHERE @@GLOBAL.general_log <> 0;
SELECT 'shared memory is on' FROM information_schema.GLOBAL_VARIABLES WHERE VARIABLE_NAME = 'SHARED_MEMORY' AND (VARIABLE_VALUE IS NULL OR VARIABLE_VALUE <> 'OFF');
'@

# Stored routines and views of all schemas (schema:type:name:definer), compared with
# $BootstrapObjects by Get-UnexpectedObjects.
$StoredObjectSql = @'
SELECT CONCAT(ROUTINE_SCHEMA, ':', ROUTINE_TYPE, ':', ROUTINE_NAME, ':', DEFINER) FROM information_schema.ROUTINES;
SELECT CONCAT(TABLE_SCHEMA, ':VIEW:', TABLE_NAME, ':', DEFINER) FROM information_schema.VIEWS;
'@

# Routines and views that the Windows bootstrap of MariaDB 12.3.3 creates (schema:type:name),
# all with the definer mariadb.sys@localhost: the view mysql.user
# (scripts/mariadb_system_tables.sql) and the sys schema (the files listed in
# scripts/sys_schema/CMakeLists.txt; its trigger files only drop triggers). Extracted from the
# sources at the tag mariadb-12.3.3 on 2026-10-12. The bootstrap creates no events and no
# triggers.
$BootstrapObjectDefiner = 'mariadb.sys@localhost'
$BootstrapObjects = @'
mysql:VIEW:user
sys:FUNCTION:extract_schema_from_file_name
sys:FUNCTION:extract_table_from_file_name
sys:FUNCTION:format_bytes
sys:FUNCTION:format_path
sys:FUNCTION:format_statement
sys:FUNCTION:format_time
sys:FUNCTION:list_add
sys:FUNCTION:list_drop
sys:FUNCTION:ps_is_account_enabled
sys:FUNCTION:ps_is_consumer_enabled
sys:FUNCTION:ps_is_instrument_default_enabled
sys:FUNCTION:ps_is_instrument_default_timed
sys:FUNCTION:ps_is_thread_instrumented
sys:FUNCTION:ps_thread_account
sys:FUNCTION:ps_thread_id
sys:FUNCTION:ps_thread_stack
sys:FUNCTION:ps_thread_trx_info
sys:FUNCTION:quote_identifier
sys:FUNCTION:sys_get_config
sys:FUNCTION:version_major
sys:FUNCTION:version_minor
sys:FUNCTION:version_patch
sys:PROCEDURE:create_synonym_db
sys:PROCEDURE:diagnostics
sys:PROCEDURE:execute_prepared_stmt
sys:PROCEDURE:optimizer_switch_choice
sys:PROCEDURE:optimizer_switch_off
sys:PROCEDURE:optimizer_switch_on
sys:PROCEDURE:ps_setup_disable_background_threads
sys:PROCEDURE:ps_setup_disable_consumer
sys:PROCEDURE:ps_setup_disable_instrument
sys:PROCEDURE:ps_setup_disable_thread
sys:PROCEDURE:ps_setup_enable_background_threads
sys:PROCEDURE:ps_setup_enable_consumer
sys:PROCEDURE:ps_setup_enable_instrument
sys:PROCEDURE:ps_setup_enable_thread
sys:PROCEDURE:ps_setup_reload_saved
sys:PROCEDURE:ps_setup_reset_to_default
sys:PROCEDURE:ps_setup_save
sys:PROCEDURE:ps_setup_show_disabled
sys:PROCEDURE:ps_setup_show_disabled_consumers
sys:PROCEDURE:ps_setup_show_disabled_instruments
sys:PROCEDURE:ps_setup_show_enabled
sys:PROCEDURE:ps_setup_show_enabled_consumers
sys:PROCEDURE:ps_setup_show_enabled_instruments
sys:PROCEDURE:ps_statement_avg_latency_histogram
sys:PROCEDURE:ps_trace_statement_digest
sys:PROCEDURE:ps_trace_thread
sys:PROCEDURE:ps_truncate_all_tables
sys:PROCEDURE:statement_performance_analyzer
sys:PROCEDURE:table_exists
sys:VIEW:host_summary
sys:VIEW:host_summary_by_file_io
sys:VIEW:host_summary_by_file_io_type
sys:VIEW:host_summary_by_stages
sys:VIEW:host_summary_by_statement_latency
sys:VIEW:host_summary_by_statement_type
sys:VIEW:innodb_buffer_stats_by_schema
sys:VIEW:innodb_buffer_stats_by_table
sys:VIEW:innodb_lock_waits
sys:VIEW:io_by_thread_by_latency
sys:VIEW:io_global_by_file_by_bytes
sys:VIEW:io_global_by_file_by_latency
sys:VIEW:io_global_by_wait_by_bytes
sys:VIEW:io_global_by_wait_by_latency
sys:VIEW:latest_file_io
sys:VIEW:memory_by_host_by_current_bytes
sys:VIEW:memory_by_thread_by_current_bytes
sys:VIEW:memory_by_user_by_current_bytes
sys:VIEW:memory_global_by_current_bytes
sys:VIEW:memory_global_total
sys:VIEW:metrics
sys:VIEW:privileges_by_table_by_level
sys:VIEW:processlist
sys:VIEW:ps_check_lost_instrumentation
sys:VIEW:schema_auto_increment_columns
sys:VIEW:schema_index_statistics
sys:VIEW:schema_object_overview
sys:VIEW:schema_redundant_indexes
sys:VIEW:schema_table_lock_waits
sys:VIEW:schema_table_statistics
sys:VIEW:schema_table_statistics_with_buffer
sys:VIEW:schema_tables_with_full_table_scans
sys:VIEW:schema_unused_indexes
sys:VIEW:session
sys:VIEW:session_ssl_status
sys:VIEW:statement_analysis
sys:VIEW:statements_with_errors_or_warnings
sys:VIEW:statements_with_full_table_scans
sys:VIEW:statements_with_runtimes_in_95th_percentile
sys:VIEW:statements_with_sorting
sys:VIEW:statements_with_temp_tables
sys:VIEW:user_summary
sys:VIEW:user_summary_by_file_io
sys:VIEW:user_summary_by_file_io_type
sys:VIEW:user_summary_by_stages
sys:VIEW:user_summary_by_statement_latency
sys:VIEW:user_summary_by_statement_type
sys:VIEW:version
sys:VIEW:wait_classes_global_by_avg_latency
sys:VIEW:wait_classes_global_by_latency
sys:VIEW:waits_by_host_by_latency
sys:VIEW:waits_by_user_by_latency
sys:VIEW:waits_global_by_latency
sys:VIEW:x$host_summary
sys:VIEW:x$host_summary_by_file_io
sys:VIEW:x$host_summary_by_file_io_type
sys:VIEW:x$host_summary_by_stages
sys:VIEW:x$host_summary_by_statement_latency
sys:VIEW:x$host_summary_by_statement_type
sys:VIEW:x$innodb_buffer_stats_by_schema
sys:VIEW:x$innodb_buffer_stats_by_table
sys:VIEW:x$innodb_lock_waits
sys:VIEW:x$io_by_thread_by_latency
sys:VIEW:x$io_global_by_file_by_bytes
sys:VIEW:x$io_global_by_file_by_latency
sys:VIEW:x$io_global_by_wait_by_bytes
sys:VIEW:x$io_global_by_wait_by_latency
sys:VIEW:x$latest_file_io
sys:VIEW:x$memory_by_host_by_current_bytes
sys:VIEW:x$memory_by_thread_by_current_bytes
sys:VIEW:x$memory_by_user_by_current_bytes
sys:VIEW:x$memory_global_by_current_bytes
sys:VIEW:x$memory_global_total
sys:VIEW:x$processlist
sys:VIEW:x$ps_digest_95th_percentile_by_avg_us
sys:VIEW:x$ps_digest_avg_latency_distribution
sys:VIEW:x$ps_schema_table_statistics_io
sys:VIEW:x$schema_flattened_keys
sys:VIEW:x$schema_index_statistics
sys:VIEW:x$schema_table_lock_waits
sys:VIEW:x$schema_table_statistics
sys:VIEW:x$schema_table_statistics_with_buffer
sys:VIEW:x$schema_tables_with_full_table_scans
sys:VIEW:x$session
sys:VIEW:x$statement_analysis
sys:VIEW:x$statements_with_errors_or_warnings
sys:VIEW:x$statements_with_full_table_scans
sys:VIEW:x$statements_with_runtimes_in_95th_percentile
sys:VIEW:x$statements_with_sorting
sys:VIEW:x$statements_with_temp_tables
sys:VIEW:x$user_summary
sys:VIEW:x$user_summary_by_file_io
sys:VIEW:x$user_summary_by_file_io_type
sys:VIEW:x$user_summary_by_stages
sys:VIEW:x$user_summary_by_statement_latency
sys:VIEW:x$user_summary_by_statement_type
sys:VIEW:x$wait_classes_global_by_avg_latency
sys:VIEW:x$wait_classes_global_by_latency
sys:VIEW:x$waits_by_host_by_latency
sys:VIEW:x$waits_by_user_by_latency
sys:VIEW:x$waits_global_by_latency
'@ -split "`r?`n" | Where-Object { $_ }

# The grants must be stored exactly with the escaped names; result "<matching>|<all>".
$AppGrantCheckSql = "SELECT CONCAT(SUM(Db IN ('$($DatabaseName.Replace('_', '\\_'))', '$($TestDatabaseName.Replace('_', '\\_'))')), '|', COUNT(*)) FROM mysql.db WHERE User = '$AppUser';"

function New-RandomSecret {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    # URL-safe Base64 without padding: no quotes or backslashes that would need escaping in SQL.
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function ConvertFrom-SecureStringToPlain {
    param([Security.SecureString]$Value)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Get-RootPasswordProblem {
    # Returns a description of the problem, or $null if the password is acceptable.
    # The password is written into an SQL string literal and into an option file. Only
    # printable ASCII is accepted, without characters that have a meaning there: quotes and
    # backslashes (SQL literal, option file quoting and escape sequences), '#' and ';'
    # (comments). Leading and trailing spaces are refused as well (MariaDB documentation,
    # "Configuring MariaDB with Option Files").
    param([string]$Password)
    if ($Password.Length -lt $MinimumRootPasswordLength) {
        return "Use at least $MinimumRootPasswordLength characters."
    }
    if ($Password -cnotmatch '^[\x20-\x7E]+$') {
        return 'Use only printable ASCII characters (no umlauts or other non-ASCII characters).'
    }
    if ($Password -match '[#"''\\;]') {
        return 'The password must not contain # " '' \ or ;.'
    }
    if ($Password -ne $Password.Trim()) {
        return 'The password must not start or end with a space.'
    }
    return $null
}

function Read-RootPassword {
    while ($true) {
        $first = ConvertFrom-SecureStringToPlain (Read-Host -AsSecureString 'New password for the local MariaDB root accounts')
        $second = ConvertFrom-SecureStringToPlain (Read-Host -AsSecureString 'Repeat the password')
        if ($first -cne $second) {
            Write-Warning 'The passwords do not match. Please try again.'
            continue
        }
        $problem = Get-RootPasswordProblem -Password $first
        if ($problem) {
            Write-Warning "$problem Please try again."
            continue
        }
        return $first
    }
}

function Get-ClientArguments {
    # Arguments of the mariadb client. --defaults-file ("Only read default options from the
    # given file") or --no-defaults must be given as the first option (MariaDB documentation,
    # "mariadb Command-Line Client"). Protocol, pipe name or host and port are given on the
    # command line, which takes precedence over option files and environment variables
    # ("Environment variables have the lowest precedence", "MariaDB Environment Variables").
    # MariaDB does not read MySQL's .mylogin.cnf ("MariaDB ... do[es] not support this feature").
    param(
        [ValidateSet('Pipe', 'Tcp')][string]$Protocol,
        [string]$PipeName,
        [string]$User,
        [string]$OptionFile
    )
    $arguments = @()
    if ($OptionFile) { $arguments += "--defaults-file=$OptionFile" } else { $arguments += '--no-defaults' }
    if ($Protocol -eq 'Pipe') {
        if ($PipeName -cnotmatch '^[A-Za-z0-9_.-]+$') { throw "Invalid pipe name '$PipeName'." }
        $arguments += @('--protocol=PIPE', "--socket=$PipeName")
    } else {
        $arguments += @('--protocol=TCP', '--host=127.0.0.1', "--port=$DatabasePort")
    }
    $arguments += @("--user=$User", '--batch', '--skip-column-names')
    return $arguments
}

function New-AdministratorsFileSecurity {
    # Security descriptor of a file written by this script: no inheritance, full control for
    # Administrators and SYSTEM only, owner Administrators.
    $security = New-Object Security.AccessControl.FileSecurity
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($SidAdministrators, $SidSystem)) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, [Security.AccessControl.FileSystemRights]::FullControl, 'Allow')))
    }
    $security.SetOwner((New-Object Security.Principal.SecurityIdentifier($SidAdministrators)))
    return $security
}

function New-ProtectedTemporaryFile {
    # Creates a new file with an unpredictable name in the temporary folder, with its security
    # descriptor set at creation (FileSystemAclExtensions.Create in .NET, the FileStream
    # constructor with FileSecurity in .NET Framework; Microsoft Learn), and writes $Content as
    # ASCII. FileMode.CreateNew fails if the file exists. Needs an elevated process (owner
    # Administrators). Returns the path.
    param([string]$Content)
    $path = Join-Path ([IO.Path]::GetTempPath()) ('leg-setup-' + [Guid]::NewGuid().ToString('N') + '.cnf')
    $security = New-AdministratorsFileSecurity
    $rights = [Security.AccessControl.FileSystemRights]::FullControl
    $extensions = 'System.IO.FileSystemAclExtensions' -as [type]
    if ($extensions) {
        $stream = $extensions::Create((New-Object IO.FileInfo($path)), [IO.FileMode]::CreateNew, $rights, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security)
    } else {
        $stream = New-Object IO.FileStream($path, [IO.FileMode]::CreateNew, $rights, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security)
    }
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        $stream.Dispose()
    }
    return $path
}

function Invoke-MariaDb {
    # Runs SQL with the mariadb client. With -Password, the password is passed through a
    # temporary option file that only Administrators and SYSTEM can read (created with its
    # permissions set), so that it appears neither on the command line nor in the process
    # list. Without -Password, no password is sent.
    param(
        [string]$Client,
        [string]$Sql,
        [ValidateSet('Pipe', 'Tcp')][string]$Protocol,
        [string]$PipeName,
        [string]$User = 'root',
        [string]$Password
    )
    if ($Password) {
        # The passwords contain only printable ASCII (Get-RootPasswordProblem, New-RandomSecret).
        $optionFile = New-ProtectedTemporaryFile -Content "[client]`npassword=$Password`n"
        try {
            $arguments = Get-ClientArguments -Protocol $Protocol -PipeName $PipeName -User $User -OptionFile $optionFile
            $Sql | & $Client @arguments
        } finally {
            Remove-Item -LiteralPath $optionFile -Force -ErrorAction SilentlyContinue
        }
    } else {
        $arguments = Get-ClientArguments -Protocol $Protocol -PipeName $PipeName -User $User
        $Sql | & $Client @arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "The mariadb client exited with code $LASTEXITCODE." }
}

function Clear-ClientEnvironment {
    # Removes the variables in $ClientEnvironmentVariables from this script's process only;
    # their values are never printed. The user and machine environment is not changed.
    foreach ($name in $ClientEnvironmentVariables) {
        Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
    }
}

function Test-IsFile {
    param([string]$Path)
    try { return [bool](Test-Path -LiteralPath $Path -PathType Leaf) } catch { return $false }
}

function Resolve-ServiceExecutablePath {
    # Returns the existing executable that a service command line (Win32_Service.PathName)
    # starts, or $null. A quoted path is taken as it is (mariadb-install-db.exe registers
    # "<bin>\mysqld.exe" "--defaults-file=<datadir>\my.ini" "<service>", sql/mysql_install_db.cc).
    # For an unquoted path (e.g. the earlier MySQL package: C:\tools\mysql\current\bin\mysqld
    # MySQL), the white-space separated prefixes are tried from the shortest to the longest,
    # the order Microsoft documents for CreateProcess (Microsoft Learn, "CreateProcessW
    # function"). For a candidate without an extension, the path itself and then the path with
    # ".exe" appended are tried. Only existing files are returned.
    param([string]$PathName)
    if (-not $PathName) { return $null }
    $trimmed = $PathName.Trim()
    $candidates = @()
    if ($trimmed.StartsWith('"')) {
        $end = $trimmed.IndexOf('"', 1)
        if ($end -lt 2) { return $null }
        $candidates += $trimmed.Substring(1, $end - 1)
    } else {
        $tokens = @($trimmed -split '\s+')
        for ($count = 1; $count -le $tokens.Count; $count++) {
            $candidates += ($tokens[0..($count - 1)] -join ' ')
        }
    }
    foreach ($candidate in $candidates) {
        $paths = @($candidate)
        try {
            $extension = [IO.Path]::GetExtension($candidate)
        } catch {
            # Characters that are invalid in paths: not an executable path.
            continue
        }
        if (-not $extension) { $paths += "$candidate.exe" }
        foreach ($path in $paths) {
            if (Test-IsFile -Path $path) { return $path }
        }
    }
    return $null
}

function Get-DefaultsFileFromCommandLine {
    # Returns the value of --defaults-file in a service command line, or $null. Handles
    # "--defaults-file=C:\a b\my.ini" (mariadb-install-db.exe), --defaults-file="C:\a b\my.ini"
    # and --defaults-file=C:\a\my.ini.
    param([string]$PathName)
    foreach ($pattern in @('"--defaults-file=([^"]+)"', '--defaults-file="([^"]+)"', '--defaults-file=([^\s"]+)')) {
        $match = [regex]::Match("$PathName", $pattern)
        if ($match.Success) { return $match.Groups[1].Value }
    }
    return $null
}

function Read-TextFile {
    # Reads a text file and returns its text together with the encoding and line break, so
    # that it can be written back unchanged apart from the edited lines. A file without a byte
    # order mark must be valid UTF-8 (ASCII is a subset); otherwise the script stops instead of
    # guessing a code page and damaging the file.
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    $encoding = $null
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $encoding = New-Object Text.UTF8Encoding($true, $true)
        $offset = 3
    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $encoding = New-Object Text.UnicodeEncoding($false, $true, $true)
        $offset = 2
    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
        $encoding = New-Object Text.UnicodeEncoding($true, $true, $true)
        $offset = 2
    } else {
        $encoding = $strictUtf8
    }
    try {
        $text = $encoding.GetString($bytes, $offset, $bytes.Length - $offset)
    } catch {
        # Strict decoders throw on invalid byte sequences.
        throw "$Path is not valid UTF-8 or UTF-16; edit it manually (see docs/INSTALLATION.md)."
    }
    $lineBreak = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    return [pscustomobject]@{ Text = $text; Encoding = $encoding; LineBreak = $lineBreak }
}

function ConvertTo-OptionKey {
    # Normalises an option name: lower case, "_" as "-" ("Dashes (-) and underscores (_) in
    # option names are interchangeable"), without the prefix "loose-" (MariaDB documentation,
    # "Configuring MariaDB with Option Files" and "mariadbd Options").
    param([string]$Name)
    $key = $Name.Trim().ToLowerInvariant().Replace('_', '-')
    if ($key.StartsWith('loose-')) { $key = $key.Substring(6) }
    return $key
}

function Get-OptionFileEntries {
    # Parses option file text into entries (line index, group, normalised key, value).
    # Comment lines (# or ;) and empty lines are skipped. !include, !includedir and
    # ?includedir lines are returned with the key '!include'. A value in double quotes is
    # unquoted; escape sequences are not interpreted.
    param([string]$Text)
    $entries = @()
    $group = ''
    $lines = $Text -split "`r?`n"
    for ($index = 0; $index -lt $lines.Count; $index++) {
        $line = $lines[$index].Trim()
        if (-not $line -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
        $header = [regex]::Match($line, '^\[([^\]]+)\]$')
        if ($header.Success) {
            $group = $header.Groups[1].Value.Trim().ToLowerInvariant()
            continue
        }
        if ($line -match '^[!?]include') {
            $entries += [pscustomobject]@{ Index = $index; Group = $group; Key = '!include'; Value = $line }
            continue
        }
        $separator = $line.IndexOf('=')
        if ($separator -ge 0) {
            $name = $line.Substring(0, $separator)
            $value = $line.Substring($separator + 1).Trim()
            if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        } else {
            $name = $line
            $value = $null
        }
        $entries += [pscustomobject]@{ Index = $index; Group = $group; Key = (ConvertTo-OptionKey -Name $name); Value = $value }
    }
    return $entries
}

function Get-ServerOptionValue {
    # Returns the value of the last occurrence of an option in a server group ("If multiple
    # instances of a given option are found, the last instance takes precedence"), '' for an
    # option without value, or $null if absent.
    param($Entries, [string]$Key)
    $found = @($Entries | Where-Object { $_.Group -match $ServerGroupPattern -and $_.Key -eq $Key })
    if ($found.Count -eq 0) { return $null }
    $value = $found[$found.Count - 1].Value
    if ($null -eq $value) { return '' }
    return $value
}

function Test-IsOptionOn {
    # Boolean option values: an option without value, 1 or ON enable it.
    # Not typed [string]: PowerShell would convert $null (option absent) to ''.
    param($Value)
    if ($null -eq $Value) { return $false }
    return "$Value" -eq '' -or "$Value" -in @('1', 'ON', 'TRUE')
}

function Get-InitialOptionFileProblems {
    # Checks my.ini as written by the installation: no include directives, the server starts
    # without networking on a named pipe, the data directory is the folder of my.ini, and the
    # feedback plugin (which sends usage data to mariadb.org) is not enabled.
    param($Entries, [string]$DataDirectory)
    $problems = @()
    if (@($Entries | Where-Object { $_.Key -eq '!include' }).Count -gt 0) {
        $problems += 'my.ini contains include directives'
    }
    if (-not (Test-IsOptionOn -Value (Get-ServerOptionValue -Entries $Entries -Key 'skip-networking'))) {
        $problems += 'skip-networking is not set (the MSI property SKIPNETWORKING did not take effect)'
    }
    $socket = Get-ServerOptionValue -Entries $Entries -Key 'socket'
    if ($socket -cne $ServiceName) {
        $problems += "the named pipe is '$socket', not '$ServiceName'"
    }
    $dataDir = Get-ServerOptionValue -Entries $Entries -Key 'datadir'
    if (-not (Test-IsSamePath -First $dataDir -Second $DataDirectory)) {
        $problems += "datadir is '$dataDir', not $DataDirectory"
    }
    if (Test-IsOptionOn -Value (Get-ServerOptionValue -Entries $Entries -Key 'shared-memory')) {
        $problems += 'shared memory connections are enabled'
    }
    $feedback = Get-ServerOptionValue -Entries $Entries -Key 'feedback'
    if ($null -ne $feedback -and ($feedback -eq '' -or $feedback -notin @('0', 'OFF', 'off', 'Off'))) {
        $problems += 'the feedback plugin is enabled'
    }
    return $problems
}

function Get-NetworkOptionFileText {
    # Returns my.ini switched to TCP on the loopback interface: every option that decides where
    # the server listens is removed from the server groups, protocol and socket from the client
    # groups, and a [mysqld] block with the final values is appended, which also wins over any
    # remaining occurrence ("the last instance takes precedence"). All other lines are kept.
    param([string]$Text, [string]$LineBreak)
    $remove = @{}
    foreach ($entry in Get-OptionFileEntries -Text $Text) {
        if ($entry.Group -match $ServerGroupPattern -and $entry.Key -match $ListenerOptionPattern) { $remove[$entry.Index] = $true }
        if ($entry.Group -match $ClientGroupPattern -and $entry.Key -in @('protocol', 'socket')) { $remove[$entry.Index] = $true }
    }
    $lines = @($Text -split "`r?`n")
    $kept = @(for ($index = 0; $index -lt $lines.Count; $index++) { if (-not $remove.ContainsKey($index)) { $lines[$index] } })
    while ($kept.Count -gt 0 -and $kept[$kept.Count - 1].Trim() -eq '') { $kept = @($kept | Select-Object -First ($kept.Count - 1)) }
    $kept += @(
        '',
        '[mysqld]',
        '# Added by scripts/local/setup-dev-windows.ps1 (D-030): TCP on 127.0.0.1 only, no named pipe.',
        "port=$DatabasePort",
        'bind-address=127.0.0.1',
        'skip-networking=0',
        'named-pipe=OFF',
        'event-scheduler=DISABLED',
        ''
    )
    return ($kept -join $LineBreak)
}

function Get-ServiceInfo {
    param([string]$Name)
    return Get-CimInstance Win32_Service -Filter "Name='$Name'"
}

function Get-ServiceAccountSid {
    # The service account: LocalSystem or the virtual account NT SERVICE\<service>, which
    # mariadb-install-db.exe uses ("Run service under virtual account NT SERVICE\service_name",
    # sql/mysql_install_db.cc). Any other account is refused.
    param([string]$StartName, [string]$Service)
    if ($StartName -ieq 'LocalSystem') { return $SidSystem }
    if ($StartName -ieq "NT SERVICE\$Service") {
        $account = New-Object Security.Principal.NTAccount('NT SERVICE', $Service)
        return $account.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    throw "The service '$Service' runs as '$StartName'; expected NT SERVICE\$Service."
}

function Get-TrustedInstallerSid {
    $account = New-Object Security.Principal.NTAccount('NT SERVICE', 'TrustedInstaller')
    return $account.Translate([Security.Principal.SecurityIdentifier]).Value
}

function Get-MariaDbUninstallEntries {
    # Installed MariaDB server products (DisplayName matching $MariaDbProductPattern) from the uninstall registry keys of
    # both registry views, with their InstallLocation.
    return @(Select-MariaDbUninstallEntries -Entries @(Get-MariaDbUninstallRegistryEntries))
}

function Select-MariaDbUninstallEntries {
    # Besides one visible entry per product name, the MSI writes a hidden ProductCode entry (ARPSYSTEMCOMPONENT=1,
    # win/packaging/extra.wxs.in, branch 12.3); it is skipped only if a visible entry has the same name and location.
    param([object[]]$Entries)
    $visible = @($Entries | Where-Object { -not $_.Hidden })
    # Each visible entry explains at most one hidden entry.
    $absorbed = @{}
    $selected = @()
    foreach ($entry in $Entries) {
        if ($entry.Hidden -and $entry.InstallLocation) {
            $match = $null
            for ($i = 0; $i -lt $visible.Count; $i++) {
                if (-not $absorbed.ContainsKey($i) -and $visible[$i].DisplayName -ceq $entry.DisplayName -and
                    (Test-IsSamePath -First $visible[$i].InstallLocation -Second $entry.InstallLocation)) { $match = $i; break }
            }
            if ($null -ne $match) { $absorbed[$match] = $true; continue }
        }
        $selected += [pscustomobject]@{ DisplayName = $entry.DisplayName; InstallLocation = $entry.InstallLocation }
    }
    return $selected
}

function Select-MariaDbProductCode {
    # ProductCode of the installed MSI: the key name of the one hidden uninstall entry with the given DisplayName
    # and InstallLocation (win/packaging/extra.wxs.in, branch 12.3). Throws unless exactly one entry matches.
    param([object[]]$Entries, [string]$DisplayName, [string]$InstallLocation)
    $matching = @($Entries | Where-Object {
        $_.Hidden -and "$($_.KeyName)" -cmatch $ProductCodePattern -and $_.DisplayName -ceq $DisplayName -and
        (Test-IsSamePath -First $_.InstallLocation -Second $InstallLocation)
    })
    if ($matching.Count -ne 1) {
        throw "Expected exactly one Windows Installer product '$DisplayName' in $InstallLocation, found $($matching.Count)."
    }
    return "$($matching[0].KeyName)"
}

function Get-MariaDbUninstallRegistryEntries {
    # Raw uninstall entries of MariaDB server products with KeyName, DisplayName, InstallLocation and Hidden
    # (SystemComponent 1).
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $entries = @()
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
            $properties = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if (-not $properties) { continue }
            $displayName = $properties.PSObject.Properties['DisplayName']
            if (-not $displayName -or "$($displayName.Value)" -cnotmatch $MariaDbProductPattern) { continue }
            $systemComponent = $properties.PSObject.Properties['SystemComponent']
            $location = $properties.PSObject.Properties['InstallLocation']
            $entries += [pscustomobject]@{
                KeyName         = $key.PSChildName
                DisplayName     = "$($displayName.Value)"
                InstallLocation = $(if ($location) { "$($location.Value)" } else { '' })
                Hidden          = [bool]($systemComponent -and "$($systemComponent.Value)" -eq '1')
            }
        }
    }
    return $entries
}

function Get-ProgramFilesFolder {
    # The 64-bit Program Files folder (the script must run in a 64-bit PowerShell).
    if (-not [Environment]::Is64BitProcess) { throw 'Run this script in a 64-bit PowerShell.' }
    return [Environment]::GetFolderPath('ProgramFiles')
}

function Get-ReparsePointOnPath {
    # Returns the first existing item from $Root down to $Path that is a reparse point, or
    # $null. $Path must lie below $Root.
    param([string]$Path, [string]$Root)
    $current = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $stop = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $items = @()
    while ($current -and $current.Length -ge $stop.Length) {
        $items += $current
        if ([string]::Equals($current, $stop, [StringComparison]::OrdinalIgnoreCase)) { break }
        $current = Split-Path -Parent $current
    }
    foreach ($item in $items) {
        if (Test-IsReparsePoint -Path $item) { return $item }
    }
    return $null
}

function Test-IsSamePath {
    # Compares two absolute Windows paths case-insensitively, ignoring trailing separators
    # and "/" versus "\". Relative paths are never considered equal.
    param([string]$First, [string]$Second)
    if (-not $First -or -not $Second) { return $false }
    try {
        if (-not [IO.Path]::IsPathRooted($First) -or -not [IO.Path]::IsPathRooted($Second)) { return $false }
        $a = [IO.Path]::GetFullPath($First.Replace('/', '\')).TrimEnd('\')
        $b = [IO.Path]::GetFullPath($Second.Replace('/', '\')).TrimEnd('\')
    } catch {
        return $false
    }
    return [string]::Equals($a, $b, [StringComparison]::OrdinalIgnoreCase)
}

function Test-IsPathUnder {
    # True if $Path is $Root or lies below it. Both are normalised to full paths and compared
    # case-insensitively; relative paths are never accepted.
    param([string]$Path, [string]$Root)
    if (-not $Path -or -not $Root) { return $false }
    try {
        if (-not [IO.Path]::IsPathRooted($Path) -or -not [IO.Path]::IsPathRooted($Root)) { return $false }
        $fullPath = [IO.Path]::GetFullPath($Path.Replace('/', '\')).TrimEnd('\')
        $fullRoot = [IO.Path]::GetFullPath($Root.Replace('/', '\')).TrimEnd('\')
    } catch {
        return $false
    }
    return [string]::Equals($fullPath, $fullRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $fullPath.StartsWith($fullRoot + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Test-IsStrictlyUnder {
    # True if $Path lies below $Root (not equal to it).
    param([string]$Path, [string]$Root)
    return (Test-IsPathUnder -Path $Path -Root $Root) -and -not (Test-IsSamePath -First $Path -Second $Root)
}

function ConvertFrom-ChocolateyListOutput {
    # Returns the installed version of a package from "choco list <name> --exact
    # --limit-output" (lines of the form name|version), or $null.
    param([string[]]$Lines, [string]$Name)
    foreach ($line in @($Lines)) {
        $parts = "$line".Trim().Split('|')
        if ($parts.Count -eq 2 -and $parts[0] -ieq $Name -and $parts[1]) { return $parts[1] }
    }
    return $null
}

function Get-InstalledChocolateyPackageVersion {
    # Chocolatey 2 lists only locally installed packages with "choco list".
    param([string]$Name)
    $output = @(choco list $Name --exact --limit-output)
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while listing packages." }
    return ConvertFrom-ChocolateyListOutput -Lines $output -Name $Name
}

function Get-ChocolateyToolsRoot {
    # Installation root of the earlier package 'mysql': Chocolatey's Get-ToolsLocation uses
    # the variable ChocolateyToolsLocation or "<system drive>\tools"
    # (helpers\functions\Get-ToolsLocation.ps1). Only the machine-wide value or exactly
    # C:\tools is used: process and user values can be set without administrator rights.
    $value = [Environment]::GetEnvironmentVariable('ChocolateyToolsLocation', 'Machine')
    if ($value) { return $value }
    return 'C:\tools'
}

function Get-OldPackageRoot {
    return Join-Path (Get-ChocolateyToolsRoot) $LegacyMySqlPackage
}

function Disable-DatabaseService {
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    # Stop-Service can fail while the service is still starting or stopping: wait up to 30
    # seconds for a stable state and try again.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $status = "$((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status)"
        if ($status -notin @('StartPending', 'StopPending', 'ContinuePending', 'PausePending')) { break }
        Start-Sleep -Seconds 2
    }
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Set-Service -Name $ServiceName -StartupType Disabled -ErrorAction SilentlyContinue
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -eq 'Stopped' -and "$($service.StartType)" -eq 'Disabled') {
        Write-Warning "The service '$ServiceName' was stopped and its start type set to Disabled."
    } else {
        Write-Host ''
        Write-Host "ERROR: The service '$ServiceName' could not be stopped and disabled. Run as administrator:" -ForegroundColor Red
        Write-Host "  sc.exe stop $ServiceName" -ForegroundColor Red
        Write-Host "  sc.exe config $ServiceName start= disabled" -ForegroundColor Red
        Write-Host "Then check with: Get-Service $ServiceName | Select-Object Status, StartType" -ForegroundColor Red
    }
}

function Stop-DatabaseService {
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
}

function Start-DatabaseService {
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
}

function Test-IsReparsePoint {
    # True if the file or directory exists and is a reparse point (e.g. a symbolic link, a
    # junction or a mount point).
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Wait-ServiceRemoved {
    # "sc.exe delete" only marks a service for deletion; it disappears once no handle is open.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Seconds 2
    }
    throw "The service '$ServiceName' still exists. Close the Services console and Task Manager, or restart Windows, then run: $RetryCommand"
}

function Test-RightsAllowWrite {
    # Takes the raw value: generic rights are not defined in the FileSystemRights enumeration.
    param([long]$Rights)
    # Generic rights (e.g. GENERIC_WRITE 0x40000000, GENERIC_ALL 0x10000000) can appear in
    # inherit-only ACEs; they are treated as write access.
    $generic = 0x10000000 -bor 0x40000000
    return (($Rights -band [long]$WriteRights) -ne 0) -or (($Rights -band $generic) -ne 0)
}

function Get-AceViolation {
    # Allowlist for one access control entry: deny entries are always allowed. Allow entries
    # are allowed for the SIDs in $AllowedSids; with -AllowReadForOthers, also for any other SID
    # without a write right (generic rights count as write rights). Returns $null or a
    # description.
    param([string]$Sid, [long]$Rights, [string]$Type, [string[]]$AllowedSids, [switch]$AllowReadForOthers)
    if ($Type -eq 'Deny') { return $null }
    if ($Sid -in $AllowedSids) { return $null }
    if ($AllowReadForOthers -and -not (Test-RightsAllowWrite -Rights $Rights)) { return $null }
    return "$Type $Sid $Rights"
}

function Get-SecurityViolations {
    # Checks the owner and every access control entry of $Path and every file and folder below
    # it, except the subtree $ExcludePath. Reparse points are never followed; each one is a
    # violation.
    param(
        [string]$Path,
        [string[]]$AllowedOwners,
        [string[]]$AllowedSids,
        [switch]$AllowReadForOthers,
        [string]$ExcludePath
    )
    $violations = @()
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue($Path)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        if ($ExcludePath -and (Test-IsPathUnder -Path $current -Root $ExcludePath)) { continue }
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            $violations += "$current is a reparse point"
            continue
        }
        $acl = Get-Acl -LiteralPath $current
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $AllowedOwners) { $violations += "$current has the owner $owner" }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            $violation = Get-AceViolation -Sid $rule.IdentityReference.Value -Rights ([long]$rule.FileSystemRights) -Type "$($rule.AccessControlType)" -AllowedSids $AllowedSids -AllowReadForOthers:$AllowReadForOthers
            if ($violation) { $violations += "${current}: $violation" }
        }
        if ($item.PSIsContainer) {
            foreach ($child in @(Get-ChildItem -LiteralPath $current -Force)) { $pending.Enqueue($child.FullName) }
        }
    }
    return $violations
}

function Get-TreeItemsWithoutReparsePoints {
    # Returns the full paths of $Path and every file and folder below it. Throws on the first
    # reparse point, which is never followed.
    param([string]$Path)
    $items = @()
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue($Path)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "$current is a symbolic link, junction or mount point."
        }
        $items += $current
        if ($item.PSIsContainer) {
            foreach ($child in @(Get-ChildItem -LiteralPath $current -Force)) { $pending.Enqueue($child.FullName) }
        }
    }
    return $items
}

function Get-ItemDacl {
    # Reads only the DACL of a file or folder (AccessControlSections.Access), so that writing
    # it back with Set-ItemDacl changes only the DACL, never the owner. .NET Framework:
    # FileSystemInfo.GetAccessControl; .NET: FileSystemAclExtensions (Microsoft Learn).
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    $sections = [Security.AccessControl.AccessControlSections]::Access
    $extensions = 'System.IO.FileSystemAclExtensions' -as [type]
    if ($extensions) { return $extensions::GetAccessControl($item, $sections) }
    return $item.GetAccessControl($sections)
}

function Set-ItemDacl {
    # Writes the modified sections of $Security (here: only the DACL) to a file or folder.
    param([string]$Path, $Security)
    $item = Get-Item -LiteralPath $Path -Force
    $extensions = 'System.IO.FileSystemAclExtensions' -as [type]
    if ($extensions) { $extensions::SetAccessControl($item, $Security) } else { $item.SetAccessControl($Security) }
}

function New-DataDirectorySecurity {
    # Permissions of the data directory: no inheritance from the program folder (which gives
    # Users read access), full control for Administrators, SYSTEM and the service account,
    # inherited by all files and folders below.
    param([string]$ServiceSid)
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($SidAdministrators, $SidSystem, $ServiceSid) | Select-Object -Unique) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, [Security.AccessControl.FileSystemRights]::FullControl, $inherit, $propagation, 'Allow')
        $security.AddAccessRule($rule)
    }
    return $security
}

function Protect-DataDirectory {
    # Sets the protected DACL of New-DataDirectorySecurity on the data directory (setting a DACL
    # by name propagates its inheritable entries to existing children, Microsoft Learn,
    # "SetNamedSecurityInfoW"), then resets
    # every file and folder below it to inherited permissions only (explicit entries removed,
    # inheritance enabled): mariadb-install-db.exe creates the subfolders of the data directory
    # with an explicit copy of its DACL (MARIADB_NEW_DIRECTORY_SDDL, sql/mysql_install_db.cc and
    # sql/mysqld.cc). All items are enumerated first without following reparse points (any
    # reparse point fails before anything is changed). Only used while the service is stopped,
    # on a tree below the program folder that ordinary users cannot change (checked before).
    param([string]$DataDirectory, [string]$ServiceSid)
    $items = @(Get-TreeItemsWithoutReparsePoints -Path $DataDirectory)
    Set-ItemDacl -Path $DataDirectory -Security (New-DataDirectorySecurity -ServiceSid $ServiceSid)
    foreach ($itemPath in @($items | Select-Object -Skip 1)) {
        if (Test-IsReparsePoint -Path $itemPath) { throw "$itemPath became a symbolic link, junction or mount point." }
        $acl = Get-ItemDacl -Path $itemPath
        $changed = $false
        if ($acl.AreAccessRulesProtected) {
            $acl.SetAccessRuleProtection($false, $false)
            $changed = $true
        }
        foreach ($rule in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
            [void]$acl.RemoveAccessRuleSpecific($rule)
            $changed = $true
        }
        if ($changed) { Set-ItemDacl -Path $itemPath -Security $acl }
    }
}

function Get-NamedPipeServerProcessId {
    # Returns the process ID of the server of the named pipe \\.\pipe\<name>, or -1 if the pipe
    # cannot be opened. The pipe is opened with SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
    # so that its server can only identify, not impersonate this elevated process (Microsoft
    # Learn, "CreateFileW" and "Impersonation Levels"; flag values 0x00100000 and 1 << 16 as in
    # dotnet/runtime Interop.SecurityOptions.cs). Microsoft Learn describes the handle of
    # GetNamedPipeServerProcessId as one created by CreateNamedPipe; that it also works with the
    # client handle is shown by scripts/local/test-setup-dev-windows.ps1.
    param([string]$Name)
    if (-not ('LegPlatformSetup.PipeInfo' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace LegPlatformSetup {
    public static class PipeInfo {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetNamedPipeServerProcessId(SafeFileHandle pipe, out uint processId);
        public static long GetServerProcessId(string path) {
            // GENERIC_READ | GENERIC_WRITE, OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
            using (SafeFileHandle handle = CreateFileW(path, 0xC0000000, 0, IntPtr.Zero, 3, 0x00110000, IntPtr.Zero)) {
                if (handle.IsInvalid) { return -1; }
                uint processId;
                if (!GetNamedPipeServerProcessId(handle, out processId)) { return -1; }
                return processId;
            }
        }
    }
}
'@
    }
    if ($Name -cnotmatch '^[A-Za-z0-9_.-]+$') { throw "Invalid pipe name '$Name'." }
    return [LegPlatformSetup.PipeInfo]::GetServerProcessId("\\.\pipe\$Name")
}

function Get-ServiceProcessId {
    $service = Get-ServiceInfo -Name $ServiceName
    if (-not $service -or $service.ProcessId -eq 0) { throw "Service '$ServiceName' is not running." }
    return [int]$service.ProcessId
}

function Assert-PipeServedByService {
    # Waits until the named pipe exists and verifies that its server is the service process,
    # before any password is sent over it. The server creates the pipe with
    # FILE_FLAG_FIRST_PIPE_INSTANCE and denies other users FILE_CREATE_PIPE_INSTANCE
    # (sql/handle_connections_win.cc), so the pipe stays with the service while it runs.
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        $servicePid = Get-ServiceProcessId
        $pipePid = Get-NamedPipeServerProcessId -Name $ServiceName
        if ($pipePid -eq $servicePid) { return $servicePid }
        if ($pipePid -gt 0) { throw "The named pipe '$ServiceName' belongs to process $pipePid, not to the service." }
        Start-Sleep -Seconds 2
    }
    throw "The named pipe '$ServiceName' of the service did not become available."
}

function Get-ListenerProblems {
    # Checks the TCP listeners of the server process (objects with LocalAddress, LocalPort).
    # With -ExpectNone, any listener is a problem (skip-networking). Otherwise exactly
    # 127.0.0.1:$DatabasePort is expected.
    param([object[]]$Listeners, [switch]$ExpectNone)
    $problems = @()
    $all = @($Listeners | Where-Object { $_ })
    if ($ExpectNone) {
        foreach ($listener in $all) { $problems += "listens on $($listener.LocalAddress):$($listener.LocalPort)" }
        return $problems
    }
    if (@($all | Where-Object { "$($_.LocalAddress)" -eq '127.0.0.1' -and [int]$_.LocalPort -eq $DatabasePort }).Count -eq 0) {
        $problems += "does not listen on 127.0.0.1:$DatabasePort"
    }
    foreach ($listener in @($all | Where-Object { "$($_.LocalAddress)" -ne '127.0.0.1' -or [int]$_.LocalPort -ne $DatabasePort })) {
        $problems += "listens on $($listener.LocalAddress):$($listener.LocalPort)"
    }
    return $problems
}

function Get-ServiceListeners {
    param([int]$ProcessId)
    return @(Get-NetTCPConnection -State Listen -OwningProcess $ProcessId -ErrorAction SilentlyContinue)
}

function Assert-LoopbackOnly {
    # Waits until the server listens on 127.0.0.1:$DatabasePort and verifies that it listens
    # nowhere else.
    $deadline = (Get-Date).AddSeconds(60)
    $problems = @('no check was run')
    while ((Get-Date) -lt $deadline) {
        $problems = @(Get-ListenerProblems -Listeners (Get-ServiceListeners -ProcessId (Get-ServiceProcessId)))
        if ($problems.Count -eq 0) { return }
        Start-Sleep -Seconds 2
    }
    throw "The server $($problems -join '; ')."
}

function Get-ServerCheckProblems {
    # Checks the result line of $ServerCheckSql (field order documented there).
    param([string]$Line)
    $parts = "$Line".Trim().Split('|')
    if ($parts.Count -ne 12) { return @("unexpected server check result '$Line'") }
    $problems = @()
    if ($parts[0] -ne '127.0.0.1') { $problems += "bind_address is '$($parts[0])'" }
    if ($parts[1] -ne '0') { $problems += 'skip_networking is on' }
    if ($parts[2] -ne '0') { $problems += 'named_pipe is on' }
    if ($parts[3] -ne "$DatabasePort") { $problems += "port is $($parts[3])" }
    if ($parts[4] -eq 'ACTIVE') { $problems += 'the feedback plugin is active' }
    if ($parts[5] -cne 'OFF' -and $parts[5] -cne 'ABSENT') { $problems += "shared_memory is '$($parts[5])'" }
    if ($parts[6] -ne '0') { $problems += 'general_log is on' }
    if ($parts[7] -ne '0') { $problems += 'slow_query_log is on' }
    if ($parts[8] -ne 'FILE') { $problems += "log_output is '$($parts[8])'" }
    if ($parts[11] -ne 'DISABLED') { $problems += "event_scheduler is '$($parts[11])'" }
    foreach ($index in @(9, 10)) {
        if (-not $parts[$index] -or $parts[$index] -match '[\\/:]') { $problems += "log file '$($parts[$index])' is not a plain file name" }
    }
    return $problems
}

function Get-UnexpectedAccounts {
    # Returns the accounts (user@host, one per line) that are neither reserved accounts nor in
    # $Additional.
    param([string[]]$Lines, [string[]]$Additional = @())
    $allowed = @($ReservedAccounts) + @($Additional)
    $accounts = @($Lines | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    return @($accounts | Where-Object { $_ -cnotin $allowed })
}

function Get-UnexpectedObjects {
    # Returns the stored routines and views (lines schema:type:name:definer of $StoredObjectSql)
    # that are not bootstrap objects with the bootstrap definer.
    param([string[]]$Lines)
    $problems = @()
    foreach ($line in @($Lines | ForEach-Object { "$_".Trim() } | Where-Object { $_ })) {
        $index = $line.LastIndexOf(':')
        $object = if ($index -gt 0) { $line.Substring(0, $index) } else { $line }
        $definer = if ($index -gt 0) { $line.Substring($index + 1) } else { '' }
        if ($object -cnotin $BootstrapObjects -or $definer -cne $BootstrapObjectDefiner) { $problems += "stored object: $line" }
    }
    return $problems
}

function Get-GrantProblems {
    # Checks the output of SHOW GRANTS for the application account: one USAGE grant on *.* and
    # exactly the privileges $AppPrivileges on each of the two databases, nothing else. The
    # lines are never included in the result (the USAGE line contains the password hash).
    # mariadb --batch escapes a backslash as \.
    param([string[]]$Lines)
    $expected = @($AppPrivileges.Split(',') | ForEach-Object { $_.Trim() } | Sort-Object)
    $databases = @{}
    $problems = @()
    foreach ($raw in @($Lines | ForEach-Object { "$_".Trim() } | Where-Object { $_ })) {
        $line = $raw.Replace('\\', '\')
        if ($line -cmatch '^GRANT USAGE ON \*\.\* TO ') { continue }
        $match = [regex]::Match($line, '^GRANT (.+) ON `([^`]+)`\.\* TO `' + [regex]::Escape($AppUser) + '`@`127\.0\.0\.1`$')
        if (-not $match.Success) { $problems += 'unexpected grant line'; continue }
        $privileges = @($match.Groups[1].Value.Split(',') | ForEach-Object { $_.Trim() } | Sort-Object)
        if (($privileges -join ',') -cne ($expected -join ',')) { $problems += "unexpected privileges on $($match.Groups[2].Value)" }
        $databases[$match.Groups[2].Value] = $true
    }
    foreach ($database in @($DatabaseName, $TestDatabaseName)) {
        if (-not $databases.ContainsKey($database.Replace('_', '\_'))) { $problems += "no grant on $database" }
    }
    if ($databases.Count -gt 2) { $problems += 'grants on other databases' }
    return $problems
}

function Assert-ServerState {
    # Runs the account allowlist, the stored-object allowlist and the hardening checks. Used
    # over the named pipe after the hardening and again over TCP after the restart.
    param([string]$Client, [string]$Protocol, [string]$Password, [string[]]$AdditionalAccounts = @())
    $connection = @{ Client = $Client; Protocol = $Protocol; Password = $Password }
    if ($Protocol -eq 'Pipe') { $connection.PipeName = $ServiceName }
    $unexpected = @(Get-UnexpectedAccounts -Lines @(Invoke-MariaDb @connection -Sql $AccountListSql) -Additional $AdditionalAccounts)
    if ($unexpected.Count -gt 0) { throw "Unexpected accounts: $($unexpected -join ', ')." }
    $problems = @(Get-UnexpectedObjects -Lines @(Invoke-MariaDb @connection -Sql $StoredObjectSql))
    $problems += @(Invoke-MariaDb @connection -Sql $HardeningProblemSql | Where-Object { "$_".Trim() })
    if ($problems.Count -gt 0) { throw "Unexpected server state: $($problems -join '; ')." }
}

function Get-HardeningSql {
    # Hardening of a new data directory, following mariadb-secure-installation
    # (scripts/mysql_secure_installation.sh): remove anonymous accounts, root accounts for
    # other hosts than localhost, 127.0.0.1 and ::1 (the bootstrap creates root without a
    # password for localhost, the host name, 127.0.0.1 and ::1), the 'test' database and its
    # grants (scripts/mariadb_test_db.sql grants all on 'test' and 'test\_%' to PUBLIC, which
    # would include test_leg_platform), then set the root password. The proxy grants of the
    # removed root accounts are deleted as well. The first statement aborts the batch (SIGNAL;
    # the client stops at the first error) if any other client is connected; it is a compound
    # statement outside a stored program and needs its own delimiter (MariaDB documentation,
    # "Using Compound Statements Outside of Stored Programs").
    param([string]$Password)
    return @"
DELIMITER //
IF (SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() AND COMMAND <> 'Daemon') > 0 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Another client is connected; setup aborted.'; END IF //
DELIMITER ;
DELETE FROM mysql.global_priv WHERE User='';
DELETE FROM mysql.global_priv WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
DELETE FROM mysql.proxies_priv WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
DROP DATABASE IF EXISTS test;
DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';
FLUSH PRIVILEGES;
ALTER USER IF EXISTS 'root'@'localhost' IDENTIFIED BY '$Password', 'root'@'127.0.0.1' IDENTIFIED BY '$Password', 'root'@'::1' IDENTIFIED BY '$Password';
"@
}

function Get-AppAccountSql {
    # In database-level GRANT statements, '_' is a wildcard (MariaDB documentation, GRANT:
    # "database names in GRANT can contain % and _ wildcards"); it is escaped as '\_' so that
    # the grants cover exactly these databases (the form mariadb-secure-installation uses
    # for 'test\_%').
    param([string]$Password)
    $grantDatabase = $DatabaseName.Replace('_', '\_')
    $grantTestDatabase = $TestDatabaseName.Replace('_', '\_')
    return @"
CREATE DATABASE ``$DatabaseName`` CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;
CREATE USER '$AppUser'@'127.0.0.1' IDENTIFIED BY '$Password';
GRANT $AppPrivileges ON ``$grantDatabase``.* TO '$AppUser'@'127.0.0.1';
GRANT $AppPrivileges ON ``$grantTestDatabase``.* TO '$AppUser'@'127.0.0.1';
"@
}

function Get-SignedInUser {
    # User signed in at the console: Win32_ComputerSystem.UserName ("Name of a user that is
    # logged on currently"; empty for remote sessions). Returns $null if unknown.
    try {
        $name = (Get-CimInstance Win32_ComputerSystem).UserName
        if ($name) { return "$name" }
    } catch {
    }
    return $null
}

function Get-PresentOptionFiles {
    # Returns the paths that exist (as file or folder) among $Paths.
    param([string[]]$Paths)
    return @($Paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
}

function Remove-PathListEntry {
    # Returns the PATH value without the entries that are the same path as one of
    # $Directories (Test-IsSamePath); all other entries, including unexpanded ones such as
    # %USERPROFILE%\bin and empty segments, are kept unchanged and in order.
    param([string]$Value, [string[]]$Directories)
    if (-not $Value) { return $Value }
    $kept = @($Value.Split(';') | Where-Object {
        $entry = $_
        -not ($entry -and ($Directories | Where-Object { Test-IsSamePath -First $entry -Second $_ }))
    })
    return ($kept -join ';')
}

function Get-MySqlPathEntries {
    # Returns the entries of a PATH value that point to a MySQL bin folder.
    param([string]$Value)
    if (-not $Value) { return @() }
    return @($Value.Split(';') | Where-Object { $_ -match '(?i)mysql.*\\bin\\?$' })
}

function Read-PathValue {
    # Reads a PATH value from the registry without expanding environment variables and
    # returns it with its value kind, so that a REG_EXPAND_SZ value is written back as such
    # (Microsoft Learn, "RegistryValueOptions": DoNotExpandEnvironmentNames; RegistryKey.GetValueKind).
    param([Microsoft.Win32.RegistryKey]$Key)
    if (-not $Key -or -not ($Key.GetValueNames() -contains 'Path')) { return $null }
    return [pscustomobject]@{
        Value = [string]$Key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        Kind  = $Key.GetValueKind('Path')
    }
}

function Remove-UserPathEntry {
    # Removes the given directories from the PATH of the current user (HKCU\Environment). The
    # earlier MySQL package added its bin folder there (Install-ChocolateyPath, default
    # PathType User). A stale entry would let anyone who can recreate the folder place programs
    # that are found through PATH. New terminals see the change.
    param([string[]]$Directories)
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $path = Read-PathValue -Key $key
        if (-not $path) { return }
        $newValue = Remove-PathListEntry -Value $path.Value -Directories $Directories
        if ($newValue -cne $path.Value) {
            $key.SetValue('Path', $newValue, $path.Kind)
            Write-Host 'Removed MySQL entries from the user PATH; open a new terminal to use the new PATH.'
        }
    } finally {
        if ($key) { $key.Dispose() }
    }
}

function Remove-MachinePathEntry {
    # Removes $Directory from the machine PATH. The package mariadb.install adds
    # <InstallLocation>\bin there (Install-ChocolateyPath ... 'Machine'); its uninstall script,
    # which would remove it, is skipped. New terminals see the change.
    param([string]$Directory)
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $true)
    try {
        $path = Read-PathValue -Key $key
        if (-not $path) { return }
        $newValue = Remove-PathListEntry -Value $path.Value -Directories @($Directory)
        if ($newValue -cne $path.Value) {
            $key.SetValue('Path', $newValue, $path.Kind)
            Write-Host "Removed $Directory from the machine PATH; sign out and in again (or restart Windows) so that new terminals use the new PATH."
        }
    } finally {
        if ($key) { $key.Dispose() }
    }
}

function Write-MachinePathWarning {
    # Apart from the bin folder of a removed MariaDB installation (Remove-MachinePathEntry),
    # machine-wide PATH entries are not changed by this script; MySQL entries there are
    # reported. (The MariaDB package adds <program folder>\bin to the machine PATH; that
    # folder is below %ProgramFiles% and not writable by ordinary users.)
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $false)
    try {
        $path = Read-PathValue -Key $key
        $entries = @(if ($path) { Get-MySqlPathEntries -Value $path.Value })
        if ($entries.Count -gt 0) {
            Write-Warning "The machine PATH contains MySQL folders: $($entries -join ', '). Remove them if they are not used by another program (System Properties, Environment Variables)."
        }
    } finally {
        if ($key) { $key.Dispose() }
    }
}

function Get-ServiceOwnershipProblem {
    # Returns $null if the service command line belongs to the earlier MySQL installation of
    # this project: the program lies under <tools>\mysql and --defaults-file is absent or
    # points to <tools>\mysql\current\my.ini. Otherwise returns the problem.
    param([string]$PathName, [string]$Executable, [string]$ToolsRoot)
    if (-not $Executable) {
        return "the program of the service could not be found in its command line '$PathName'"
    }
    $oldPackageRoot = Join-Path $ToolsRoot $LegacyMySqlPackage
    if (-not (Test-IsPathUnder -Path $Executable -Root $oldPackageRoot)) {
        return "the service runs '$Executable', which is not under $oldPackageRoot"
    }
    $defaultsFile = Get-DefaultsFileFromCommandLine -PathName $PathName
    $expected = Join-Path $oldPackageRoot 'current\my.ini'
    if ($defaultsFile -and -not (Test-IsSamePath -First $defaultsFile -Second $expected)) {
        return "the service uses the option file '$defaultsFile', not $expected"
    }
    return $null
}

function Test-ServiceBelongsToMariaDb {
    # True if the service program lies in the bin folder of one of the MariaDB installations.
    param([string]$Executable, $Entries)
    if (-not $Executable) { return $false }
    foreach ($entry in @($Entries)) {
        if ($entry.InstallLocation -and (Test-IsPathUnder -Path $Executable -Root (Join-Path $entry.InstallLocation 'bin'))) { return $true }
    }
    return $false
}

function Get-InstallationState {
    # Collects what exists, without changing anything.
    $service = Get-ServiceInfo -Name $ServiceName
    $executable = $null
    $ownershipProblem = $null
    $serviceIsLegacy = $false
    if ($service) {
        $executable = Resolve-ServiceExecutablePath -PathName $service.PathName
        $ownershipProblem = Get-ServiceOwnershipProblem -PathName $service.PathName -Executable $executable -ToolsRoot (Get-ChocolateyToolsRoot)
        $serviceIsLegacy = [bool]($executable -and (Test-IsPathUnder -Path $executable -Root (Get-OldPackageRoot)))
    }
    $entries = @(Get-MariaDbUninstallEntries)
    $otherServices = @($ReservedServiceNames | Where-Object { $_ -ne $ServiceName -and (Get-Service -Name $_ -ErrorAction SilentlyContinue) })
    return [pscustomobject]@{
        ServiceExists           = [bool]$service
        ServiceRunning          = [bool]($service -and "$($service.State)" -ne 'Stopped')
        ServiceIsLegacy         = $serviceIsLegacy
        OwnershipProblem        = $ownershipProblem
        ServiceIsMariaDb        = Test-ServiceBelongsToMariaDb -Executable $executable -Entries $entries
        OtherServices           = $otherServices
        PackageVersion          = Get-InstalledChocolateyPackageVersion -Name $LegacyMySqlPackage
        OldPackageRootExists    = Test-Path -LiteralPath (Get-OldPackageRoot)
        OldDataDirectoryExists  = Test-Path -LiteralPath $OldDataDirectory
        MariaDbEntries          = $entries
        MariaDbPackageVersion   = Get-InstalledChocolateyPackageVersion -Name $MariaDbPackage
        MariaDbMetaVersion      = Get-InstalledChocolateyPackageVersion -Name $MariaDbMetaPackage
    }
}

function Get-InstallationKind {
    # 'Legacy' (earlier MySQL installation, manual removal), 'Foreign' (a service named MySQL
    # or MariaDB that is not part of a MariaDB installation), 'MariaDb' (a MariaDB
    # installation) or 'None'.
    param($State)
    if ($State.ServiceIsLegacy -or $State.OldPackageRootExists -or $State.OldDataDirectoryExists -or $State.PackageVersion) { return 'Legacy' }
    if (@($State.OtherServices).Count -gt 0) { return 'Foreign' }
    if ($State.ServiceExists -and -not $State.ServiceIsMariaDb) { return 'Foreign' }
    if ($State.ServiceExists -or @($State.MariaDbEntries).Count -gt 0 -or $State.MariaDbPackageVersion -or $State.MariaDbMetaVersion) { return 'MariaDb' }
    return 'None'
}

function Get-MariaDbRemovalProblems {
    # Reasons why a MariaDB installation cannot be removed automatically. Only exactly one
    # MariaDB installed by the Chocolatey package below %ProgramFiles%, whose program folder is
    # no reparse point, and a service 'MySQL' (if any) that runs its program, are removed.
    param($State, [string]$ProgramFiles)
    $problems = @()
    $entries = @($State.MariaDbEntries)
    if ($entries.Count -ne 1) {
        $problems += "expected exactly one MariaDB installation, found $($entries.Count)"
    } else {
        $location = $entries[0].InstallLocation
        if (-not (Test-IsStrictlyUnder -Path $location -Root $ProgramFiles)) {
            $problems += "the MariaDB folder '$location' is not below $ProgramFiles"
        }
    }
    if (-not $State.MariaDbPackageVersion) {
        $problems += "the Chocolatey package '$MariaDbPackage' is not installed"
    }
    if ($State.ServiceExists -and -not $State.ServiceIsMariaDb) {
        $problems += "the service '$ServiceName' does not run the program of this MariaDB installation"
    }
    if (@($State.OtherServices).Count -gt 0) {
        $problems += "other services exist: $(@($State.OtherServices) -join ', ')"
    }
    return $problems
}

function Get-LegacyCheckPaths {
    # Folders whose type is checked before deletion commands are printed.
    return @((Get-ChocolateyToolsRoot), (Get-OldPackageRoot), $OldDataParent, $OldDataDirectory)
}

function Get-ReparsePaths {
    # Returns the existing paths among $Paths that are reparse points (symbolic links,
    # junctions, mount points).
    param([string[]]$Paths)
    return @($Paths | Where-Object { $_ -and (Test-IsReparsePoint -Path $_) })
}

function Get-LegacyRemovalCommands {
    # Commands for the manual removal of the earlier MySQL installation, for an elevated
    # PowerShell. Only C:\ProgramData\MySQL\data is deleted recursively (Oracle's installer uses
    # other folders below C:\ProgramData\MySQL); the parent only if it is then empty
    # (non-recursive Directory.Delete fails otherwise). [IO.Directory]::Delete with
    # recursive=true "does not recurse through the reparse point" (Microsoft Learn,
    # "Directory.Delete Method"). The service commands are included only for the service of
    # the earlier installation. Returns no commands if one of the folders is a reparse point.
    param($State, [string[]]$ReparsePaths)
    if (@($ReparsePaths).Count -gt 0) { return @() }
    $checkPaths = (@(Get-LegacyCheckPaths) | ForEach-Object { "'$_'" }) -join ', '
    $commands = @(
        "# 1. Must print nothing; otherwise stop and ask for help:",
        "Get-Item -LiteralPath $checkPaths -Force -ErrorAction SilentlyContinue | Where-Object { `$_.Attributes -band [IO.FileAttributes]::ReparsePoint }"
    )
    $serviceIsOurs = $State.ServiceIsLegacy -and -not $State.OwnershipProblem
    if ($serviceIsOurs) {
        $commands += @(
            "Stop-Service -Name $ServiceName -Force",
            "sc.exe config $ServiceName start= disabled"
        )
    }
    if ($State.PackageVersion -and ($serviceIsOurs -or -not $State.ServiceExists)) {
        $commands += "choco uninstall $LegacyMySqlPackage -y"
    }
    if ($serviceIsOurs) { $commands += "sc.exe delete $ServiceName" }
    if ($State.OldPackageRootExists) { $commands += "[IO.Directory]::Delete('$(Get-OldPackageRoot)', `$true)" }
    if ($State.OldDataDirectoryExists) {
        $commands += @(
            "[IO.Directory]::Delete('$OldDataDirectory', `$true)",
            "# Optional; fails if the folder is not empty (other MySQL installations):",
            "[IO.Directory]::Delete('$OldDataParent')"
        )
    }
    return $commands
}

function Write-ManualRemovalInstructions {
    param($State, [string]$Kind)
    Write-Host ''
    Write-Host 'Nothing was changed. This installation is not removed automatically (docs/INSTALLATION.md, "Recovery after a failed setup").' -ForegroundColor Red
    if ($Kind -eq 'Legacy') {
        if ($State.ServiceExists -and -not ($State.ServiceIsLegacy -and -not $State.OwnershipProblem)) {
            Write-Warning "The service '$ServiceName' belongs to another installation. Do not stop or delete it for this project; resolve the name conflict manually."
        }
        $reparsePaths = @(Get-ReparsePaths -Paths (Get-LegacyCheckPaths))
        if ($reparsePaths.Count -gt 0) {
            Write-Warning "These folders are symbolic links, junctions or mount points: $($reparsePaths -join ', '). Do not delete anything; check the folders first."
            return
        }
        Write-Host 'Remove the earlier local MySQL installation in a PowerShell window opened as administrator, while no other user is signed in to this computer:' -ForegroundColor Red
        foreach ($command in Get-LegacyRemovalCommands -State $State -ReparsePaths $reparsePaths) { Write-Host "  $command" }
        Write-Host 'Commands for parts that do not exist report an error; that is expected. Then run this script again.' -ForegroundColor Red
    } else {
        Write-Host "A service named $($ReservedServiceNames -join ' or ') or a MariaDB installation exists that this script does not remove. Check it and remove it manually if it is no longer needed." -ForegroundColor Red
    }
}

function Confirm-FailedInstallationRemoval {
    # Lists the MariaDB installation and asks for the typed confirmation before anything is
    # changed. Throws if the installation cannot be removed automatically.
    param($State, [string]$ProgramFiles)
    $problems = @(Get-MariaDbRemovalProblems -State $State -ProgramFiles $ProgramFiles)
    if ($problems.Count -gt 0) {
        Write-ManualRemovalInstructions -State $State -Kind 'MariaDb'
        throw "The installation cannot be removed automatically: $($problems -join '; ')."
    }
    $location = @($State.MariaDbEntries)[0].InstallLocation
    Write-Host ''
    Write-Host 'The following local MariaDB installation will be removed:' -ForegroundColor Yellow
    if ($State.ServiceExists) { Write-Host "  - Windows service '$ServiceName' (stopped and disabled first)" }
    Write-Host "  - MariaDB product '$(@($State.MariaDbEntries)[0].DisplayName)' (uninstalled with Windows Installer)"
    if ($State.MariaDbMetaVersion) { Write-Host "  - Chocolatey package '$MariaDbMetaPackage' $($State.MariaDbMetaVersion) (package record only)" }
    Write-Host "  - Chocolatey package '$MariaDbPackage' $($State.MariaDbPackageVersion) (package record only)"
    Write-Host "  - $location with programs, option file and ALL local MariaDB databases" -ForegroundColor Yellow
    Write-Host "  - machine PATH entry $(Join-Path $location 'bin')"
    Write-Host 'This cannot be undone.' -ForegroundColor Yellow
    $answer = Read-Host 'Type DELETE to remove all items listed above'
    if ($answer -cne 'DELETE') {
        throw 'Not confirmed. Nothing was changed.'
    }
    return $location
}

function Remove-FailedInstallation {
    # Removes the MariaDB installation after Confirm-FailedInstallationRemoval. Every step
    # checks the current state, so that the removal can be repeated after an interruption.
    param([string]$InstallLocation, [string]$ProgramFiles)
    $current = Get-InstallationState
    if ((Get-InstallationKind -State $current) -ne 'MariaDb') { throw 'The installation changed since the confirmation. Nothing was changed.' }
    $problems = @(Get-MariaDbRemovalProblems -State $current -ProgramFiles $ProgramFiles)
    if ($problems.Count -gt 0 -or -not (Test-IsSamePath -First @($current.MariaDbEntries)[0].InstallLocation -Second $InstallLocation)) {
        throw "The installation changed since the confirmation: $($problems -join '; ')."
    }
    $reparse = Get-ReparsePointOnPath -Path $InstallLocation -Root $ProgramFiles
    if ($reparse) { throw "$reparse is a symbolic link, junction or mount point. Nothing was changed." }
    $productCode = Select-MariaDbProductCode -Entries @(Get-MariaDbUninstallRegistryEntries) `
        -DisplayName @($current.MariaDbEntries)[0].DisplayName -InstallLocation $InstallLocation
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Disable-DatabaseService
        if ("$((Get-Service -Name $ServiceName).Status)" -ne 'Stopped') {
            throw "The service '$ServiceName' could not be stopped. Nothing else was changed."
        }
    }
    # The package's chocolateyUninstall.ps1 (12.3.3) passes the uninstall string as -File, which
    # Uninstall-ChocolateyPackage ignores for MSI files (msiexec /x without a product, exit code
    # 1619; D-030). The MSI is therefore removed directly, by its ProductCode.
    $msiexec = Join-Path ([Environment]::GetFolderPath('System')) 'msiexec.exe'
    $process = Start-Process -FilePath $msiexec -ArgumentList @('/x', $productCode, '/qn', '/norestart') -Wait -PassThru
    # 3010: success, restart required (Microsoft Learn, "MsiExec.exe and InstMsi.exe error messages").
    $restartRequired = $process.ExitCode -eq 3010
    if (-not $restartRequired -and $process.ExitCode -ne 0) {
        throw "msiexec exited with code $($process.ExitCode) while uninstalling $productCode."
    }
    # -n skips the package scripts, --skip-autouninstaller the automatic uninstaller: only the
    # package records are removed (Chocolatey documentation, "Uninstall").
    foreach ($package in @($MariaDbMetaPackage, $MariaDbPackage)) {
        if (Get-InstalledChocolateyPackageVersion -Name $package) {
            choco uninstall $package -y --no-progress -n --skip-autouninstaller
            if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while removing the package $package." }
        }
    }
    Remove-MachinePathEntry -Directory (Join-Path $InstallLocation 'bin')
    # The MSI removes its service and, by default (CLEANUPDATA=1), its data directory
    # (win/packaging/extra.wxs.in). Leftovers are removed only if they belong to the folder
    # recorded above.
    $service = Get-ServiceInfo -Name $ServiceName
    if ($service) {
        $executable = Resolve-ServiceExecutablePath -PathName $service.PathName
        if (-not ($executable -and (Test-IsPathUnder -Path $executable -Root (Join-Path $InstallLocation 'bin'))) -and
            -not (Test-IsPathUnder -Path (Get-DefaultsFileFromCommandLine -PathName $service.PathName) -Root $InstallLocation)) {
            throw "The service '$ServiceName' does not belong to $InstallLocation; it was not deleted."
        }
        sc.exe delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sc.exe delete $ServiceName failed with exit code $LASTEXITCODE." }
        Wait-ServiceRemoved
    }
    if (Test-Path -LiteralPath $InstallLocation) {
        $reparse = Get-ReparsePointOnPath -Path $InstallLocation -Root $ProgramFiles
        if ($reparse) { throw "$reparse is a symbolic link, junction or mount point; $InstallLocation was not deleted." }
        # Below %ProgramFiles%, ordinary users cannot have changed the folder. [IO.Directory]::Delete
        # with recursive=true "does not recurse through the reparse point" (Microsoft Learn).
        try {
            [IO.Directory]::Delete($InstallLocation, $true)
        } catch {
            # Files may stay locked until the restart that Windows Installer asked for.
            if ($restartRequired) { throw "Windows Installer requires a restart and $InstallLocation could not be deleted yet. Restart Windows, delete $InstallLocation as administrator, then run this script again." }
            throw
        }
        if (Test-Path -LiteralPath $InstallLocation) { throw "$InstallLocation could not be deleted." }
        Write-Host "$InstallLocation was deleted."
    }
    if ($restartRequired) {
        throw 'MariaDB was removed, but Windows Installer requires a restart. Restart Windows, then run this script again without -RemoveFailedInstallation.'
    }
}

function Get-MariaDbProductName {
    # MSI product name of the 64-bit MariaDB server package of a version, e.g. 12.3.3 ->
    # "MariaDB 12.3 (x64)" (CPACK_WIX_PACKAGE_NAME, win/packaging/CMakeLists.txt).
    param([string]$Version)
    if ($Version -notmatch '^([0-9]+)\.([0-9]+)(\.|$)') { throw "Invalid MariaDB version '$Version'." }
    return "MariaDB $($Matches[1]).$($Matches[2]) (x64)"
}

function Get-SetupIncompleteReasons {
    # A MariaDB installation of this script is complete only if the service starts
    # automatically (set at the very end) and my.ini has networking on and the named pipe off
    # (set in step 4f). Returns the reasons why it is not, or none.
    param([string]$StartType, $Entries)
    $reasons = @()
    if ($StartType -ne 'Automatic') { $reasons += "the service start type is $StartType" }
    if ($null -eq $Entries) {
        $reasons += 'my.ini could not be read'
    } else {
        if (Test-IsOptionOn -Value (Get-ServerOptionValue -Entries $Entries -Key 'skip-networking')) { $reasons += 'networking is still skipped' }
        if (Test-IsOptionOn -Value (Get-ServerOptionValue -Entries $Entries -Key 'named-pipe')) { $reasons += 'the named pipe is still on' }
    }
    return $reasons
}

function Stop-IncompleteSetup {
    # On a repeated run: a MariaDB service whose setup did not complete (e.g. after Ctrl+C or a
    # closed console window) may still run without a root password; it is stopped and disabled.
    $service = Get-ServiceInfo -Name $ServiceName
    if (-not $service) { return }
    $entries = $null
    try {
        $optionFile = Get-DefaultsFileFromCommandLine -PathName $service.PathName
        if ($optionFile -and (Test-IsFile -Path $optionFile)) { $entries = @(Get-OptionFileEntries -Text (Read-TextFile -Path $optionFile).Text) }
    } catch {
        $entries = $null
    }
    $reasons = @(Get-SetupIncompleteReasons -StartType "$((Get-Service -Name $ServiceName).StartType)" -Entries $entries)
    if ($reasons.Count -gt 0) {
        Write-Host "WARNING: The setup of the MariaDB service '$ServiceName' was not completed ($($reasons -join '; ')). It is stopped and disabled now." -ForegroundColor Red
        Disable-DatabaseService
    }
}

function Wait-WindowsInstallerIdle {
    # Waits up to 10 minutes while Windows Installer runs an installation: the service is then
    # running with SERVICE_ACCEPT_SHUTDOWN in its accepted controls (Microsoft Learn,
    # "_MSIExecute Mutex"; ServiceController.CanShutdown). Used after an interruption, so that
    # a service the installer creates or starts afterwards is still disabled.
    $deadline = (Get-Date).AddMinutes(10)
    while ((Get-Date) -lt $deadline) {
        $installer = Get-Service -Name 'msiserver' -ErrorAction SilentlyContinue
        if (-not $installer -or $installer.Status -ne 'Running' -or -not $installer.CanShutdown) { return }
        Write-Host 'Waiting for Windows Installer to finish ...'
        Start-Sleep -Seconds 5
    }
    Write-Warning "Windows Installer is still busy. When it has finished, run as administrator: sc.exe stop $ServiceName; sc.exe config $ServiceName start= disabled"
}

function Disable-ServerFirewallRules {
    # The MSI adds a Windows Firewall exception for mysqld.exe with Scope='any'
    # (win/packaging/CPackWixConfig.cmake). Enabled inbound allow rules for the server program
    # are disabled (not deleted, so that the uninstallation can still remove them), and the
    # result is checked in the active store.
    param([string]$Server)
    $matching = {
        param($Store, $ErrorAction)
        @(Get-NetFirewallApplicationFilter -PolicyStore $Store -ErrorAction $ErrorAction |
            Where-Object { $_.Program -and $_.Program -ne 'Any' -and (Test-IsSamePath -First ([Environment]::ExpandEnvironmentVariables("$($_.Program)")) -Second $Server) } |
            Get-NetFirewallRule -ErrorAction $ErrorAction |
            Where-Object { "$($_.Direction)" -eq 'Inbound' -and "$($_.Action)" -eq 'Allow' -and "$($_.Enabled)" -eq 'True' })
    }
    foreach ($rule in & $matching 'PersistentStore' 'SilentlyContinue') {
        Disable-NetFirewallRule -Name $rule.Name -PolicyStore PersistentStore
        Write-Host "Disabled the inbound firewall rule '$($rule.DisplayName)' for $Server."
    }
    # The check of the active store fails closed if it cannot be read.
    $remaining = @(& $matching 'ActiveStore' 'Stop')
    if ($remaining.Count -gt 0) { throw "Inbound firewall rules allow connections to ${Server}: $(($remaining | ForEach-Object { $_.DisplayName }) -join ', ')." }
}

function Wait-NoOtherConnections {
    # Waits up to 15 seconds until no other client is connected (e.g. the connection opened by
    # the pipe check has been closed); the hardening checks it again in the same session.
    param([string]$Client)
    $deadline = (Get-Date).AddSeconds(15)
    $count = $null
    while ((Get-Date) -lt $deadline) {
        $count = "$(@(Invoke-MariaDb -Client $Client -Protocol Pipe -PipeName $ServiceName -Sql $OtherConnectionsSql) | Select-Object -Last 1)".Trim()
        if ($count -eq '0') { return }
        Start-Sleep -Seconds 1
    }
    throw "Other clients are connected to the server ($count); setup aborted."
}

function Write-RecoveryInstructions {
    Write-Host ''
    Write-Host 'Recovery (docs/INSTALLATION.md, "Recovery after a failed setup"): fix the cause shown above and run as administrator:'
    Write-Host "  $RetryCommand"
    Write-Host 'It lists what it removes and asks for a typed confirmation before it deletes anything.'
}

function Get-MariaDbInstallation {
    # Locates the new installation and verifies its layout: exactly one MariaDB product below
    # %ProgramFiles% without reparse points, the service runs its bin\mysqld.exe or
    # bin\mariadbd.exe with --defaults-file=<data directory>\my.ini below the program folder,
    # and the client mariadb.exe exists.
    param([string]$ProgramFiles, [string]$ProductName)
    $entries = @(Get-MariaDbUninstallEntries)
    if ($entries.Count -ne 1) { throw "Expected exactly one MariaDB installation, found $($entries.Count)." }
    if ($entries[0].DisplayName -cne $ProductName) { throw "The installed MariaDB is '$($entries[0].DisplayName)', not '$ProductName'." }
    $installDir = $entries[0].InstallLocation.TrimEnd('\')
    if (-not (Test-IsStrictlyUnder -Path $installDir -Root $ProgramFiles)) { throw "MariaDB was installed to '$installDir', not below $ProgramFiles." }
    $reparse = Get-ReparsePointOnPath -Path $installDir -Root $ProgramFiles
    if ($reparse) { throw "$reparse is a symbolic link, junction or mount point." }
    $service = Get-ServiceInfo -Name $ServiceName
    if (-not $service) { throw "Service '$ServiceName' was not created by the installation." }
    $server = Resolve-ServiceExecutablePath -PathName $service.PathName
    $binDir = Join-Path $installDir 'bin'
    if (-not $server -or -not (Test-IsSamePath -First (Split-Path -Parent $server) -Second $binDir) -or
        (Split-Path -Leaf $server) -notin @('mysqld.exe', 'mariadbd.exe')) {
        throw "The service command line '$($service.PathName)' does not start the server in $binDir."
    }
    $optionFile = Get-DefaultsFileFromCommandLine -PathName $service.PathName
    if (-not $optionFile -or (Split-Path -Leaf $optionFile) -ne 'my.ini' -or -not (Test-IsFile -Path $optionFile)) {
        throw "The service command line has no --defaults-file=<data directory>\my.ini."
    }
    $dataDir = Split-Path -Parent $optionFile
    if (-not (Test-IsStrictlyUnder -Path $dataDir -Root $installDir)) { throw "The data directory $dataDir is not below $installDir." }
    if (Get-ReparsePointOnPath -Path $optionFile -Root $installDir) { throw "A folder or file on the path $optionFile is a reparse point." }
    $client = @('mariadb.exe', 'mysql.exe') | ForEach-Object { Join-Path $binDir $_ } | Where-Object { Test-IsFile -Path $_ } | Select-Object -First 1
    if (-not $client) { throw "The client mariadb.exe was not found in $binDir." }
    return [pscustomobject]@{
        InstallDir  = $installDir
        Server      = $server
        DataDir     = $dataDir
        OptionFile  = $optionFile
        Client      = $client
        ServiceSid  = Get-ServiceAccountSid -StartName "$($service.StartName)" -Service $ServiceName
    }
}

function Get-InstallationSecurityViolations {
    # Program folder (without the data directory): no reparse points, owners Administrators,
    # SYSTEM or TrustedInstaller, write access only for these and CREATOR OWNER; read access
    # for anybody. Data directory: owners Administrators, SYSTEM or the service account, any
    # access only for these three.
    param($Installation, [switch]$ProgramFolder, [switch]$DataDirectory)
    $trustedInstaller = Get-TrustedInstallerSid
    $violations = @()
    if ($ProgramFolder) {
        $violations += @(Get-SecurityViolations -Path $Installation.InstallDir -ExcludePath $Installation.DataDir `
            -AllowedOwners @($SidAdministrators, $SidSystem, $trustedInstaller) `
            -AllowedSids @($SidAdministrators, $SidSystem, $trustedInstaller, $SidCreatorOwner) -AllowReadForOthers)
    }
    if ($DataDirectory) {
        $allowed = @($SidAdministrators, $SidSystem, $Installation.ServiceSid)
        $violations += @(Get-SecurityViolations -Path $Installation.DataDir -AllowedOwners $allowed -AllowedSids $allowed)
    }
    return $violations
}


# 0. Preconditions

if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    throw 'Chocolatey (choco) was not found.'
}
$chocoVersion = "$(choco --version)".Trim()
if ($chocoVersion -notmatch '^(\d+)\.' -or [int]$Matches[1] -lt 2) {
    # "choco list" lists only local packages from Chocolatey 2 on.
    throw "Chocolatey 2 or later is required (found '$chocoVersion'). Run: choco upgrade chocolatey -y"
}
$programFiles = Get-ProgramFilesFolder
$presentOptionFiles = @(Get-PresentOptionFiles -Paths $GlobalOptionFiles)
if ($presentOptionFiles.Count -gt 0) {
    throw "MariaDB programs would also read these option files: $($presentOptionFiles -join ', '). Remove or rename them first."
}

Clear-ClientEnvironment

# The PATH cleanup and the user environment variables apply to the account that runs this
# script (HKCU). If an administrator account other than the signed-in user is used for the
# elevation, they do not reach the signed-in user.
$signedInUser = Get-SignedInUser
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($signedInUser -and $signedInUser -ine $currentUser) {
    Write-Warning "This script runs as '$currentUser', but '$signedInUser' is signed in. PATH changes and the environment variables apply to '$currentUser' only. Run the script elevated with the signed-in account (Run as administrator)."
}

# Stale PATH entry of the earlier MySQL installation: removed from the user PATH; MySQL entries
# in the machine PATH are only reported.
Remove-UserPathEntry -Directories @($LegacyPathEntry, (Join-Path (Get-OldPackageRoot) 'current\bin'))
Write-MachinePathWarning

$state = Get-InstallationState
$kind = Get-InstallationKind -State $state
$removeLocation = $null
if ($kind -eq 'Legacy' -or $kind -eq 'Foreign') {
    if ($state.ServiceRunning) {
        Write-Warning "The service '$ServiceName' is running."
    }
    Write-ManualRemovalInstructions -State $state -Kind $kind
    throw 'A local database installation exists that this script does not remove automatically.'
}
if ($kind -eq 'MariaDb') {
    Stop-IncompleteSetup
    if (-not $RemoveFailedInstallation) {
        throw "A local MariaDB installation already exists (e.g. from a failed run). To remove it and start over, run as administrator: $RetryCommand"
    }
    $removeLocation = Confirm-FailedInstallationRemoval -State $state -ProgramFiles $programFiles
} elseif ($RemoveFailedInstallation) {
    Write-Host 'No local MariaDB installation found; nothing to remove.'
}

# 1. Ask for the root password before anything is installed

$rootPassword = Read-RootPassword

# 2. Remove the failed installation (only with -RemoveFailedInstallation, confirmed above)

if ($removeLocation) {
    Remove-FailedInstallation -InstallLocation $removeLocation -ProgramFiles $programFiles
    if ((Get-InstallationKind -State (Get-InstallationState)) -ne 'None') {
        throw "Parts of the old installation remain. Check docs/INSTALLATION.md, then run: $RetryCommand"
    }
    Write-Host 'The old local MariaDB installation was removed.'
}

# 3. Install Python unless the package is already installed
# Newest Python version supported by the newest Django release (D-015: the newest possible
# version is used for development; the minimum versions are only declared and tested in CI).

$installedPython = Get-InstalledChocolateyPackageVersion -Name $PythonPackage
if ($installedPython) {
    Write-Host "$PythonPackage $installedPython is already installed; skipping the installation."
    if ($installedPython -ne $PythonVersion) {
        Write-Warning "Expected $PythonPackage $PythonVersion. Update it with: choco upgrade $PythonPackage -y"
    }
} else {
    choco install $PythonPackage --version $PythonVersion -y --no-progress
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing Python." }
}

# 4. Install and configure MariaDB; on any failure, leave the server stopped and disabled

$completed = $false
$installStarted = $false
$settings = $null
try {
    # 4a. Install without networking (named pipe only) until the server is hardened.

    $installStarted = $true
    choco install $MariaDbPackage --version $MariaDbVersion -y --no-progress "--install-arguments=$MsiInstallArguments"
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing MariaDB." }
    # The server must not start automatically before the setup has succeeded.
    Set-Service -Name $ServiceName -StartupType Manual

    # 4b. Locate and verify the installation and the initial option file.

    $installation = Get-MariaDbInstallation -ProgramFiles $programFiles -ProductName (Get-MariaDbProductName -Version $MariaDbVersion)
    Disable-ServerFirewallRules -Server $installation.Server
    $optionFile = Read-TextFile -Path $installation.OptionFile
    $problems = @(Get-InitialOptionFileProblems -Entries (Get-OptionFileEntries -Text $optionFile.Text) -DataDirectory $installation.DataDir)
    if ($problems.Count -gt 0) { throw "Unexpected settings in $($installation.OptionFile): $($problems -join '; ')." }

    # 4c. File permissions: stop the server, check the program folder, restrict the data
    # directory (it inherits read access for Users from %ProgramFiles%) and check it.

    Stop-DatabaseService
    Write-Host "Checking the permissions below $($installation.InstallDir) (this can take a minute) ..."
    $violations = @(Get-InstallationSecurityViolations -Installation $installation -ProgramFolder)
    if ($violations.Count -gt 0) { throw "Unexpected permissions in the program folder: $($violations -join '; ')" }
    if (@(Get-InstallationSecurityViolations -Installation $installation -DataDirectory).Count -gt 0) {
        Protect-DataDirectory -DataDirectory $installation.DataDir -ServiceSid $installation.ServiceSid
    }
    $violations = @(Get-InstallationSecurityViolations -Installation $installation -DataDirectory)
    if ($violations.Count -gt 0) { throw "Unexpected permissions in the data directory: $($violations -join '; ')" }
    Write-Host 'Permissions verified (program folder: write access for administrators only; data directory: Administrators, SYSTEM and the service account only).'
    Start-DatabaseService

    # 4d. Hardening over the named pipe, after verifying that the service serves it and does
    # not listen on TCP.

    $servicePid = Assert-PipeServedByService
    $problems = @(Get-ListenerProblems -Listeners (Get-ServiceListeners -ProcessId $servicePid) -ExpectNone)
    if ($problems.Count -gt 0) { throw "The server should not listen on TCP yet but $($problems -join '; ')." }
    Wait-NoOtherConnections -Client $installation.Client
    Invoke-MariaDb -Client $installation.Client -Protocol Pipe -PipeName $ServiceName -Sql (Get-HardeningSql -Password $rootPassword)
    Invoke-MariaDb -Client $installation.Client -Protocol Pipe -PipeName $ServiceName -Password $rootPassword -Sql 'SELECT 1;' | Out-Null
    Assert-ServerState -Client $installation.Client -Protocol Pipe -Password $rootPassword

    # 4e. Database and application account

    $appPassword = New-RandomSecret
    Invoke-MariaDb -Client $installation.Client -Protocol Pipe -PipeName $ServiceName -Password $rootPassword -Sql (Get-AppAccountSql -Password $appPassword)
    $grantCheck = "$(@(Invoke-MariaDb -Client $installation.Client -Protocol Pipe -PipeName $ServiceName -Password $rootPassword -Sql $AppGrantCheckSql) | Select-Object -Last 1)".Trim()
    if ($grantCheck -ne '2|2') { throw "The grants of '$AppUser' are not exactly on $DatabaseName and $TestDatabaseName ($grantCheck)." }
    if ((Get-ServiceProcessId) -ne $servicePid) { throw 'The server process changed during the setup.' }

    # 4f. Switch to TCP on 127.0.0.1 only and disable the named pipe; verify sockets and
    # server variables.

    Stop-DatabaseService
    if (Get-ReparsePointOnPath -Path $installation.OptionFile -Root $installation.InstallDir) { throw "A folder or file on the path $($installation.OptionFile) is a reparse point." }
    $optionFile = Read-TextFile -Path $installation.OptionFile
    $newText = Get-NetworkOptionFileText -Text $optionFile.Text -LineBreak $optionFile.LineBreak
    # Overwriting the existing file keeps its permissions (inherited from the data directory).
    [IO.File]::WriteAllText($installation.OptionFile, $newText, $optionFile.Encoding)
    Start-DatabaseService
    Assert-LoopbackOnly
    $check = "$(@(Invoke-MariaDb -Client $installation.Client -Protocol Tcp -Password $rootPassword -Sql $ServerCheckSql) | Select-Object -Last 1)"
    $problems = @(Get-ServerCheckProblems -Line $check)
    if ($problems.Count -gt 0) { throw "Unexpected server settings: $($problems -join '; ')." }
    Invoke-MariaDb -Client $installation.Client -Protocol Tcp -User $AppUser -Password $appPassword -Sql 'SELECT 1;' | Out-Null
    # Again after the restart, over TCP as root@127.0.0.1 (which also tests that login): an
    # event or other object created during the passwordless phase could have acted between the
    # first checks and the restart.
    Assert-ServerState -Client $installation.Client -Protocol Tcp -Password $rootPassword -AdditionalAccounts @("$AppUser@127.0.0.1")
    $grantCheck = "$(@(Invoke-MariaDb -Client $installation.Client -Protocol Tcp -Password $rootPassword -Sql $AppGrantCheckSql) | Select-Object -Last 1)".Trim()
    if ($grantCheck -ne '2|2') { throw "The grants of '$AppUser' are not exactly on $DatabaseName and $TestDatabaseName ($grantCheck)." }
    $problems = @(Get-GrantProblems -Lines @(Invoke-MariaDb -Client $installation.Client -Protocol Tcp -Password $rootPassword -Sql "SHOW GRANTS FOR '$AppUser'@'127.0.0.1';"))
    if ($problems.Count -gt 0) { throw "Unexpected grants of '$AppUser': $($problems -join '; ')." }
    Write-Host "MariaDB listens only on 127.0.0.1:$DatabasePort; the named pipe is disabled; the application account can log in."

    # 4g. Store the local configuration as user environment variables

    $settings = [ordered]@{
        APP_ENV           = 'local'
        DB_NAME           = $DatabaseName
        DB_USER           = $AppUser
        DB_PASSWORD       = $appPassword
        DB_HOST           = '127.0.0.1'
        DB_PORT           = "$DatabasePort"
        DJANGO_SECRET_KEY = (New-RandomSecret -Bytes 50)
    }
    foreach ($name in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($name, $settings[$name], 'User')
    }
    $appPassword = $null

    Set-Service -Name $ServiceName -StartupType Automatic
    $completed = $true
} catch {
    Write-Warning "Setup failed: $($_.Exception.Message)"
    throw
} finally {
    # Also runs when the script is interrupted with Ctrl+C, which skips the catch block.
    $rootPassword = $null
    $appPassword = $null
    if (-not $completed) {
        # After an interruption, the installer may still be running and start the service.
        if ($installStarted) { Wait-WindowsInstallerIdle }
        Disable-DatabaseService
        Write-RecoveryInstructions
    }
}

Write-Host ''
Write-Host "MariaDB $MariaDbVersion is installed in $($installation.InstallDir), listens only on 127.0.0.1, the root password is set and the service '$ServiceName' starts automatically."
Write-Host "Database '$DatabaseName' and account '$AppUser'@'127.0.0.1' were created."
Write-Host "User environment variables set: $($settings.Keys -join ', ')"
Write-Host 'Restart VS Code so that new terminals and Claude Code receive the variables.'
Write-Host 'Never print the environment (e.g. Get-ChildItem env:); it contains DB_PASSWORD and DJANGO_SECRET_KEY.'
