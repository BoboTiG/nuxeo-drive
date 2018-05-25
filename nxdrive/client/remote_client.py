# coding: utf-8
import os
import socket
import tempfile
import time
import unicodedata
from collections import namedtuple
from datetime import datetime
from logging import getLogger
from threading import Lock, current_thread

from dateutil import parser
from nuxeo.auth import TokenAuth
from nuxeo.client import Nuxeo
from nuxeo.compat import get_text
from nuxeo.exceptions import HTTPError
from nuxeo.models import FileBlob

from . import NotFound
from ..constants import (APP_NAME, DEFAULT_TYPES, DOWNLOAD_TMP_FILE_PREFIX,
                         DOWNLOAD_TMP_FILE_SUFFIX, FILE_BUFFER_SIZE,
                         MAX_CHILDREN, TIMEOUT, TOKEN_PERMISSION, TX_TIMEOUT)
from ..engine.activity import Action, FileAction
from ..options import Options
from ..utils import (get_device, lock_path, make_tmp_file, unlock_path,
                     version_le)

log = getLogger(__name__)

socket.setdefaulttimeout(TX_TIMEOUT)


# Data Transfer Object for remote file info
RemoteFileInfo = namedtuple('RemoteFileInfo', [
    'name',  # title of the file (not guaranteed to be locally unique)
    'uid',  # id of the file
    'parent_uid',  # id of the parent file
    'path',  # abstract file system path: useful for ordering folder trees
    'folderish',  # True is can host children
    'last_modification_time',  # last update time
    'creation_time',  # creation time
    'last_contributor',  # last contributor
    'digest',  # digest of the file
    'digest_algorithm',  # digest algorithm of the file
    'download_url',  # download URL of the file
    'can_rename',  # True is can rename
    'can_delete',  # True is can delete
    'can_update',  # True is can update content
    'can_create_child',  # True is can create child
    'lock_owner',  # lock owner
    'lock_created',  # lock creation time
    'can_scroll_descendants'  # True if the API to scroll through
                              # the descendants can be used
])


# Data Transfer Object for doc info on the Remote Nuxeo repository
NuxeoDocumentInfo = namedtuple('NuxeoDocumentInfo', [
    'root',  # ref of the document that serves as sync root
    'name',  # title of the document (not guaranteed to be locally unique)
    'uid',   # ref of the document
    'parent_uid',  # ref of the parent document
    'path',  # remote path (useful for ordering)
    'folderish',  # True is can host child documents
    'last_modification_time',  # last update time
    'last_contributor',  # last contributor
    'digest_algorithm',  # digest algorithm of the document's blob
    'digest',  # digest of the document's blob
    'repository',  # server repository name
    'doc_type',  # Nuxeo document type
    'version',  # Nuxeo version
    'state',  # Nuxeo lifecycle state
    'is_trashed',  # Nuxeo trashed status
    'has_blob',  # If this doc has blob
    'filename',  # Filename of document
    'lock_owner',  # lock owner
    'lock_created',  # lock creation time
    'permissions',  # permissions
])


class Remote(Nuxeo):

    def __init__(
            self,
            url,  # type: Text
            user_id,  # type: Text
            device_id,  # type: Text
            version,  # type: Text
            dao=None,  # type: Optional[Any]
            proxy=None,  # type: Type[Proxy]
            password=None,  # type: Optional[Text]
            token=None,  # type: Optional[Text]
            repository=Options.remote_repo,  # type: Text
            timeout=TIMEOUT,  # type: int
            upload_tmp_dir=None,  # type: Optional[Text]
            check_suspended=None,  # type: Optional[Callable]
            base_folder=None,  # type: Optional[Text]
            **kwargs  # type: Any
    ):
        auth = TokenAuth(token) if token else (user_id, password)
        self.kwargs = kwargs

        super(Remote, self).__init__(
            auth=auth,
            host=url,
            app_name=APP_NAME,
            version=version,
            repository=repository,
            **kwargs)

        self.client.headers.update({
            'X-User-Id': user_id,
            'X-Device-Id': device_id,
            'Cache-Control': 'no-cache',
        })

        self.set_proxy(proxy)

        if dao:
            self._dao = dao

        self.timeout = timeout if timeout > 0 else TIMEOUT

        self.device_id = device_id
        self.user_id = user_id
        self.version = version
        self.check_suspended = check_suspended
        self._has_new_trash_service = not version_le(
            self.client.server_version, '10.1')

        self.upload_tmp_dir = (upload_tmp_dir if upload_tmp_dir is not None
                               else tempfile.gettempdir())

        self.upload_lock = Lock()

        if base_folder is not None:
            base_folder_doc = self.fetch(base_folder)
            self._base_folder_ref = base_folder_doc['uid']
            self._base_folder_path = base_folder_doc['path']
        else:
            self._base_folder_ref, self._base_folder_path = None, None

    def __repr__(self):
        attrs = sorted(self.__init__.__code__.co_varnames[1:])
        attrs = ', '.join('{}={!r}'.format(attr, getattr(self, attr, None))
                          for attr in attrs)
        return '<{} {}>'.format(self.__class__.__name__, attrs)

    def request_token(self, revoke=False):
        # type: (bool) -> Text
        """Request and return a new token for the user"""
        return self.client.request_auth_token(
            device_id=self.device_id, app_name=APP_NAME,
            permission=TOKEN_PERMISSION, device=get_device(), revoke=revoke)

    def revoke_token(self):
        # type: () -> Text
        return self.request_token(revoke=True)

    def download(self, url, file_out=None, digest=None, **kwargs):
        # type: (Text, Optional[Text], Optional[Text], Any) -> Text
        log.trace('Downloading file from %r to %r with digest=%r',
                  url, file_out, digest)

        resp = self.client.request(
            'GET', url.replace(self.client.host, ''))

        current_action = Action.get_current_action()
        if current_action and resp:
            current_action.size = int(resp.headers.get('Content-Length', 0))

        if file_out:
            check_suspended = kwargs.pop(
                'check_suspended', self.check_suspended)
            locker = unlock_path(file_out)
            try:
                self.operations.save_to_file(
                    current_action,
                    resp,
                    file_out,
                    digest=digest,
                    chunk_size=FILE_BUFFER_SIZE,
                    check_suspended=check_suspended)
            finally:
                lock_path(file_out, locker)
            return file_out
        else:
            result = resp.content
            return result

    def upload(
            self,
            file_path,  # type: Text
            filename=None,  # type: Optional[Text]
            mime_type=None,   # type: Optional[Text]
            command=None,  # type: Optional[Text]
            **params  # type: Any
    ):
        """ Upload a file with a batch.

        If command is not None, the operation is executed
        with the batch as an input.
        """
        with self.upload_lock:
            tick = time.time()
            action = FileAction('Upload', file_path, filename)
            try:
                # Init resumable upload getting a batch generated by the
                # server. This batch is to be used as a resumable session
                batch = self.uploads.batch()

                blob = FileBlob(file_path)
                if filename:
                    blob.name = filename
                if mime_type:
                    blob.mimetype = mime_type
                upload_result = batch.upload(blob)

                upload_duration = int(time.time() - tick)
                action.transfer_duration = upload_duration
                # Use upload duration * 2 as Nuxeo transaction timeout
                tx_timeout = max(TX_TIMEOUT, upload_duration * 2)
                log.trace(
                    'Using %d seconds [max(%d, 2 * upload time=%d)] as '
                    'Nuxeo transaction timeout for batch execution of %r '
                    'with file %r', tx_timeout, TX_TIMEOUT, upload_duration,
                    command, file_path)

                if upload_duration > 0:
                    size = os.stat(file_path).st_size
                    log.trace('Speed for %d bytes is %d sec: %f bytes/sec',
                              size, upload_duration, size / upload_duration)

                if command:
                    headers = {'Nuxeo-Transaction-Timeout': str(tx_timeout)}
                    return self.operations.execute(
                        command=command,
                        input_obj=upload_result,
                        headers=headers,
                        **params)
            finally:
                FileAction.finish_action()

    def get_fs_info(self, fs_item_id, parent_fs_item_id=None,
                    raise_if_missing=True):
        # type: (Text, Optional[Text], bool) -> (Optional[RemoteFileInfo])
        fs_item = self.get_fs_item(fs_item_id,
                                   parent_fs_item_id=parent_fs_item_id)
        if fs_item is None:
            if raise_if_missing:
                raise NotFound('Could not find %r on %r' % (
                    fs_item_id, self.client.host))
            return None
        return self.file_to_info(fs_item)

    def get_filesystem_root_info(self):
        # type: () -> RemoteFileInfo
        toplevel_folder = self.operations.execute(
            command='NuxeoDrive.GetTopLevelFolder')
        return self.file_to_info(toplevel_folder)

    def get_content(self, fs_item_id, **kwargs):
        # type: (Text, Any) -> Text
        """Download and return the binary content of a file system item

        Beware that the content is loaded in memory.

        Raises NotFound if file system item with id fs_item_id
        cannot be found
        """
        fs_item_info = self.get_fs_info(fs_item_id)
        download_url = self.client.host + fs_item_info.download_url
        FileAction('Download', None, fs_item_info.name, 0)
        content = self.download(download_url, digest=fs_item_info.digest,
                                **kwargs)
        FileAction.finish_action()
        return content

    def stream_content(
            self,
            fs_item_id,  # type: Text
            file_path,  # type: Text
            parent_fs_item_id=None,  # type: Optional[Text]
            fs_item_info=None,  # type: Optional[Text]
            file_out=None,  # type: Optional[Text]
            **kwargs  # type: Any
    ):
        # type: (...) -> Text
        """Stream the binary content of a file system item to a tmp file

        Raises NotFound if file system item with id fs_item_id
        cannot be found
        """
        fs_item_info = fs_item_info or self.get_fs_info(
            fs_item_id, parent_fs_item_id=parent_fs_item_id)
        download_url = self.client.host + fs_item_info.download_url
        file_name = os.path.basename(file_path)
        if file_out is None:
            file_dir = os.path.dirname(file_path)
            file_out = os.path.join(file_dir, ''.join(
                [DOWNLOAD_TMP_FILE_PREFIX, file_name,
                 str(current_thread().ident), DOWNLOAD_TMP_FILE_SUFFIX]))

        FileAction('Download', file_out, file_name, 0)
        try:
            tmp_file = self.download(
                download_url, file_out=file_out, digest=fs_item_info.digest,
                **kwargs)
        except Exception as e:
            if os.path.exists(file_out):
                os.remove(file_out)
            raise e
        finally:
            FileAction.finish_action()
        return tmp_file

    def update_content(self, fs_item_id, content, filename=None,
                       mime_type=None):
        # type: (Text, Text, Optional[Text], Optional[Text]) -> RemoteFileInfo
        """Update a document with the given content

        Creates a temporary file from the content then streams it.
        """
        file_path = make_tmp_file(self.upload_tmp_dir, content)
        try:
            if filename is None:
                filename = self.get_fs_info(fs_item_id).name
            fs_item = self.upload(
                file_path, filename=filename, mime_type=mime_type,
                command='NuxeoDrive.UpdateFile', id=fs_item_id)
            return self.file_to_info(fs_item)
        finally:
            os.remove(file_path)

    def fs_exists(self, fs_item_id):
        # type: (Text) -> bool
        return self.operations.execute(
            command='NuxeoDrive.FileSystemItemExists', id=fs_item_id)

    def get_fs_children(self, fs_item_id):
        # type: (Text) -> List[RemoteFileInfo]
        children = self.operations.execute(
           command='NuxeoDrive.GetChildren', id=fs_item_id)
        return [self.file_to_info(fs_item) for fs_item in children]

    def scroll_descendants(self, fs_item_id, scroll_id, batch_size=100):
        # type: (Text, Text, int) -> Dict[Text, Any]
        res = self.operations.execute(
            command='NuxeoDrive.ScrollDescendants', id=fs_item_id,
            scrollId=scroll_id, batchSize=batch_size)
        return {
            'scroll_id': res['scrollId'],
            'descendants': [self.file_to_info(fs_item)
                            for fs_item in res['fileSystemItems']]
        }

    def is_filtered(self, path):
        # type: (Text) -> bool
        return False

    def make_folder(self, parent_id, name, overwrite=False):
        # type: (Text, Text, bool) -> RemoteFileInfo
        fs_item = self.operations.execute(
            command='NuxeoDrive.CreateFolder', parentId=parent_id, name=name,
            overwrite=overwrite)
        return self.file_to_info(fs_item)

    def make_file(self, parent_id, name, content):
        # type: (Text, Text, Text) -> RemoteFileInfo
        """Create a document with the given name and content

        Creates a temporary file from the content then streams it.
        """
        file_path = make_tmp_file(self.upload_tmp_dir, content)
        try:
            fs_item = self.upload(
                file_path, filename=name, command='NuxeoDrive.CreateFile',
                parentId=parent_id)
            return self.file_to_info(fs_item)
        finally:
            os.remove(file_path)

    def stream_file(
            self,
            parent_id,  # type: Text
            file_path,  # type: Text
            filename=None,  # type: Optional[Text]
            mime_type=None,  # type: Optional[Text]
            overwrite=False  # type: bool
    ):
        # type: (...) -> RemoteFileInfo
        """Create a document by streaming the file with the given path

        :param overwrite: Allows to overwrite an existing document with the
        same title on the server.
        """
        fs_item = self.upload(
            file_path, filename=filename, mime_type=mime_type,
            command='NuxeoDrive.CreateFile', parentId=parent_id,
            overwrite=overwrite)
        return self.file_to_info(fs_item)

    def stream_update(
            self,
            fs_item_id,  # type: Text
            file_path,  # type: Text
            parent_fs_item_id=None,  # type: Optional[Text]
            filename=None,  # type: Optional[Text]
            mime_type=None,  # type: Optional[Text]
            fs=True,  # type: bool
            apply_versioning_policy=False  # type: bool
    ):
        # type: (...) -> RemoteFileInfo
        """Update a document by streaming the file with the given path"""
        if fs:
            fs_item = self.upload(
                file_path,
                filename=filename,
                command='NuxeoDrive.UpdateFile',
                id=fs_item_id,
                parentId=parent_fs_item_id)
            return self.file_to_info(fs_item)

        self.upload(
            file_path,
            filename=filename,
            mime_type=mime_type,
            command='NuxeoDrive.AttachBlob',
            document=self._check_ref(fs_item_id),
            applyVersioningPolicy=apply_versioning_policy)

    def delete(self, fs_item_id, parent_fs_item_id=None):
        # type: (Text, Optional[Text]) -> None
        self.operations.execute(
            command='NuxeoDrive.Delete', id=fs_item_id,
            parentId=parent_fs_item_id)

    def undelete(self, uid):
        # type: (Text) -> Text
        input_obj = 'doc:' + uid
        if not self._has_new_trash_service:
            return self.operations.execute(
                command='Document.SetLifeCycle',
                input_obj=input_obj, value='undelete')
        else:
            return self.documents.untrash(uid)

    def rename(self, fs_item_id, new_name):
        # type: (Text, Text) -> RemoteFileInfo
        return self.file_to_info(self.operations.execute(
            command='NuxeoDrive.Rename', id=fs_item_id, name=new_name))

    def move(self, fs_item_id, new_parent_id):
        # type: (Text, Text) -> RemoteFileInfo
        return self.file_to_info(self.operations.execute(
            command='NuxeoDrive.Move', srcId=fs_item_id, destId=new_parent_id))

    @staticmethod
    def file_to_info(fs_item):
        # type: (Dict[Text, Any]) -> RemoteFileInfo
        """Convert Automation file system item description to RemoteFileInfo"""
        folderish = fs_item['folder']
        last_update = datetime.fromtimestamp(
            fs_item['lastModificationDate'] // 1000)
        creation = datetime.fromtimestamp(fs_item['creationDate'] // 1000)
        last_contributor = fs_item.get('lastContributor')

        if folderish:
            digest = None
            digest_algorithm = None
            download_url = None
            can_update = False
            can_create_child = fs_item['canCreateChild']
            # Scroll API availability
            can_scroll = fs_item.get('canScrollDescendants', False)
            can_scroll_descendants = can_scroll
        else:
            digest = fs_item['digest']
            digest_algorithm = fs_item['digestAlgorithm'] or None
            if digest_algorithm:
                digest_algorithm = digest_algorithm.lower().replace('-', '')
            download_url = fs_item['downloadURL']
            can_update = fs_item['canUpdate']
            can_create_child = False
            can_scroll_descendants = False

        # Lock info
        lock_info = fs_item.get('lockInfo')
        lock_owner = lock_created = None
        if lock_info:
            lock_owner = lock_info.get('owner')
            lock_created_millis = lock_info.get('created')
            if lock_created_millis:
                lock_created = datetime.fromtimestamp(
                    lock_created_millis // 1000)

        # Normalize using NFC to make the tests more intuitive
        name = fs_item['name']
        if name:
            name = unicodedata.normalize('NFC', name)
        return RemoteFileInfo(name, fs_item['id'], fs_item['parentId'],
                              fs_item['path'], folderish, last_update,
                              creation, last_contributor, digest,
                              digest_algorithm, download_url,
                              fs_item['canRename'], fs_item['canDelete'],
                              can_update, can_create_child, lock_owner,
                              lock_created, can_scroll_descendants)

    def get_fs_item(self, fs_item_id, parent_fs_item_id=None):
        # type: (Text, Optional[Text]) -> Optional[Dict[Text, Any]]
        if fs_item_id is None:
            log.warning('get_fs_item() called without fs_item_id')
            return None
        return self.operations.execute(
            command='NuxeoDrive.GetFileSystemItem', id=fs_item_id,
            parentId=parent_fs_item_id)

    def get_top_level_children(self):
        # type: () -> Dict[Text, Any]
        return self.operations.execute(
            command='NuxeoDrive.GetTopLevelChildren')

    def get_changes(self, last_root_definitions,
                    log_id=0, last_sync_date=0):
        # type: (Text, Optional[int], Optional[int]) -> Dict[Text, Any]
        if log_id:
            # If available, use last event log id as 'lowerBound' parameter
            # according to the new implementation of the audit change finder,
            # see https://jira.nuxeo.com/browse/NXP-14826.
            return self.operations.execute(
                command='NuxeoDrive.GetChangeSummary',
                lowerBound=log_id,
                lastSyncActiveRootDefinitions=last_root_definitions)

        # Use last sync date as 'lastSyncDate' parameter according to the
        # old implementation of the audit change finder.
        return self.operations.execute(
            command='NuxeoDrive.GetChangeSummary',
            lastSyncDate=last_sync_date,
            lastSyncActiveRootDefinitions=last_root_definitions)

    # From DocumentClient
    def fetch(self, ref, **kwargs):
        # type: (Text, Any) -> Dict[Text, Any]
        try:
            return self.operations.execute(
                command='Document.Fetch', value=get_text(ref), **kwargs)
        except HTTPError as e:
            if e.status == 404:
                raise NotFound('Failed to fetch document %r on server %r' % (
                    ref, self.client.host))
            raise e

    def _check_ref(self, ref):
        # type: (Text) -> Text
        if ref.startswith('/') and self._base_folder_path is not None:
            # This is a path ref (else an id ref)
            if self._base_folder_path.endswith('/'):
                ref = self._base_folder_path + ref[1:]
            else:
                ref = self._base_folder_path + ref
        return ref

    def doc_to_info(self, doc, fetch_parent_uid=True, parent_uid=None):
        # type: (Dict[Text, Any], bool, Optional[Text]) -> NuxeoDocumentInfo
        """Convert Automation document description to NuxeoDocumentInfo"""
        props = doc['properties']
        name = props['dc:title']
        filename = None
        folderish = 'Folderish' in doc['facets']
        try:
            last_update = datetime.strptime(
                doc['lastModified'], '%Y-%m-%dT%H:%M:%S.%fZ')
        except ValueError:
            # no millisecond?
            last_update = datetime.strptime(
                doc['lastModified'], '%Y-%m-%dT%H:%M:%SZ')

        # TODO: support other main files
        has_blob = False
        if folderish:
            digest_algorithm = None
            digest = None
        else:
            blob = props.get('file:content')
            if blob is None:
                note = props.get('note:note')
                if note is None:
                    digest_algorithm = None
                    digest = None
                else:
                    import hashlib
                    m = hashlib.md5()
                    m.update(note.encode('utf-8'))
                    digest = m.hexdigest()
                    digest_algorithm = 'md5'
                    ext = '.txt'
                    mime_type = props.get('note:mime_type')
                    if mime_type == 'text/html':
                        ext = '.html'
                    elif mime_type == 'text/xml':
                        ext = '.xml'
                    elif mime_type == 'text/x-web-markdown':
                        ext = '.md'
                    if not name.endswith(ext):
                        filename = name + ext
                    else:
                        filename = name
            else:
                has_blob = True
                digest_algorithm = blob.get('digestAlgorithm')
                if digest_algorithm is not None:
                    digest_algorithm = digest_algorithm.lower().replace('-',
                                                                        '')
                digest = blob.get('digest')
                filename = blob.get('name')

        # Lock info
        lock_owner = doc.get('lockOwner')
        lock_created = doc.get('lockCreated')
        if lock_created is not None:
            lock_created = parser.parse(lock_created)

        # Permissions
        permissions = doc.get('contextParameters', {}).get('permissions', None)

        # Trashed
        is_trashed = doc.get('isTrashed', doc['state'] == 'deleted')

        # XXX: we need another roundtrip just to fetch the parent uid...
        if parent_uid is None and fetch_parent_uid:
            parent_uid = self.fetch(os.path.dirname(doc['path']))['uid']

        # Normalize using NFC to make the tests more intuitive
        if 'uid:major_version' in props and 'uid:minor_version' in props:
            version = (str(props['uid:major_version'])
                       + '.'
                       + str(props['uid:minor_version']))
        else:
            version = None
        if name is not None:
            name = unicodedata.normalize('NFC', name)
        return NuxeoDocumentInfo(
            root=self._base_folder_ref,
            name=name, uid=doc['uid'],
            parent_uid=parent_uid,
            path=doc['path'],
            folderish=folderish,
            last_modification_time=last_update,
            last_contributor=props['dc:lastContributor'],
            digest_algorithm=digest_algorithm,
            digest=digest,
            repository=self.client.repository,
            doc_type=doc['type'],
            version=version,
            state=doc['state'],
            is_trashed=is_trashed,
            has_blob=has_blob,
            filename=filename,
            lock_owner=lock_owner,
            lock_created=lock_created,
            permissions=permissions)

    def _filtered_results(self, entries, fetch_parent_uid=True,
                          parent_uid=None):
        # type: (List[Dict], bool, Optional[Text]) -> List[NuxeoDocumentInfo]
        # Filter out filenames that would be ignored by the file system client
        # so as to be consistent.
        filtered = []
        for info in [self.doc_to_info(d, fetch_parent_uid=fetch_parent_uid,
                                      parent_uid=parent_uid)
                     for d in entries]:

            name = info.name.lower()
            if (name.endswith(Options.ignored_suffixes)
                    or name.startswith(Options.ignored_prefixes)):
                continue

            filtered.append(info)

        return filtered

    def query(self, query):
        # type: (Text) -> Dict[Text, Any]
        return self.operations.execute(command='Document.Query', query=query)

    def exists(self, ref, use_trash=True, include_versions=False):
        # type: (unicode, bool, bool) -> bool
        """
        Check if a document exists on the server.

        :param ref: Document reference (UID).
        :param use_trash: Filter documents inside the trash.
        :param include_versions:
        :rtype: bool
        """
        ref = self._check_ref(ref)
        id_prop = 'ecm:path' if ref.startswith('/') else 'ecm:uuid'

        trash = self._get_trash_condition() if use_trash else ""
        version = "" if include_versions else "AND ecm:isVersion = 0"

        query = ("SELECT * FROM Document WHERE %s = '%s' %s %s"
                 " LIMIT 1") % (
            id_prop, ref, trash, version)
        results = self.query(query)
        return len(results[u'entries']) == 1

    def get_info(self, ref, raise_if_missing=True, fetch_parent_uid=True,
                 use_trash=True, include_versions=False):
        # type: (Text, bool, bool, bool, bool) -> Optional[NuxeoDocumentInfo]
        if not self.exists(ref, use_trash=use_trash,
                           include_versions=include_versions):
            if raise_if_missing:
                raise NotFound("Could not find '%s' on '%s'" % (
                    self._check_ref(ref), self.client.host))
            return None
        return self.doc_to_info(self.fetch(self._check_ref(ref)),
                                fetch_parent_uid=fetch_parent_uid)

    def get_children_info(self, ref, types=DEFAULT_TYPES, limit=MAX_CHILDREN):
        # type: (Text, Tuple[Text], int) -> List[NuxeoDocumentInfo]
        ref = self._check_ref(ref)

        query = (
            "SELECT * FROM Document"
            "       WHERE ecm:parentId = '%s'"
            "       AND ecm:primaryType IN ('%s')"
            "       %s"
            "       AND ecm:isVersion = 0"
            "       ORDER BY dc:title, dc:created LIMIT %d"
        ) % (ref,
             "', '".join(types),
             self._get_trash_condition(),
             limit)

        entries = self.query(query)['entries']
        if len(entries) == MAX_CHILDREN:
            # TODO: how to best handle this case? A warning and return an empty
            # list, a dedicated exception?
            raise RuntimeError('Folder %r on server %r has more than the'
                               'maximum number of children: %d' % (
                ref, self.client.host, MAX_CHILDREN))

        return self._filtered_results(entries)

    def get_children(self, ref):
        # type: (Text) -> Dict[Text, Any]
        return self.operations.execute(
            command='Document.GetChildren', input_obj='doc:' + ref)

    def get_blob(self, ref, file_out=None, **kwargs):
        # type: (Text, Optional[Text], Any) -> Text
        if isinstance(ref, NuxeoDocumentInfo):
            doc_id = ref.uid
            if not ref.has_blob and ref.doc_type == 'Note':
                doc = self.fetch(doc_id)
                content = doc['properties'].get('note:note')
                if file_out is not None and content is not None:
                    with open(file_out, 'wb') as f:
                        f.write(content.encode('utf-8'))
                return content
        else:
            doc_id = ref

        return self.operations.execute(
            command='Blob.Get',
            input_obj='doc:' + doc_id,
            json=False,
            file_out=file_out,
            **kwargs)

    def lock(self, ref):
        # type: (Text) -> Dict[Text, Any]
        return self.operations.execute(
            command='Document.Lock', input_obj='doc:' + self._check_ref(ref))

    def unlock(self, ref):
        # type: (Text) -> Dict[Text, Any]
        return self.operations.execute(
            command='Document.Unlock', input_obj='doc:' + self._check_ref(ref))

    def get_roots(self):
        # type: () -> List[NuxeoDocumentInfo]
        res = self.operations.execute(command='NuxeoDrive.GetRoots')
        return self._filtered_results(res['entries'], fetch_parent_uid=False)

    def register_as_root(self, ref):
        # type: (Text) -> bool
        self.operations.execute(
            command='NuxeoDrive.SetSynchronization',
            input_obj='doc:' + self._check_ref(ref), enable=True)
        return True

    def unregister_as_root(self, ref):
        # type: (Text) -> bool
        self.operations.execute(
            command='NuxeoDrive.SetSynchronization',
            input_obj='doc:' + self._check_ref(ref), enable=False)
        return True

    def conflicted_name(self, original_name):
        # type: (Text) -> Text
        """Generate a new name suitable for conflict deduplication."""
        return self.operations.execute(
            command='NuxeoDrive.GenerateConflictedItemName',
            name=original_name)

    def set_proxy(self, proxy):
        # type: (Type[Proxy]) -> None
        if proxy:
            settings = proxy.settings(url=self.client.host)
            self.client.client_kwargs['proxies'] = settings

    def _get_trash_condition(self):
        # type: () -> Text
        if not self._has_new_trash_service:
            return "AND ecm:currentLifeCycleState != 'deleted'"
        else:
            return "AND ecm:isTrashed = 0"


class FilteredRemote(Remote):

    def is_filtered(self, path):
        # type: (Text) -> bool
        return self._dao.is_filter(path)

    def get_fs_children(self, fs_item_id):
        # type: (Text) -> List[RemoteFileInfo]
        result = super(FilteredRemote, self).get_fs_children(fs_item_id)
        # Need to filter the children result
        filtered = []
        for item in result:
            if not self.is_filtered(item.path):
                filtered.append(item)
            else:
                log.debug('Filtering item %r', item)
        return filtered