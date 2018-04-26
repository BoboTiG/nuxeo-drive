# coding: utf-8
"""
Query formatting in this file is based on http://www.sqlstyle.guide/
"""
import os
import sqlite3
import sys
from datetime import datetime
from logging import getLogger
from threading import RLock, current_thread, local

from PyQt4.QtCore import QObject, pyqtSignal

log = getLogger(__name__)

SCHEMA_VERSION = 'schema_version'

# Summary status from last known pair of states
# (local_state, remote_state)
PAIR_STATES = {
    # regular cases
    ('unknown', 'unknown'): 'unknown',
    ('synchronized', 'synchronized'): 'synchronized',
    ('created', 'unknown'): 'locally_created',
    ('unknown', 'created'): 'remotely_created',
    ('modified', 'synchronized'): 'locally_modified',
    ('moved', 'synchronized'): 'locally_moved',
    ('moved', 'deleted'): 'locally_moved_created',
    ('moved', 'modified'): 'locally_moved_remotely_modified',
    ('synchronized', 'modified'): 'remotely_modified',
    ('modified', 'unknown'): 'locally_modified',
    ('unknown', 'modified'): 'remotely_modified',
    ('deleted', 'synchronized'): 'locally_deleted',
    ('synchronized', 'deleted'): 'remotely_deleted',
    ('deleted', 'deleted'): 'deleted',
    ('synchronized', 'unknown'): 'synchronized',

    # conflicts with automatic resolution
    ('created', 'deleted'): 'locally_created',
    ('deleted', 'created'): 'remotely_created',
    ('modified', 'deleted'): 'remotely_deleted',
    ('deleted', 'modified'): 'remotely_created',

    # conflict cases that need manual resolution
    ('modified', 'modified'): 'conflicted',
    ('created', 'created'): 'conflicted',
    ('created', 'modified'): 'conflicted',
    ('moved', 'unknown'): 'conflicted',
    ('moved', 'moved'): 'conflicted',

    # conflict cases that have been manually resolved
    ('resolved', 'unknown'): 'locally_resolved',

    # inconsistent cases
    ('unknown', 'deleted'): 'unknown_deleted',
    ('deleted', 'unknown'): 'deleted_unknown',
}


class AutoRetryCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        count = 1
        while True:
            count += 1
            try:
                return super(AutoRetryCursor, self).execute(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                log.debug('Retry locked database #%d, args=%r, kwargs=%r',
                          count, args, kwargs)
                if count > 5:
                    raise exc


class AutoRetryConnection(sqlite3.Connection):
    def cursor(self):
        return super(AutoRetryConnection, self).cursor(AutoRetryCursor)


class StateRow(sqlite3.Row):

    def __repr__(self):
        return ('<{name}[{cls.id!r}]'
                ' local_path={cls.local_path!r},'
                ' remote_ref={cls.remote_ref!r},'
                ' local_state={cls.local_state!r},'
                ' remote_state={cls.remote_state!r},'
                ' pair_state={cls.pair_state!r},'
                ' filter_path={cls.path!r}'
                '>'
                ).format(name=type(self).__name__, cls=self)

    def __getattr__(self, name):
        try:
            return self[name]
        except IndexError:
            return None

    def is_readonly(self):
        if self.folderish:
            return self.remote_can_create_child == 0
        return (self.remote_can_delete & self.remote_can_rename
                & self.remote_can_update) == 0

    def update_state(self, local_state=None, remote_state=None):
        if local_state is not None:
            self.local_state = local_state
        if remote_state is not None:
            self.remote_state = remote_state


class FakeLock(object):
    def acquire(self):
        pass

    __enter__ = acquire

    def release(self):
        pass

    def __exit__(self, t, v, tb):
        self.release()


class ConfigurationDAO(QObject):

    _state_factory = StateRow

    def __init__(self, db):
        super(ConfigurationDAO, self).__init__()
        log.debug('Create DAO on %r', db)
        self._db = db
        migrate = os.path.exists(self._db)
        # For testing purpose only should always be True
        self.share_connection = True
        self.auto_commit = True
        self.schema_version = self.get_schema_version()
        self.in_tx = None
        self._tx_lock = RLock()
        # If we dont share connection no need to lock
        self._lock = RLock() if self.share_connection else FakeLock()
        # Use to clean
        self._connections = []
        self._conns = local()
        self._create_main_conn()
        self._conn.row_factory = self._state_factory
        c = self._conn.cursor()
        self._init_db(c)
        if migrate:
            res = c.execute('SELECT value '
                            '  FROM Configuration '
                            ' WHERE name = ?', (SCHEMA_VERSION,)).fetchone()
            schema = int(res[0]) if res else 0
            if schema != self.schema_version:
                self._migrate_db(c, schema)
        else:
            c.execute('INSERT INTO Configuration (name, value) '
                      'VALUES (?, ?)', (SCHEMA_VERSION, self.schema_version))
        self._conn.commit()
        # FOR PYTHON 3.3...
        # if log.getEffectiveLevel() < 6:
        #    self._conn.set_trace_callback(self._log_trace)

    def get_schema_version(self):
        return 1

    def get_db(self):
        return self._db

    def _migrate_table(self, cursor, name):
        # Add the last_transfer
        tmpname = '{}Migration'.format(name)
        cursor.execute('ALTER TABLE {} RENAME TO {}'.format(name, tmpname))
        # Because Windows dont release the table, force the creation
        self._create_table(cursor, name, force=True)
        target_cols = self._get_columns(cursor, name)
        source_cols = self._get_columns(cursor, tmpname)
        cols = ', '.join(set(target_cols).intersection(source_cols))
        cursor.execute('INSERT INTO {} ({}) '
                       'SELECT {}'
                       '  FROM {}'.format(
            name, cols, cols, tmpname))
        cursor.execute('DROP TABLE {}'.format(tmpname))

    def _create_table(self, cursor, name, force=False):
        if name == 'Configuration':
            return self._create_configuration_table(cursor)

    def _get_columns(self, cursor, table):
        return [col.name for col in cursor.execute(
            "PRAGMA table_info('{}')".format(table)).fetchall()]

    def _migrate_db(self, cursor, version):
        if version < 1:
            self.update_config(SCHEMA_VERSION, 1)

    def _init_db(self, cursor):
        # http://www.stevemcarthur.co.uk/blog/post/some-kind-of-disk-io-error-occurred-sqlite
        cursor.execute('PRAGMA journal_mode = MEMORY')
        self._create_configuration_table(cursor)

    def _create_configuration_table(self, cursor):
        cursor.execute('CREATE TABLE if not exists Configuration ('
                       '    name    VARCHAR NOT NULL,'
                       '    value   VARCHAR,'
                       '    PRIMARY KEY (name)'
                       ')')

    def _create_main_conn(self):
        log.debug(
            'Create main connexion on %r (dir_exists=%r, file_exists=%r)',
            self._db, os.path.exists(os.path.dirname(self._db)),
            os.path.exists(self._db))
        self._conn = AutoRetryConnection(self._db, check_same_thread=False)
        self._conn.row_factory = self._state_factory
        self._connections.append(self._conn)

    def _log_trace(self, query):
        log.trace(query)

    def dispose(self):
        log.debug('Disposing SQLite database %r', self.get_db())
        for con in self._connections:
            con.close()
        self._connections = []
        self._conn = None

    def _get_write_connection(self):
        if self.share_connection or self.in_tx:
            if self._conn is None:
                self._create_main_conn()
            return self._conn
        return self._get_read_connection()

    def _get_read_connection(self):
        # If in transaction
        if self.in_tx is not None:
            if current_thread().ident != self.in_tx:
                log.trace('In transaction wait for read connection')
                # Wait for the thread in transaction to finished
                with self._tx_lock:
                    pass
            else:
                # Return the write connection
                return self._conn

        if getattr(self._conns, '_conn', None) is None:
            # Dont check same thread for closing purpose
            self._conns._conn = AutoRetryConnection(
                self._db, check_same_thread=False)
            self._conns._conn.row_factory = self._state_factory
            self._connections.append(self._conns._conn)

        # Python3.3 feature
        # if log.getEffectiveLevel() < 6:
        #     self._conns._conn.set_trace_callback(self._log_trace)
        return self._conns._conn

    def commit(self):
        if self.auto_commit:
            return
        with self._lock:
            self._get_write_connection().commit()

    def _delete_config(self, cursor, name):
        cursor.execute('DELETE FROM Configuration'
                       '       WHERE name = ?', (name,))

    def delete_config(self, name):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            self._delete_config(c, name)
            if self.auto_commit:
                con.commit()

    def update_config(self, name, value):
        if self.get_config(name) == value:
            return

        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE OR IGNORE Configuration'
                      '             SET value = ?'
                      '           WHERE name = ?', (value, name))
            c.execute('INSERT OR IGNORE INTO Configuration (value, name) '
                      'VALUES (?, ?)', (value, name))
            if self.auto_commit:
                con.commit()

    def get_config(self, name, default=None):
        c = self._get_read_connection().cursor()
        obj = c.execute('SELECT value'
                        '  FROM Configuration'
                        ' WHERE name = ?', (name,)).fetchone()
        if not obj or not obj.value:
            return default
        return obj.value


class ManagerDAO(ConfigurationDAO):

    def get_schema_version(self):
        return 2

    def _init_db(self, cursor):
        super(ManagerDAO, self)._init_db(cursor)
        cursor.execute('CREATE TABLE if not exists Engines ('
                       '    uid          VARCHAR,'
                       '    engine       VARCHAR NOT NULL,'
                       '    name         VARCHAR,'
                       '    local_folder VARCHAR NOT NULL UNIQUE,'
                       '    PRIMARY KEY (uid)'
                       ')')
        cursor.execute('CREATE TABLE if not exists Notifications ('
                       '    uid         VARCHAR UNIQUE,'
                       '    engine      VARCHAR,'
                       '    level       VARCHAR,'
                       '    title       VARCHAR,'
                       '    description VARCHAR,'
                       '    action      VARCHAR,'
                       '    flags       INT,'
                       '    PRIMARY KEY (uid)'
                       ')')
        cursor.execute('CREATE TABLE if not exists AutoLock ('
                       '    path      VARCHAR,'
                       '    remote_id VARCHAR,'
                       '    process   INT,'
                       '    PRIMARY KEY(path)'
                       ')')

    def insert_notification(self, notification):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute(
                'INSERT INTO Notifications '
                '(uid, engine, level, title, description, action, flags) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (notification.uid, notification.engine_uid,
                 notification.level, notification.title,
                 notification.description, notification.action,
                 notification.flags))
            if self.auto_commit:
                con.commit()

    def unlock_path(self, path):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('DELETE FROM AutoLock WHERE path = ?', (path,))
            if self.auto_commit:
                con.commit()

    def get_locked_paths(self):
        con = self._get_read_connection()
        c = con.cursor()
        return c.execute('SELECT * FROM AutoLock').fetchall()

    def lock_path(self, path, process, doc_id):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            try:
                c.execute('INSERT INTO AutoLock (path, process, remote_id) '
                          'VALUES (?, ?, ?)', (path, process, doc_id))
                if self.auto_commit:
                    con.commit()
            except sqlite3.IntegrityError:
                # Already there just update the process
                c.execute('UPDATE AutoLock'
                          '   SET process = ?,'
                          '       remote_id = ?'
                          ' WHERE path = ?', (process, doc_id, path))
                if self.auto_commit:
                    con.commit()

    def update_notification(self, notification):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE Notifications'
                      '   SET level = ?,'
                      '       title = ?,'
                      '       description = ?'
                      ' WHERE uid = ?',
                      (notification.level, notification.title,
                       notification.description, notification.uid))
            if self.auto_commit:
                con.commit()

    def get_notifications(self, discarded=True):
        # Flags used:
        #    1 = Notification.FLAG_DISCARD
        c = self._get_read_connection().cursor()
        req = ('SELECT *'
               '  FROM Notifications'
               ' WHERE (flags & 1) = 0')
        if discarded:
            req = 'SELECT * FROM Notifications'

        return c.execute(req).fetchall()

    def discard_notification(self, uid):
        # Flags used:
        #    1 = Notification.FLAG_DISCARD
        #    4 = Notification.FLAG_DISCARDABLE
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE Notifications'
                      '   SET flags = (flags | 1)'
                      ' WHERE uid = ?'
                      '   AND (flags & 4) = 4', (uid,))
            if self.auto_commit:
                con.commit()

    def remove_notification(self, uid):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('DELETE FROM Notifications WHERE uid = ?', (uid,))
            if self.auto_commit:
                con.commit()

    def _migrate_db(self, cursor, version):
        if version < 2:
            cursor.execute('CREATE TABLE if not exists Notifications ('
                           '    uid         VARCHAR,'
                           '    engine      VARCHAR,'
                           '    level       VARCHAR,'
                           '    title       VARCHAR,'
                           '    description VARCHAR,'
                           '    action      VARCHAR,'
                           '    flags       INT,'
                           '    PRIMARY KEY (uid)'
                           ')')
            self.update_config(SCHEMA_VERSION, 2)
        if version < 3:
            cursor.execute('CREATE TABLE if not exists AutoLock ('
                           '    path      VARCHAR,'
                           '    remote_id VARCHAR,'
                           '    process   INT,'
                           '    PRIMARY KEY (path)'
                           ')')
            self.update_config(SCHEMA_VERSION, 3)

    def get_engines(self):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT * FROM Engines').fetchall()

    def update_engine_path(self, engine, path):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE Engines'
                      '   SET local_folder = ?'
                      ' WHERE uid = ?', (path, engine))
            if self.auto_commit:
                con.commit()

    def add_engine(self, engine, path, key, name):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('INSERT INTO Engines (local_folder, engine, uid, name) '
                      'VALUES (?, ?, ?, ?)', (path, engine, key, name))
            if self.auto_commit:
                con.commit()
            result = c.execute('SELECT *'
                               '  FROM Engines'
                               ' WHERE uid = ?', (key,)).fetchone()
        return result

    def delete_engine(self, uid):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('DELETE FROM Engines WHERE uid = ?', (uid,))
            if self.auto_commit:
                con.commit()


class EngineDAO(ConfigurationDAO):
    newConflict = pyqtSignal(object)

    def __init__(self, db, state_factory=None):
        if state_factory:
            self._state_factory = state_factory

        super(EngineDAO, self).__init__(db)

        self._queue_manager = None
        self._items_count = 0
        self._items_count = self.get_syncing_count()
        self._filters = self.get_filters()
        self.reinit_processors()

    def get_schema_version(self):
        return 4

    def _migrate_state(self, cursor):
        try:
            self._migrate_table(cursor, 'States')
        except sqlite3.IntegrityError:
            # If we cannot smoothly migrate harder migration
            cursor.execute('DROP TABLE if exists StatesMigration')
            self._reinit_states(cursor)

    def _migrate_db(self, cursor, version):
        if version < 1:
            self._migrate_state(cursor)
            cursor.execute("UPDATE States"
                           "   SET last_transfer = 'upload'"
                           " WHERE last_local_updated < last_remote_updated"
                           "   AND folderish = 0")
            cursor.execute("UPDATE States"
                           "   SET last_transfer = 'download'"
                           " WHERE last_local_updated > last_remote_updated"
                           "   AND folderish = 0")
            self.update_config(SCHEMA_VERSION, 1)
        if version < 2:
            cursor.execute('CREATE TABLE if not exists ToRemoteScan ('
                           '    path STRING NOT NULL,'
                           '    PRIMARY KEY (path)'
                           ')')
            self.update_config(SCHEMA_VERSION, 2)
        if version < 3:
            self._migrate_state(cursor)
            self.update_config(SCHEMA_VERSION, 3)
        if version < 4:
            self._migrate_state(cursor)
            cursor.execute('UPDATE States'
                           '   SET creation_date = last_remote_updated')
            self.update_config(SCHEMA_VERSION, 4)


    def _create_table(self, cursor, name, force=False):
        if name == 'States':
            return self._create_state_table(cursor, force)
        super(EngineDAO, self)._create_table(cursor, name, force)

    @staticmethod
    def _create_state_table(cursor, force=False):
        statement = '' if force else 'if not exists'
        # Cannot force UNIQUE for a local_path as a duplicate can have
        # virtually the same path until they are resolved by Processor
        # Should improve that
        cursor.execute(
            "CREATE TABLE {} States ("
            "    id                      INTEGER    NOT NULL,"
            "    last_local_updated      TIMESTAMP,"
            "    last_remote_updated     TIMESTAMP,"
            "    local_digest            VARCHAR,"
            "    remote_digest           VARCHAR,"
            "    local_path              VARCHAR,"
            "    remote_ref              VARCHAR,"
            "    local_parent_path       VARCHAR,"
            "    remote_parent_ref       VARCHAR,"
            "    remote_parent_path      VARCHAR,"
            "    local_name              VARCHAR,"
            "    remote_name             VARCHAR,"
            "    size                    INTEGER    DEFAULT (0),"
            "    folderish               INTEGER,"
            "    local_state             VARCHAR    DEFAULT('unknown'),"
            "    remote_state            VARCHAR    DEFAULT('unknown'),"
            "    pair_state              VARCHAR    DEFAULT('unknown'),"
            "    remote_can_rename       INTEGER,"
            "    remote_can_delete       INTEGER,"
            "    remote_can_update       INTEGER,"
            "    remote_can_create_child INTEGER,"
            "    last_remote_modifier    VARCHAR,"
            "    last_sync_date          TIMESTAMP,"
            "    error_count             INTEGER    DEFAULT (0),"
            "    last_sync_error_date    TIMESTAMP,"
            "    last_error              VARCHAR,"
            "    last_error_details      TEXT,"
            "    version                 INTEGER    DEFAULT (0),"
            "    processor               INTEGER    DEFAULT (0),"
            "    last_transfer           VARCHAR,"
            "    creation_date           TIMESTAMP,"
            "    PRIMARY KEY (id),"
            "    UNIQUE(remote_ref, remote_parent_ref),"
            "    UNIQUE(remote_ref, local_path))".format(statement))

    def _init_db(self, cursor):
        super(EngineDAO, self)._init_db(cursor)
        for table in ('Filters', 'RemoteScan', 'ToRemoteScan'):
            cursor.execute('CREATE TABLE if not exists {} ('
                           '   path STRING NOT NULL,'
                           '   PRIMARY KEY (path)'
                           ')'.format(table))
        self._create_state_table(cursor)

    def acquire_state(self, thread_id, row_id):
        if self.acquire_processor(thread_id, row_id):
            # Avoid any lock for this call by using the write connection
            try:
                return self.get_state_from_id(row_id, from_write=True)
            except:
                self.release_processor(thread_id)
                raise
        raise sqlite3.OperationalError('Cannot acquire')

    def release_state(self, thread_id):
        self.release_processor(thread_id)

    def release_processor(self, processor_id):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            # TO_REVIEW Might go back to primary key id
            c.execute('UPDATE States'
                      '   SET processor = 0'
                      ' WHERE processor = ?',
                      (processor_id,))
            if self.auto_commit:
                con.commit()
        return c.rowcount > 0

    def acquire_processor(self, thread_id, row_id):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET processor = ?'
                      ' WHERE id = ?'
                      '   AND processor IN (0, ?)',
                      (thread_id, row_id, thread_id))
            if self.auto_commit:
                con.commit()
        return c.rowcount == 1

    def _reinit_states(self, cursor):
        cursor.execute('DROP TABLE States')
        self._create_state_table(cursor, force=True)
        for config in ('remote_last_sync_date', 'remote_last_event_log_id',
                       'remote_last_event_last_root_definitions',
                       'remote_last_full_scan', 'last_sync_date'):
            self._delete_config(cursor, config)

    def reinit_states(self):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            self._reinit_states(c)
            con.commit()
            con.execute('VACUUM')

    def reinit_processors(self):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET processor = 0')
            c.execute("UPDATE States"
                      "   SET error_count = 0,"
                      "       last_sync_error_date = NULL,"
                      "       last_error = NULL"
                      " WHERE pair_state = 'synchronized'")
            if self.auto_commit:
                con.commit()
            con.execute('VACUUM')

    def delete_remote_state(self, doc_pair):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            update = ("UPDATE States"
                      "   SET remote_state = 'deleted',"
                      "       pair_state = ?")
            c.execute('{} WHERE id = ?'.format(update),
                      ('remotely_deleted', doc_pair.id))
            if doc_pair.folderish:
                c.execute(update + ' '
                          + self._get_recursive_remote_condition(doc_pair),
                          ('parent_remotely_deleted',))
            # Only queue parent
            self._queue_pair_state(
                doc_pair.id, doc_pair.folderish, 'remotely_deleted')
            if self.auto_commit:
                con.commit()

    def delete_local_state(self, doc_pair):
        current_state = None
        try:
            with self._lock:
                con = self._get_write_connection()
                c = con.cursor()
                # Check parent to see current pair state
                parent = c.execute('SELECT * '
                                   '  FROM States'
                                   ' WHERE local_path = ?',
                                   (doc_pair.local_parent_path,)).fetchone()
                if parent and (
                        parent.pair_state in ('locally_deleted',
                                              'parent_locally_deleted')):
                    current_state = 'parent_locally_deleted'
                else:
                    current_state = 'locally_deleted'

                update = ("UPDATE States"
                          "   SET local_state = 'deleted',"
                          "       pair_state = ?")
                c.execute('{} WHERE id = ?'.format(update),
                          (current_state, doc_pair.id))
                if doc_pair.folderish:
                    c.execute(update + ' '
                              + self._get_recursive_condition(doc_pair),
                              ('parent_locally_deleted',))
                if self.auto_commit:
                    con.commit()
        finally:
            self._queue_manager.interrupt_processors_on(
                doc_pair.local_path, exact_match=False)
            # Only queue parent
            if current_state and current_state == 'locally_deleted':
                self._queue_pair_state(
                    doc_pair.id, doc_pair.folderish, current_state)

    def insert_local_state(self, info, parent_path):
        pair_state = PAIR_STATES.get(('created', 'unknown'))
        digest = info.get_digest()
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            name = os.path.basename(info.path)
            c.execute("INSERT INTO States "
                      "(last_local_updated, local_digest, local_path, "
                      "local_parent_path, local_name, folderish, size, "
                      "local_state, remote_state, pair_state) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, 'created', 'unknown', ?)",
                (info.last_modification_time, digest, info.path,
                 parent_path, name, info.folderish, info.size, pair_state))
            row_id = c.lastrowid
            parent = c.execute('SELECT *'
                               '  FROM States'
                               ' WHERE local_path = ?',
                               (parent_path,)).fetchone()
            # Don't queue if parent is not yet created
            if ((parent is None and parent_path == '')
                    or (parent and parent.pair_state != 'locally_created')):
                self._queue_pair_state(row_id, info.folderish, pair_state)
            if self.auto_commit:
                con.commit()
            self._items_count += 1
        return row_id

    def get_last_files(self, number, direction=''):
        c = self._get_read_connection().cursor()
        conditions = {'remote': "AND last_transfer = 'upload'",
                      'local': "AND last_transfer = 'download'"}
        condition = conditions.get(direction, '')
        return c.execute("SELECT *"
                         "  FROM States"
                         " WHERE pair_state = 'synchronized'"
                         "   AND folderish = 0 {} "
                         " ORDER BY last_sync_date DESC"
                         " LIMIT {}".format(
                            condition, number)).fetchall()

    def _get_to_sync_condition(self):
        return ("pair_state != 'synchronized' "
                "AND pair_state != 'unsynchronized'")

    def register_queue_manager(self, manager):
        # Prevent any update while init queue
        with self._lock:
            self._queue_manager = manager
            con = self._get_write_connection()
            c = con.cursor()
            # Order by path to be sure to process parents before childs
            pairs = c.execute('SELECT *'
                              '  FROM States'
                              ' WHERE {}'
                              ' ORDER BY local_path ASC'.format(
                                  self._get_to_sync_condition())).fetchall()
            folders = dict()
            for pair in pairs:
                # Add all the folders
                if pair.folderish:
                    folders[pair.local_path] = True
                if pair.local_parent_path not in folders:
                    self._queue_manager.push_ref(
                        pair.id, pair.folderish, pair.pair_state)
        # Dont block everything if queue manager fail
        # TODO As the error should be fatal not sure we need this

    def _queue_pair_state(self, row_id, folderish, pair_state, pair=None):
        if (self._queue_manager
                and pair_state not in ('synchronized', 'unsynchronized')):
            if pair_state == 'conflicted':
                log.trace('Emit newConflict with: %r, pair=%r', row_id, pair)
                self.newConflict.emit(row_id)
            else:
                log.trace('Push to queue: %s, pair=%r', pair_state, pair)
                self._queue_manager.push_ref(row_id, folderish, pair_state)
        else:
            log.trace('Will not push pair: %s, pair=%r', pair_state, pair)

    def _get_pair_state(self, row):
        return PAIR_STATES.get((row.local_state, row.remote_state))

    def update_last_transfer(self, row_id, transfer):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET last_transfer = ?'
                      ' WHERE id = ?',
                      (transfer, row_id))
            if self.auto_commit:
                con.commit()

    def get_dedupe_pair(self, name, parent, row_id):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE id != ?'
                         '   AND local_name = ?'
                         '   AND remote_parent_ref = ?',
                         (row_id, name, parent)).fetchone()

    def remove_local_path(self, row_id):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute("UPDATE States"
                      "   SET local_path = ''"
                      " WHERE id = ?", (row_id,))
            if self.auto_commit:
                con.commit()

    def update_local_state(self, row, info, versioned=True, queue=True):
        row.pair_state = self._get_pair_state(row)
        log.trace('Updating local state for row=%r with info=%r', row, info)

        version = ''
        if versioned:
            version = ', version = version + 1'
            log.trace('Increasing version to %d for pair %r',
                      row.version + 1, row)

        parent_path = os.path.dirname(info.path)
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET last_local_updated = ?,'
                      '       local_digest = ?,'
                      '       local_path = ?,'
                      '       local_parent_path = ?, '
                      '       local_name = ?, '
                      '       local_state = ?,'
                      '       size = ?,'
                      '       remote_state = ?, '
                      '       pair_state = ? {version}'
                      ' WHERE id = ?'.format(version=version),
                (info.last_modification_time, row.local_digest,
                 info.path, parent_path, os.path.basename(info.path),
                 row.local_state, info.size, row.remote_state,
                 row.pair_state, row.id,))
            if queue:
                parent = c.execute('SELECT *'
                                   '  FROM States'
                                   ' WHERE local_path = ?',
                                   (parent_path,)).fetchone()
                # Don't queue if parent is not yet created
                if ((not parent and not parent_path)
                        or (parent and parent.local_state != 'created')):
                    self._queue_pair_state(
                        row.id, info.folderish, row.pair_state, pair=row)
            if self.auto_commit:
                con.commit()

    def update_local_modification_time(self, row, info):
        self.update_local_state(row, info, versioned=False, queue=False)

    def get_valid_duplicate_file(self, digest):
        c = self._get_read_connection().cursor()
        return c.execute("SELECT *"
                         "  FROM States"
                         " WHERE remote_digest = ?"
                         "   AND pair_state = 'synchronized'",
                         (digest,)).fetchone()

    def get_remote_descendants(self, path):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_parent_path LIKE ?',
                         ('{}%'.format(path),)).fetchall()

    def get_remote_descendants_from_ref(self, ref):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_parent_path LIKE ?',
                         ('%{}%'.format(ref),)).fetchall()

    def get_remote_children(self, ref):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_parent_ref = ?',
                         (ref,)).fetchall()

    def get_new_remote_children(self, ref):
        c = self._get_read_connection().cursor()
        return c.execute("SELECT *"
                         "  FROM States"
                         " WHERE remote_parent_ref = ?"
                         "   AND remote_state = 'created'"
                         "   AND local_state = 'unknown'",
                         (ref,)).fetchall()

    def get_unsynchronized_count(self):
        return self.get_count("pair_state = 'unsynchronized'")

    def get_conflict_count(self):
        return self.get_count("pair_state = 'conflicted'")

    def get_error_count(self, threshold=3):
        return self.get_count('error_count > {}'.format(str(threshold)))

    def get_syncing_count(self, threshold=3):
        count = self.get_count("    pair_state != 'synchronized' "
                               "AND pair_state != 'conflicted' "
                               "AND pair_state != 'unsynchronized' "
                               "AND error_count < {}".format(
                                   str(threshold)))
        if self._items_count != count:
            log.trace('Cache Syncing count incorrect should be %d was %d',
                      count, self._items_count)
            self._items_count = count
        return count

    def get_sync_count(self, filetype=None):
        conditions = {'file': 'AND folderish = 0',
                      'folder': 'AND folderish = 1'}
        condition = conditions.get(filetype, '')
        return self.get_count(
            "pair_state = 'synchronized' {}".format(condition))

    def get_count(self, condition=None):
        query = ('SELECT COUNT(*) as count'
                 '  FROM States')
        if condition:
            query = '{} WHERE {}'.format(query, condition)
        c = self._get_read_connection().cursor()
        return c.execute(query).fetchone().count

    def get_global_size(self):
        c = self._get_read_connection().cursor()
        total = c.execute("SELECT SUM(size) as sum"
                          "  FROM States"
                          " WHERE folderish = 0"
                          "   AND pair_state = 'synchronized'").fetchone().sum
        # `total` may be `None` if there is not synced files,
        # so we ensure to have an int at the end
        return total or 0

    def get_unsynchronizeds(self):
        c = self._get_read_connection().cursor()
        return c.execute("SELECT *"
                         "  FROM States"
                         " WHERE pair_state = 'unsynchronized'").fetchall()

    def get_conflicts(self):
        c = self._get_read_connection().cursor()
        return c.execute("SELECT *"
                         "  FROM States"
                         " WHERE pair_state = 'conflicted'").fetchall()

    def get_errors(self, limit=3):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE error_count > ?',
                         (limit,)).fetchall()

    def get_local_children(self, path):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE local_parent_path = ?',
                         (path,)).fetchall()

    def get_states_from_partial_local(self, path):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE local_path LIKE ?',
                         ('{}%'.format(path),)).fetchall()

    def get_first_state_from_partial_remote(self, ref):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_ref LIKE ? '
                         ' ORDER BY last_remote_updated ASC'
                         ' LIMIT 1', ('%{}'.format(ref),)).fetchone()

    def get_normal_state_from_remote(self, ref):
        # TODO Select the only states that is not a collection
        states = self.get_states_from_remote(ref)
        return states[0] if states else None

    def get_state_from_remote_with_path(self, ref, path):
        # remote_path root is empty, should refactor this
        path = '' if path == '/' else path
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_ref = ?'
                         '   AND remote_parent_path = ?',
                             (ref, path)).fetchone()

    def get_states_from_remote(self, ref):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_ref = ?',
                         (ref,)).fetchall()

    def get_state_from_id(self, row_id, from_write=False):
        # Dont need to read from write as auto_commit is True
        if from_write and self.auto_commit:
            from_write = False
        try:
            if from_write:
                self._lock.acquire()
                c = self._get_write_connection().cursor()
            else:
                c = self._get_read_connection().cursor()
            state = c.execute('SELECT *'
                              '  FROM States'
                              ' WHERE id = ?',
                              (row_id,)).fetchone()
        finally:
            if from_write:
                self._lock.release()
        return state

    def _get_recursive_condition(self, doc_pair):
        path = self._escape(doc_pair.local_path)
        res = (" WHERE (local_parent_path LIKE '" + path + "/%'"
               "        OR local_parent_path = '" + path + "')")
        if doc_pair.remote_ref:
            path = self._escape(doc_pair.remote_parent_path
                                + '/' + doc_pair.remote_ref)
            res += " AND remote_parent_path LIKE '" + path + "%'"
        return res

    def _get_recursive_remote_condition(self, doc_pair):
        path = self._escape(doc_pair.remote_parent_path.encode('utf-8')
                            + '/' + doc_pair.remote_name)
        return (" WHERE remote_parent_path LIKE '" + path + "/%'"
                "    OR remote_parent_path = '" + path + "'")

    def update_remote_parent_path(self, doc_pair, new_path):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            if doc_pair.folderish:
                count = str(len(doc_pair.remote_parent_path
                                + '/' + doc_pair.remote_ref) + 1)
                path = self._escape(new_path + '/' + doc_pair.remote_ref)
                query = ("UPDATE States"
                         "   SET remote_parent_path = '" + path + "'"
                         "     || substr(remote_parent_path, " + count + ")"
                         + self._get_recursive_remote_condition(doc_pair))

                log.trace('Update remote_parent_path %r', query)
                c.execute(query)
            c.execute('UPDATE States'
                      '   SET remote_parent_path = ?'
                      ' WHERE id = ?',
                      (new_path, doc_pair.id))
            if self.auto_commit:
                con.commit()

    def update_local_parent_path(self, doc_pair, new_name, new_path):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            if doc_pair.folderish:
                if new_path == '/':
                    new_path = ''
                path = self._escape(new_path + '/' + new_name)
                count = str(len(doc_pair.local_path) + 1)
                query = ("UPDATE States"
                         "   SET local_parent_path = '" + path + "'"
                         "       || substr(local_parent_path, " + count + "),"
                         "          local_path = '" + path + "'"
                         "       || substr(local_path, " + count + ") "
                         + self._get_recursive_condition(doc_pair))
                c.execute(query)
            # Dont need to update the path as it is refresh later
            c.execute('UPDATE States'
                      '   SET local_parent_path = ?'
                      ' WHERE id = ?',
                      (new_path, doc_pair.id))
            if self.auto_commit:
                con.commit()

    def mark_descendants_remotely_created(self, doc_pair):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            update = ("UPDATE States"
                      "   SET local_digest = NULL,"
                      "       last_local_updated = NULL,"
                      "       local_name = NULL,"
                      "       remote_state = 'created',"
                      "       pair_state = 'remotely_created'")
            c.execute('{} WHERE id = {}'.format(update, str(doc_pair.id)))
            if doc_pair.folderish:
                c.execute('{} {}'.format(
                    update, self._get_recursive_condition(doc_pair)))
            if self.auto_commit:
                con.commit()
            self._queue_pair_state(
                doc_pair.id, doc_pair.folderish, doc_pair.pair_state)

    def remove_state(self, doc_pair, remote_recursion=False):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('DELETE FROM States'
                      ' WHERE id = ?', (doc_pair.id,))
            if doc_pair.folderish:
                if remote_recursion:
                    condition = self._get_recursive_remote_condition(doc_pair)
                else:
                    condition = self._get_recursive_condition(doc_pair)
                c.execute('DELETE FROM States ' + condition)
            if self.auto_commit:
                con.commit()

    def get_state_from_local(self, path):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE local_path = ?',
                         (path,)).fetchone()

    def insert_remote_state(self, info, remote_parent_path, local_path,
                            local_parent_path):
        pair_state = PAIR_STATES.get(('unknown', 'created'))
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute("INSERT INTO States "
                      "(remote_ref, remote_parent_ref, remote_parent_path, "
                      "remote_name, last_remote_updated, remote_can_rename, "
                      "remote_can_delete, remote_can_update, "
                      "remote_can_create_child, last_remote_modifier, "
                      "remote_digest, folderish, last_remote_modifier, "
                      "local_path, local_parent_path, remote_state, "
                      "local_state, pair_state, local_name, creation_date) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                      "'created', 'unknown', ?, ?, ?)",
                      (info.uid, info.parent_uid, remote_parent_path,
                       info.name, info.last_modification_time, info.can_rename,
                       info.can_delete, info.can_update,
                       info.can_create_child, info.last_contributor,
                       info.digest, info.folderish, info.last_contributor,
                       local_path, local_parent_path, pair_state, info.name,
                       info.creation_time))
            row_id = c.lastrowid
            if self.auto_commit:
                con.commit()
            # Check if parent is not in creation
            parent = c.execute('SELECT *'
                               '  FROM States'
                               ' WHERE remote_ref = ?',
                               (info.parent_uid,)).fetchone()
            if ((parent is None and local_parent_path == '')
                    or (parent and parent.pair_state != 'remotely_created')):
                self._queue_pair_state(row_id, info.folderish, pair_state)
            self._items_count += 1
        return row_id

    def queue_children(self, row):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            children = c.execute('SELECT *'
                                 '  FROM States'
                                 ' WHERE remote_parent_ref = ?'
                                 '    OR local_parent_path = ?'
                                 '   AND ' + self._get_to_sync_condition(),
                (row.remote_ref, row.local_path)).fetchall()
            log.debug('Queuing %d children of %r', len(children), row)
            for child in children:
                self._queue_pair_state(
                    child.id, child.folderish, child.pair_state)

    def increase_error(self, row, error, details=None, incr=1):
        error_date = datetime.utcnow()
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET last_error = ?,'
                      '       last_sync_error_date = ?,'
                      '       error_count = error_count + ?,'
                      '       last_error_details = ?'
                      ' WHERE id = ?',
                      (error, error_date, incr, details, row.id))
            if self.auto_commit:
                con.commit()
        row.last_error = error
        row.error_count += incr

    def reset_error(self, row, last_error=None):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET last_error = ?,'
                      '       last_error_details = NULL,'
                      '       last_sync_error_date = NULL,'
                      '       error_count = 0'
                      ' WHERE id = ?',
                      (last_error, row.id))
            if self.auto_commit:
                con.commit()
            self._queue_pair_state(row.id, row.folderish, row.pair_state)
            self._items_count += 1
        row.last_error = None
        row.error_count = 0

    def _force_sync(self, row, local, remote, pair):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET local_state = ?,'
                      '       remote_state = ?,'
                      '       pair_state = ?,'
                      '       last_error = NULL,'
                      '       last_sync_error_date = NULL,'
                      '       error_count = 0'
                      ' WHERE id = ?'
                      '   AND version = ?',
                      (local, remote, pair, row.id, row.version))
            self._queue_pair_state(row.id, row.folderish, pair)
            if self.auto_commit:
                con.commit()
        if c.rowcount == 1:
            self._items_count += 1
            return True
        return False

    def force_remote(self, row):
        return self._force_sync(
            row, 'synchronized', 'modified', 'remotely_modified')

    def force_local(self, row):
        return self._force_sync(
            row, 'resolved', 'unknown', 'locally_resolved')

    def set_conflict_state(self, row):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET pair_state = ?'
                      ' WHERE id = ?',
                      ('conflicted', row.id))
            self.newConflict.emit(row.id)
            if self.auto_commit:
                con.commit()
        if c.rowcount == 1:
            self._items_count -= 1
            return True
        return False

    def unsynchronize_state(self, row, last_error=None):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET pair_state = ?,'
                      '       last_sync_date = ?,'
                      '       processor = 0,'
                      '       last_error = ?,'
                      '       error_count = 0,'
                      '       last_sync_error_date = NULL'
                      ' WHERE id = ?',
                      ('unsynchronized', datetime.utcnow(),
                       last_error, row.id))
            if self.auto_commit:
                con.commit()

    def synchronize_state(self, row, version=None, dynamic_states=False):
        if version is None:
            version = row.version
        log.trace('Try to synchronize state for [local_path=%r, '
                  'remote_name=%r, version=%s] with version=%s '
                  'and dynamic_states=%r',
                  row.local_path, row.remote_name,
                  row.version, version, dynamic_states)

        # Set default states to synchronized, if wanted
        if not dynamic_states:
            row.local_state = row.remote_state = 'synchronized'
        row.pair_state = self._get_pair_state(row)

        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('UPDATE States'
                      '   SET local_state = ?,'
                      '       remote_state = ?,'
                      '       pair_state = ?,'
                      '       local_digest = ?,'
                      '       last_sync_date = ?,'
                      '       processor = 0,'
                      '       last_error = NULL,'
                      '       last_error_details = NULL,'
                      '       error_count = 0,'
                      '       last_sync_error_date = NULL'
                      ' WHERE id = ?'
                      '   AND version = ?',
                      (row.local_state, row.remote_state, row.pair_state,
                       row.local_digest, datetime.utcnow(), row.id, version))
            if self.auto_commit:
                con.commit()
        result = c.rowcount == 1

        # Retry without version for folder
        if not result and row.folderish:
            with self._lock:
                con = self._get_write_connection()
                c = con.cursor()
                c.execute('UPDATE States'
                          '   SET local_state = ?,'
                          '       remote_state = ?,'
                          '       pair_state = ?,'
                          '       last_sync_date = ?,'
                          '       processor = 0,'
                          '       last_error = NULL,'
                          '       error_count = 0,'
                          '       last_sync_error_date = NULL'
                          ' WHERE id = ?'
                          '   AND local_path = ?'
                          '   AND remote_name = ?'
                          '   AND remote_ref = ?'
                          '   AND remote_parent_ref = ?',
                          (row.local_state, row.remote_state, row.pair_state,
                           datetime.utcnow(), row.id, row.local_path,
                           row.remote_name, row.remote_ref,
                           row.remote_parent_ref))
                if self.auto_commit:
                    con.commit()
            result = c.rowcount == 1

        if not result:
            log.trace('Was not able to synchronize state: %r', row)
            c = self._get_read_connection().cursor()
            row2 = c.execute('SELECT *'
                             '  FROM States'
                             ' WHERE id = ?',
                             (row.id,)).fetchone()
            if row2 is None:
                log.trace('No more row')
            else:
                log.trace('Current row=%r (version=%r)', row2, row2.version)
            log.trace('Previous row=%r (version=%r)', row, row.version)
        elif row.folderish:
            self.queue_children(row)

        return result

    def update_remote_state(self, row, info, remote_parent_path=None,
                            versioned=True, queue=True, force_update=False,
                            no_digest=False):
        row.pair_state = self._get_pair_state(row)
        if remote_parent_path is None:
            remote_parent_path = row.remote_parent_path

        # Check if it really needs an update
        if (row.remote_ref == info.uid
                and info.parent_uid == row.remote_parent_ref
                and remote_parent_path == row.remote_parent_path
                and info.name == row.remote_name
                and info.can_rename == row.remote_can_rename
                and info.can_delete == row.remote_can_delete
                and info.can_update == row.remote_can_update
                and info.can_create_child == row.remote_can_create_child):
            bname = os.path.basename(row.local_path)
            if (bname == info.name
                    or (sys.platform == 'win32'
                        and bname.strip() == info.name.strip())):
                # It looks similar
                if info.digest in (row.local_digest, row.remote_digest):
                    row.remote_state = 'synchronized'
                    row.pair_state = self._get_pair_state(row)
                if info.digest == row.remote_digest and not force_update:
                    log.trace('Not updating remote state (not dirty)'
                              ' for row=%r with info=%r', row, info)
                    return

        log.trace('Updating remote state for row=%r with info=%r (force=%r)',
                  row, info, force_update)

        if (row.pair_state not in ('conflicted', 'remotely_created')
                and row.folderish
                and row.local_name
                and row.local_name != info.name
                and row.local_state != 'resolved'):
            # We check the current pair_state to not interfer with conflicted
            # documents (a move on both sides) nor with newly remotely
            # created ones.
            row.remote_state = 'modified'
            row.pair_state = self._get_pair_state(row)

        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            query = ('UPDATE States'
                     '   SET remote_ref = ?,'
                     '       remote_parent_ref = ?,'
                     '       remote_parent_path = ?,'
                     '       remote_name = ?,'
                     '       last_remote_updated = ?,'
                     '       remote_can_rename = ?,'
                     '       remote_can_delete = ?,'
                     '       remote_can_update = ?,'
                     '       remote_can_create_child = ?,'
                     '       last_remote_modifier = ?,'
                     '       local_state = ?,'
                     '       remote_state = ?,'
                     '       pair_state = ?')

            if not no_digest and info.digest is not None:
                query += ", remote_digest = '{}'".format(info.digest)

            if versioned:
                query += ', version = version+1'
                log.trace('Increasing version to %d for pair %r',
                          row.version + 1, row)

            query += ' WHERE id = ?'
            c.execute(query,
                      (info.uid, info.parent_uid, remote_parent_path,
                       info.name, info.last_modification_time,
                       info.can_rename, info.can_delete, info.can_update,
                       info.can_create_child, info.last_contributor,
                       row.local_state, row.remote_state, row.pair_state,
                       row.id))
            if self.auto_commit:
                con.commit()
            if queue:
                # Check if parent is not in creation
                parent = c.execute('SELECT *'
                                   '  FROM States'
                                   ' WHERE remote_ref = ?',
                    (info.parent_uid,)).fetchone()
                # Parent can be None if the parent is filtered
                if ((parent and parent.pair_state != 'remotely_created')
                        or parent is None):
                    self._queue_pair_state(
                        row.id, info.folderish, row.pair_state)

    def _clean_filter_path(self, path):
        if not path.endswith('/'):
            path += '/'
        return path

    def add_path_to_scan(self, path):
        path = self._clean_filter_path(path)
        try:
            with self._lock:
                con = self._get_write_connection()
                c = con.cursor()
                # Remove any subchilds as it is gonna be scanned anyway
                c.execute('DELETE FROM ToRemoteScan'
                          ' WHERE path LIKE ?',
                          (path + '%',))
                # ADD IT
                c.execute('INSERT INTO ToRemoteScan (path) '
                          'VALUES (?)', (path,))
                if self.auto_commit:
                    con.commit()
        except sqlite3.IntegrityError:
            pass

    def delete_path_to_scan(self, path):
        path = self._clean_filter_path(path)
        try:
            with self._lock:
                con = self._get_write_connection()
                c = con.cursor()
                # ADD IT
                c.execute('DELETE FROM ToRemoteScan'
                          ' WHERE path = ?', (path,))
                if self.auto_commit:
                    con.commit()
        except sqlite3.IntegrityError:
            pass

    def get_paths_to_scan(self):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM ToRemoteScan').fetchall()

    def add_path_scanned(self, path):
        path = self._clean_filter_path(path)
        try:
            with self._lock:
                con = self._get_write_connection()
                c = con.cursor()
                # ADD IT
                c.execute('INSERT INTO RemoteScan (path) '
                          'VALUES (?)', (path,))
                if self.auto_commit:
                    con.commit()
        except sqlite3.IntegrityError:
            pass

    def clean_scanned(self):
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('DELETE FROM RemoteScan')
            if self.auto_commit:
                con.commit()

    def is_path_scanned(self, path):
        path = self._clean_filter_path(path)
        c = self._get_read_connection().cursor()
        row = c.execute('SELECT COUNT(path)'
                        '  FROM RemoteScan'
                        ' WHERE path = ?'
                        ' LIMIT 1',
            (path,)).fetchone()
        return row[0] > 0

    @staticmethod
    def get_batch_sync_ignore():
        return ("AND (pair_state != 'unsynchronized' "
                "AND pair_state != 'conflicted') "
                "AND folderish = 0 ")

    def _get_adjacent_sync_file(self, ref, comp, order, sync_mode=None):
        state = self.get_normal_state_from_remote(ref)
        if state is None:
            return None

        mode = "AND last_transfer='{}' ".format(sync_mode) if sync_mode else ''
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE last_sync_date {} ?'
                         ' {}{} '
                         ' ORDER BY last_sync_date {}'
                         ' LIMIT 1'.format(comp, mode,
                                           self.get_batch_sync_ignore(),
                                           order),
                         (state.last_sync_date,)).fetchone()

    def get_previous_sync_file(self, ref, sync_mode=None):
        return self._get_adjacent_sync_file(ref, '>', 'ASC', sync_mode)

    def get_next_sync_file(self, ref, sync_mode=None):
        return self._get_adjacent_sync_file(ref, '<', 'DESC', sync_mode)

    def _get_adjacent_folder_file(self, ref, comp, order):
        state = self.get_normal_state_from_remote(ref)
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM States'
                         ' WHERE remote_parent_ref = ?'
                         '   AND remote_name {} ?'
                         '   AND folderish = 0'
                         ' ORDER BY remote_name {}'
                         ' LIMIT 1'.format(comp, order),
                         (state.remote_parent_ref,
                          state.remote_name)).fetchone()

    def get_next_folder_file(self, ref):
        return self._get_adjacent_folder_file(ref, '>', 'ASC')

    def get_previous_folder_file(self, ref):
        return self._get_adjacent_folder_file(ref, '<', 'DESC')

    def is_filter(self, path):
        path = self._clean_filter_path(path)
        return any(path.startswith(doc.path) for doc in self._filters)

    def get_filters(self):
        c = self._get_read_connection().cursor()
        return c.execute('SELECT *'
                         '  FROM Filters').fetchall()

    def add_filter(self, path):
        if self.is_filter(path):
            return

        path = self._clean_filter_path(path)
        log.trace('Add filter on %r', path)

        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            # Delete any subfilters
            c.execute('DELETE FROM Filters WHERE path LIKE ?', (path + '%',))

            # Prevent any rescan
            c.execute('DELETE FROM ToRemoteScan'
                      ' WHERE path LIKE ?', (path + '%',))

            # Add it
            c.execute('INSERT INTO Filters (path) VALUES (?)', (path,))

            # TODO: Add this path as remotely_deleted?

            if self.auto_commit:
                con.commit()

            self._filters = self.get_filters()
            self._items_count = self.get_syncing_count()

    def remove_filter(self, path):
        path = self._clean_filter_path(path)
        with self._lock:
            con = self._get_write_connection()
            c = con.cursor()
            c.execute('DELETE FROM Filters'
                      ' WHERE path LIKE ?', (path + '%',))
            if self.auto_commit:
                con.commit()
            self._filters = self.get_filters()
            self._items_count = self.get_syncing_count()

    @staticmethod
    def _escape(_str):
        return _str.replace("'", "''")