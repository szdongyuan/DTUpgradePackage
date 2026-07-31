import hashlib
import ctypes
import ntpath
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from typing import NamedTuple

from base.json_merge import merge_json_files_with_reason

from consts.program_config import BACKUP_DIR, DEFAULT_DIR, TARGET_VERSION, ALL_VERSIONS, BACKUP_UI_PIC_PATH, CONFIG_DIR
from consts.program_config import AI_MODEL_CONFIG_PATH, SCANNER_BARCODE_CONFIG_PATH, UI_CONFIG_PATH, UI_PIC_PATH
from consts.program_config import BACKUP_AI_MODEL_CONFIG_PATH, BACKUP_SCANNER_BARCODE_CONFIG_PATH, BACKUP_UI_CONFIG_PATH
from consts.program_config import DEFAULT_TARGET_DIR

from load_yml.load_yml import Readyml


_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_READ_CONTROL = 0x00020000
_FILE_READ_DATA = 0x00000001
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_READ_EA = 0x00000008
_FILE_READ_ATTRIBUTES = 0x00000080
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

_FILE_ID_INFO_CLASS = 18
_FILE_BASIC_INFO_CLASS = 0
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_EXTD_DIRECTORY_INFO_CLASS = 19
_FILE_ID_EXTD_DIRECTORY_RESTART_INFO_CLASS = 20
_FILE_RENAME_INFO_CLASS = 3
_FILE_DISPOSITION_INFO_EX_CLASS = 21

_FILE_OPEN = 1
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_OBJ_CASE_INSENSITIVE = 0x00000040

_FILE_DISPOSITION_DELETE = 0x00000001
_FILE_DISPOSITION_POSIX_SEMANTICS = 0x00000002
_FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x00000010
_WINDOWS_DISPOSITION_REQUIRED_FLAGS = (
    _FILE_DISPOSITION_DELETE
    | _FILE_DISPOSITION_POSIX_SEMANTICS
    | _FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
)

_STATUS_SUCCESS = 0x00000000
_STATUS_NO_SUCH_FILE = 0xC000000F
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_STATUS_NOT_SUPPORTED = 0xC00000BB
_STATUS_INVALID_DEVICE_REQUEST = 0xC0000010

_ERROR_INVALID_FUNCTION = 1
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_NO_MORE_FILES = 18
_ERROR_NOT_SUPPORTED = 50
_ERROR_FILE_EXISTS = 80
_ERROR_INVALID_PARAMETER = 87
_ERROR_ALREADY_EXISTS = 183

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FILE_ID_128(ctypes.Structure):
    _fields_ = (("Identifier", ctypes.c_ubyte * 16),)


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = (
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", _FILE_ID_128),
    )


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = (
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    )


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = (
        ("AllocationSize", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("NumberOfLinks", ctypes.c_uint32),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    )


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = (
        ("FileAttributes", ctypes.c_uint32),
        ("ReparseTag", ctypes.c_uint32),
    )


class _FILE_RENAME_INFO_HEADER(ctypes.Structure):
    _fields_ = (
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    )


class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = (("Flags", ctypes.c_uint32),)


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = (
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    )


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = (
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    )


class _IO_STATUS_BLOCK_UNION(ctypes.Union):
    _fields_ = (
        ("Status", ctypes.c_long),
        ("Pointer", ctypes.c_void_p),
    )


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("Result",)
    _fields_ = (
        ("Result", _IO_STATUS_BLOCK_UNION),
        ("Information", ctypes.c_size_t),
    )


class _WindowsDirectoryEntry(NamedTuple):
    name: str
    file_id_128: bytes
    file_attributes: int
    reparse_tag: int
    size: int
    last_write_time: int


class WindowsDeleteFailure(Exception):
    def __init__(self, category, *_sensitive_details):
        self.category = str(category)
        super().__init__(self.category)


class WindowsDeleteUnsupported(WindowsDeleteFailure):
    pass


class WindowsHandleOwner:
    def __init__(self, close):
        self._close = close
        self._handles = []

    def acquire(self, handle):
        self._handles.append(handle)
        return handle

    def release(self, handle):
        for index in range(len(self._handles) - 1, -1, -1):
            if self._handles[index] == handle:
                return self._handles.pop(index)
        return None

    def close_all(self):
        first_error = None
        while self._handles:
            handle = self._handles.pop()
            try:
                self._close(handle)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def __enter__(self):
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        try:
            self.close_all()
        except BaseException:
            if exception_type is None:
                raise
        return False


class WindowsHandleOps:
    _UNSUPPORTED_WINERRORS = frozenset(
        (
            _ERROR_INVALID_FUNCTION,
            _ERROR_NOT_SUPPORTED,
            _ERROR_INVALID_PARAMETER,
        )
    )
    _UNSUPPORTED_NTSTATUSES = frozenset(
        (
            _STATUS_NOT_SUPPORTED,
            _STATUS_INVALID_DEVICE_REQUEST,
        )
    )
    _ABSENT_NTSTATUSES = frozenset(
        (
            _STATUS_NO_SUCH_FILE,
            _STATUS_OBJECT_NAME_NOT_FOUND,
        )
    )

    def __init__(self, platform_name=None, event_sink=None):
        self._platform_name = os.name if platform_name is None else platform_name
        self._event_sink = event_sink
        self._bindings_loaded = False
        self._bindings_available = None
        self.unsupported_category = None
        self.last_open_protocol = None
        self.last_rename = None
        self.last_disposition_flags = None

    @staticmethod
    def _handle_value(handle):
        if isinstance(handle, int):
            return handle
        value = getattr(handle, "value", handle)
        return value

    @staticmethod
    def _unsigned_status(status):
        return int(status) & 0xFFFFFFFF

    def _emit(self, event):
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except BaseException:
                pass

    def _load_bindings(self):
        if self._bindings_loaded:
            return bool(self._bindings_available)
        self._bindings_loaded = True
        try:
            if self._platform_name != "nt":
                self._bindings_available = False
                return False

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

            self._create_file = kernel32.CreateFileW
            self._create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            self._create_file.restype = wintypes.HANDLE

            self._close_handle = kernel32.CloseHandle
            self._close_handle.argtypes = (wintypes.HANDLE,)
            self._close_handle.restype = wintypes.BOOL

            self._get_file_information = (
                kernel32.GetFileInformationByHandleEx
            )
            self._get_file_information.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            self._get_file_information.restype = wintypes.BOOL

            self._set_file_information_native = (
                kernel32.SetFileInformationByHandle
            )
            self._set_file_information_native.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            self._set_file_information_native.restype = wintypes.BOOL

            self._set_file_pointer = kernel32.SetFilePointerEx
            self._set_file_pointer.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int64,
                ctypes.POINTER(ctypes.c_int64),
                wintypes.DWORD,
            )
            self._set_file_pointer.restype = wintypes.BOOL

            self._read_file = kernel32.ReadFile
            self._read_file.argtypes = (
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            )
            self._read_file.restype = wintypes.BOOL

            self._nt_create_file = ntdll.NtCreateFile
            self._nt_create_file.argtypes = (
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_uint32,
                ctypes.POINTER(_OBJECT_ATTRIBUTES),
                ctypes.POINTER(_IO_STATUS_BLOCK),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
            )
            self._nt_create_file.restype = ctypes.c_long

            self._nt_set_file_information = ntdll.NtSetInformationFile
            self._nt_set_file_information.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_IO_STATUS_BLOCK),
                wintypes.LPVOID,
                ctypes.c_uint32,
                ctypes.c_uint32,
            )
            self._nt_set_file_information.restype = ctypes.c_long
        except BaseException:
            self._bindings_available = False
            return False

        self._bindings_available = True
        return True

    def is_supported(self):
        self.unsupported_category = None
        if self._platform_name != "nt":
            self.unsupported_category = "unsupported_platform"
            return False
        try:
            if sys.getwindowsversion().build < 16299:
                self.unsupported_category = "unsupported_windows_version"
                return False
        except (AttributeError, OSError):
            self.unsupported_category = "unsupported_platform"
            return False
        if not self._load_bindings():
            self.unsupported_category = "unsupported_api"
            return False
        return True

    def _require_supported(self):
        if not self.is_supported():
            raise WindowsDeleteUnsupported(
                self.unsupported_category or "unsupported_api"
            )

    @staticmethod
    def _desired_access(*, directory, delete):
        access = _FILE_READ_ATTRIBUTES
        if directory:
            access |= _FILE_LIST_DIRECTORY
        else:
            access |= _FILE_READ_DATA
        if delete:
            access |= _DELETE
        return access

    @staticmethod
    def _open_flags(*, directory):
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        return flags

    def open_absolute(self, path, *, directory, delete=False):
        self._require_supported()
        desired_access = self._desired_access(
            directory=directory,
            delete=delete,
        )
        share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        flags = self._open_flags(directory=directory)
        self.last_open_protocol = {
            "relative": False,
            "desired_access": desired_access,
            "share_mode": share_mode,
            "flags": flags,
        }
        handle = self._create_file(
            os.fspath(path),
            desired_access,
            share_mode,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        handle = self._handle_value(handle)
        if handle in (None, _INVALID_HANDLE_VALUE):
            error_number = ctypes.get_last_error()
            if error_number in self._UNSUPPORTED_WINERRORS:
                raise WindowsDeleteUnsupported("unsupported_filesystem")
            raise WindowsDeleteFailure("absolute_open_failed")
        self._emit(("opened_absolute", handle, bool(directory)))
        return handle

    def _nt_open_relative(
        self,
        parent_handle,
        name,
        *,
        directory,
        delete,
        desired_access=None,
        synchronous=False,
    ):
        if desired_access is None:
            desired_access = self._desired_access(
                directory=directory,
                delete=delete,
            )
        name_buffer = ctypes.create_unicode_buffer(str(name))
        name_length = len(str(name).encode("utf-16-le"))
        unicode_name = _UNICODE_STRING(
            name_length,
            name_length + 2,
            ctypes.cast(name_buffer, ctypes.c_void_p),
        )
        object_attributes = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES),
            self._handle_value(parent_handle),
            ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        io_status = _IO_STATUS_BLOCK()
        opened_handle = ctypes.c_void_p()
        create_options = _FILE_OPEN_REPARSE_POINT
        if synchronous:
            desired_access |= _SYNCHRONIZE
            create_options |= _FILE_SYNCHRONOUS_IO_NONALERT
        if directory is True:
            create_options |= _FILE_DIRECTORY_FILE
        elif directory is False:
            create_options |= _FILE_NON_DIRECTORY_FILE
        status = self._nt_create_file(
            ctypes.byref(opened_handle),
            desired_access,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            _FILE_ATTRIBUTE_NORMAL,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            _FILE_OPEN,
            create_options,
            None,
            0,
        )
        unsigned_status = self._unsigned_status(status)
        if unsigned_status == _STATUS_SUCCESS:
            return opened_handle.value, unsigned_status
        return None, unsigned_status

    def open_relative(
        self,
        parent_handle,
        name,
        *,
        directory,
        delete=False,
    ):
        self._require_supported()
        desired_access = self._desired_access(
            directory=directory,
            delete=delete,
        ) | _SYNCHRONIZE
        share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        flags = self._open_flags(directory=directory)
        self.last_open_protocol = {
            "relative": True,
            "desired_access": desired_access,
            "share_mode": share_mode,
            "flags": flags,
        }
        handle, status = self._nt_open_relative(
            parent_handle,
            name,
            directory=directory,
            delete=delete,
            desired_access=desired_access,
            synchronous=True,
        )
        if handle is None:
            if status in self._UNSUPPORTED_NTSTATUSES:
                raise WindowsDeleteUnsupported("unsupported_filesystem")
            raise WindowsDeleteFailure("relative_open_failed")
        self._emit(("opened_relative", handle, str(name)))
        return handle

    def open_relative_attributes(self, parent_handle, name):
        self._require_supported()
        handle, status = self._nt_open_relative(
            parent_handle,
            name,
            directory=None,
            delete=False,
            desired_access=_FILE_READ_ATTRIBUTES,
        )
        if handle is None:
            if status in self._UNSUPPORTED_NTSTATUSES:
                raise WindowsDeleteUnsupported("unsupported_filesystem")
            raise WindowsDeleteFailure("relative_open_failed")
        self._emit(("opened_relative", handle, str(name)))
        return handle

    def _query_information(self, handle, information_class, structure):
        information = structure()
        if not self._get_file_information(
            handle,
            information_class,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error_number = ctypes.get_last_error()
            if error_number in self._UNSUPPORTED_WINERRORS:
                raise WindowsDeleteUnsupported("unsupported_filesystem")
            raise WindowsDeleteFailure("identity_query_failed")
        return information

    def identity(self, handle):
        self._require_supported()
        file_id_info = self._query_information(
            handle,
            _FILE_ID_INFO_CLASS,
            _FILE_ID_INFO,
        )
        tag_info = self._query_information(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO_CLASS,
            _FILE_ATTRIBUTE_TAG_INFO,
        )
        return (
            int(file_id_info.VolumeSerialNumber),
            bytes(file_id_info.FileId.Identifier),
            int(tag_info.FileAttributes),
            int(tag_info.ReparseTag),
        )

    def _query_stable_information(self, handle):
        basic_info = self._query_information(
            handle,
            _FILE_BASIC_INFO_CLASS,
            _FILE_BASIC_INFO,
        )
        standard_info = self._query_information(
            handle,
            _FILE_STANDARD_INFO_CLASS,
            _FILE_STANDARD_INFO,
        )
        return (
            int(basic_info.FileAttributes),
            int(basic_info.LastWriteTime),
            int(standard_info.EndOfFile),
            bool(standard_info.Directory),
        )

    def enumerate(self, directory_handle):
        self._require_supported()
        entries = []
        information_class = _FILE_ID_EXTD_DIRECTORY_RESTART_INFO_CLASS
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            if not self._get_file_information(
                directory_handle,
                information_class,
                buffer,
                ctypes.sizeof(buffer),
            ):
                error_number = ctypes.get_last_error()
                if error_number == _ERROR_NO_MORE_FILES:
                    break
                if error_number in self._UNSUPPORTED_WINERRORS:
                    raise WindowsDeleteUnsupported("unsupported_filesystem")
                raise WindowsDeleteFailure("directory_enumeration_failed")
            information_class = _FILE_ID_EXTD_DIRECTORY_INFO_CLASS
            offset = 0
            while True:
                next_offset = ctypes.c_uint32.from_buffer(
                    buffer,
                    offset,
                ).value
                last_write_time = ctypes.c_int64.from_buffer(
                    buffer,
                    offset + 24,
                ).value
                size = ctypes.c_int64.from_buffer(
                    buffer,
                    offset + 40,
                ).value
                file_attributes = ctypes.c_uint32.from_buffer(
                    buffer,
                    offset + 56,
                ).value
                name_length = ctypes.c_uint32.from_buffer(
                    buffer,
                    offset + 60,
                ).value
                reparse_tag = ctypes.c_uint32.from_buffer(
                    buffer,
                    offset + 68,
                ).value
                file_id = bytes(buffer[offset + 72 : offset + 88])
                name = bytes(
                    buffer[offset + 88 : offset + 88 + name_length]
                ).decode("utf-16-le")
                if name not in (".", ".."):
                    entries.append(
                        _WindowsDirectoryEntry(
                            name,
                            file_id,
                            int(file_attributes),
                            int(reparse_tag),
                            int(size),
                            int(last_write_time),
                        )
                    )
                if next_offset == 0:
                    break
                offset += next_offset
        entries.sort(key=lambda entry: entry.name.casefold())
        return tuple(entries)

    def read_digest(self, file_handle):
        self._require_supported()
        if not self._set_file_pointer(file_handle, 0, None, 0):
            error_number = ctypes.get_last_error()
            if error_number in self._UNSUPPORTED_WINERRORS:
                raise WindowsDeleteUnsupported("unsupported_filesystem")
            raise WindowsDeleteFailure("file_read_failed")
        digest = hashlib.sha256()
        while True:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            bytes_read = wintypes.DWORD()
            if not self._read_file(
                file_handle,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(bytes_read),
                None,
            ):
                error_number = ctypes.get_last_error()
                if error_number in self._UNSUPPORTED_WINERRORS:
                    raise WindowsDeleteUnsupported("unsupported_filesystem")
                raise WindowsDeleteFailure("file_read_failed")
            if bytes_read.value == 0:
                break
            digest.update(buffer.raw[: bytes_read.value])
        return digest.hexdigest()

    def name_is_absent(self, parent_handle, name):
        self._require_supported()
        handle, status = self._nt_open_relative(
            parent_handle,
            name,
            directory=None,
            delete=False,
            desired_access=_FILE_READ_ATTRIBUTES,
        )
        if handle is not None:
            self.close(handle)
            return False
        if status in self._ABSENT_NTSTATUSES:
            return True
        if status == _STATUS_OBJECT_PATH_NOT_FOUND:
            raise WindowsDeleteFailure("source_parent_changed")
        if status in self._UNSUPPORTED_NTSTATUSES:
            raise WindowsDeleteUnsupported("unsupported_filesystem")
        raise WindowsDeleteFailure("source_absence_indeterminate")

    def _set_file_information(
        self,
        handle,
        information_class,
        information,
        information_size,
    ):
        if self._set_file_information_native(
            handle,
            information_class,
            information,
            information_size,
        ):
            return True, 0
        return False, ctypes.get_last_error()

    def rename_relative_no_replace(
        self,
        object_handle,
        target_parent_handle,
        target_name,
    ):
        self._require_supported()
        encoded_name = str(target_name).encode("utf-16-le")
        name_offset = _FILE_RENAME_INFO_HEADER.FileName.offset
        buffer_size = (
            ctypes.sizeof(_FILE_RENAME_INFO_HEADER) + len(encoded_name)
        )
        buffer = ctypes.create_string_buffer(buffer_size)
        header = ctypes.cast(
            buffer,
            ctypes.POINTER(_FILE_RENAME_INFO_HEADER),
        ).contents
        header.Flags = 0
        header.RootDirectory = self._handle_value(target_parent_handle)
        header.FileNameLength = len(encoded_name)
        if encoded_name:
            ctypes.memmove(
                ctypes.addressof(buffer) + name_offset,
                encoded_name,
                len(encoded_name),
            )
        self.last_rename = {
            "root_directory": self._handle_value(target_parent_handle),
            "replace": False,
            "target_name": str(target_name),
        }
        io_status = _IO_STATUS_BLOCK()
        status = self._unsigned_status(
            self._nt_set_file_information(
                object_handle,
                ctypes.byref(io_status),
                buffer,
                ctypes.sizeof(buffer),
                10,
            )
        )
        if status == _STATUS_SUCCESS:
            succeeded, error_number = True, 0
        else:
            succeeded, error_number = False, status
        if succeeded:
            self._emit(
                (
                    "rename",
                    self._handle_value(object_handle),
                    str(target_name),
                )
            )
            return
        if error_number in (
            _ERROR_FILE_EXISTS,
            _ERROR_ALREADY_EXISTS,
            _STATUS_OBJECT_NAME_COLLISION,
        ):
            raise WindowsDeleteFailure("rename_collision")
        if (
            error_number in self._UNSUPPORTED_WINERRORS
            or error_number in self._UNSUPPORTED_NTSTATUSES
        ):
            raise WindowsDeleteUnsupported("unsupported_filesystem")
        raise WindowsDeleteFailure("rename_failed")

    def set_delete_disposition(self, handle):
        self._require_supported()
        disposition = _FILE_DISPOSITION_INFO_EX(
            _WINDOWS_DISPOSITION_REQUIRED_FLAGS
        )
        self.last_disposition_flags = int(disposition.Flags)
        succeeded, error_number = self._set_file_information(
            handle,
            _FILE_DISPOSITION_INFO_EX_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )
        if succeeded:
            self._emit(("disposition", self._handle_value(handle)))
            return
        if error_number in self._UNSUPPORTED_WINERRORS:
            raise WindowsDeleteUnsupported("unsupported_filesystem")
        raise WindowsDeleteFailure("delete_disposition_failed")

    def close(self, handle):
        self._require_supported()
        handle_value = self._handle_value(handle)
        if not self._close_handle(handle_value):
            ctypes.get_last_error()
            raise WindowsDeleteFailure("handle_close_failed")
        self._emit(("close", handle_value))

    @staticmethod
    def _entry_matches_identity(entry, identity):
        attribute_mask = (
            _FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT
        )
        return (
            entry.file_id_128 == identity[1]
            and entry.file_attributes & attribute_mask
            == identity[2] & attribute_mask
            and entry.reparse_tag == identity[3]
        )

    def manifest_entry(self, handle):
        identity_before = self.identity(handle)
        (
            file_attributes,
            last_write_time,
            size,
            is_directory,
        ) = self._query_stable_information(handle)
        is_reparse = bool(
            file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        )
        digest = None
        if not is_directory and not is_reparse:
            digest = self.read_digest(handle)
        identity_after = self.identity(handle)
        stable_after = self._query_stable_information(handle)
        if identity_after != identity_before or stable_after != (
            file_attributes,
            last_write_time,
            size,
            is_directory,
        ):
            raise WindowsDeleteFailure("manifest_changed")
        return (
            identity_before[0],
            identity_before[1],
            file_attributes,
            identity_before[3],
            size,
            last_write_time,
            digest,
        )

    def capture_manifest(self, root_handle, *, directory):
        if not directory:
            return (("", self.manifest_entry(root_handle)),)

        manifest = []

        def walk(parent_handle, relative_parent):
            for entry in self.enumerate(parent_handle):
                is_directory = bool(
                    entry.file_attributes & _FILE_ATTRIBUTE_DIRECTORY
                )
                child_handle = self.open_relative(
                    parent_handle,
                    entry.name,
                    directory=is_directory,
                )
                try:
                    child_identity = self.identity(child_handle)
                    if not self._entry_matches_identity(
                        entry,
                        child_identity,
                    ):
                        raise WindowsDeleteFailure(
                            "enumerated_identity_changed"
                        )
                    relative_name = (
                        entry.name
                        if not relative_parent
                        else relative_parent + "\\" + entry.name
                    )
                    manifest.append(
                        (
                            relative_name,
                            self.manifest_entry(child_handle),
                        )
                    )
                    if (
                        is_directory
                        and not entry.file_attributes
                        & _FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        walk(child_handle, relative_name)
                finally:
                    self.close(child_handle)

        walk(root_handle, "")
        manifest.sort(key=lambda item: item[0].casefold())
        return tuple(manifest)

    @staticmethod
    def _identity_matches_manifest(identity, manifest_entry):
        return identity == (
            manifest_entry[0],
            manifest_entry[1],
            manifest_entry[2],
            manifest_entry[3],
        )

    @staticmethod
    def _directory_entry_matches_manifest(
        directory_entry,
        manifest_entry,
        *,
        stable_metadata,
    ):
        if (
            directory_entry.file_id_128 != manifest_entry[1]
            or directory_entry.file_attributes != manifest_entry[2]
            or directory_entry.reparse_tag != manifest_entry[3]
        ):
            return False
        if not stable_metadata:
            return True
        return (
            directory_entry.size == manifest_entry[4]
            and directory_entry.last_write_time == manifest_entry[5]
        )

    @staticmethod
    def _expected_manifest_children(expected_manifest):
        manifest_by_path = {}
        children_by_parent = {}
        for relative_path, manifest_entry in expected_manifest:
            if (
                not isinstance(relative_path, str)
                or not relative_path
                or len(manifest_entry) != 7
            ):
                raise WindowsDeleteFailure("invalid_expected_manifest")
            components = relative_path.split("\\")
            if any(not component for component in components):
                raise WindowsDeleteFailure("invalid_expected_manifest")
            canonical_path = relative_path.casefold()
            if canonical_path in manifest_by_path:
                raise WindowsDeleteFailure("invalid_expected_manifest")
            manifest_by_path[canonical_path] = manifest_entry
            parent_path = "\\".join(components[:-1]).casefold()
            children = children_by_parent.setdefault(parent_path, {})
            canonical_name = components[-1].casefold()
            if canonical_name in children:
                raise WindowsDeleteFailure("invalid_expected_manifest")
            children[canonical_name] = (
                components[-1],
                relative_path,
                manifest_entry,
            )

        for canonical_path in manifest_by_path:
            if "\\" not in canonical_path:
                continue
            parent_path = canonical_path.rsplit("\\", 1)[0]
            parent_manifest = manifest_by_path.get(parent_path)
            if (
                parent_manifest is None
                or not parent_manifest[2] & _FILE_ATTRIBUTE_DIRECTORY
                or parent_manifest[2] & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise WindowsDeleteFailure("invalid_expected_manifest")
        return children_by_parent

    def delete_descendants(self, root_handle, expected_manifest):
        children_by_parent = self._expected_manifest_children(
            expected_manifest
        )

        def enumerate_by_name(parent_handle):
            entries_by_name = {}
            for entry in self.enumerate(parent_handle):
                canonical_name = entry.name.casefold()
                if canonical_name in entries_by_name:
                    raise WindowsDeleteFailure("directory_changed")
                entries_by_name[canonical_name] = entry
            return entries_by_name

        def delete_children(parent_handle, parent_path):
            expected_children = children_by_parent.get(
                parent_path.casefold(),
                {},
            )
            initial_entries = enumerate_by_name(parent_handle)
            if set(initial_entries) != set(expected_children):
                raise WindowsDeleteFailure("directory_changed")

            remaining_names = set(expected_children)
            for canonical_name in sorted(expected_children):
                (
                    expected_name,
                    child_path,
                    expected_entry,
                ) = expected_children[canonical_name]
                enumerated_entry = initial_entries[canonical_name]
                if not self._directory_entry_matches_manifest(
                    enumerated_entry,
                    expected_entry,
                    stable_metadata=True,
                ):
                    raise WindowsDeleteFailure("manifest_changed")

                is_directory = bool(
                    expected_entry[2] & _FILE_ATTRIBUTE_DIRECTORY
                )
                is_reparse = bool(
                    expected_entry[2] & _FILE_ATTRIBUTE_REPARSE_POINT
                )
                is_traversed_directory = is_directory and not is_reparse
                child_handle = self.open_relative(
                    parent_handle,
                    expected_name,
                    directory=is_directory,
                    delete=True,
                )
                disposition_accepted = False
                try:
                    child_identity = self.identity(child_handle)
                    if (
                        not self._entry_matches_identity(
                            enumerated_entry,
                            child_identity,
                        )
                        or not self._identity_matches_manifest(
                            child_identity,
                            expected_entry,
                        )
                    ):
                        raise WindowsDeleteFailure("manifest_changed")

                    if self.manifest_entry(child_handle) != expected_entry:
                        raise WindowsDeleteFailure("manifest_changed")

                    if is_traversed_directory:
                        delete_children(child_handle, child_path)
                        if self.enumerate(child_handle):
                            raise WindowsDeleteFailure(
                                "directory_not_empty"
                            )

                    current_entries = enumerate_by_name(parent_handle)
                    if set(current_entries) != remaining_names:
                        raise WindowsDeleteFailure("directory_changed")
                    current_entry = current_entries.get(canonical_name)
                    if (
                        current_entry is None
                        or not self._directory_entry_matches_manifest(
                            current_entry,
                            expected_entry,
                            stable_metadata=not is_traversed_directory,
                        )
                    ):
                        raise WindowsDeleteFailure("manifest_changed")

                    if is_traversed_directory:
                        if not self._identity_matches_manifest(
                            self.identity(child_handle),
                            expected_entry,
                        ):
                            raise WindowsDeleteFailure("manifest_changed")
                    elif (
                        self.manifest_entry(child_handle)
                        != expected_entry
                    ):
                        raise WindowsDeleteFailure("manifest_changed")

                    self.set_delete_disposition(child_handle)
                    disposition_accepted = True
                finally:
                    self.close(child_handle)
                if not disposition_accepted:
                    raise WindowsDeleteFailure("cleanup_failed")
                remaining_names.remove(canonical_name)

            if self.enumerate(parent_handle):
                raise WindowsDeleteFailure("directory_changed")

        delete_children(root_handle, "")


class _WindowsDeleteCapability(NamedTuple):
    volume_serial: int
    relative_rename: bool
    disposition: bool


class _OrdinaryConfigDeleteRestoreResult(NamedTuple):
    restored: bool
    retained_path: object
    previous_artifact_path_valid: bool = True


class _OrdinaryConfigDeleteArtifactState:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.path_valid = True
        self.retain_only = False


class _OrdinaryConfigDeleteSnapshot(NamedTuple):
    source_path: str
    source_name: str
    source_chain: tuple
    destination_path: str
    destination_name: str
    destination_chain: tuple
    destination_missing: tuple
    destination_exists: bool
    destination_directory: object
    destination_identity: object
    destination_manifest: object
    managed_root_path: str
    managed_root_exists: bool
    recovery_path: str
    recovery_chain: tuple
    recovery_identity: tuple
    capability: str


class ConfigManager(object):

    def __init__(self, logger=None, current_version=None):
        self.logger = logger
        self.current_version = current_version

        self.ui_pic_flag = True
        self.ui_config_flag = True
        self.ai_model_config_flag = True
        self.scannre_barcode_config_flag = True
        self.backup_file_path = list()
        self.upgrade_version_list = list()
        self._ordinary_config_delete_retained_artifacts = set()

        self.init_upgrade_version_list()

    def _report_best_effort(
        self,
        level,
        message,
        include_print=True,
    ):
        if include_print:
            try:
                print(message)
            except BaseException:
                pass
        try:
            getattr(self.logger, level)(message)
        except BaseException:
            pass

    def is_parent_path_os(self, parent_path, child_path):
        """
        使用os.path判断parent_path是否为child_path的父路径
        """
        try:
            common = os.path.commonpath([parent_path, child_path])
            return os.path.normpath(common) == os.path.normpath(parent_path)
        except (ValueError, OSError):
            return False

    def smart_copy(self, src_path, dst_path):
        """
        智能复制方法，自动处理文件和目录

        Args:
            src_path (str): 源路径
            dst_path (str): 目标路径
        """
        try:
            if not os.path.exists(src_path):
                self._report_best_effort(
                    "error",
                    f"源路径不存在: {src_path}",
                    include_print=False,
                )
                return False

            # 处理源路径是目录的情况
            if os.path.isdir(src_path):
                # 如果目标路径末尾有路径分隔符或目标不存在，则复制整个目录
                if os.path.exists(dst_path) and os.path.isdir(dst_path):
                    # 目标是目录，将源目录内容复制到目标目录
                    for item in os.listdir(src_path):
                        src_item = os.path.join(src_path, item)
                        dst_item = os.path.join(dst_path, item)
                        if os.path.isfile(src_item):
                            shutil.copy2(src_item, dst_item)
                        else:
                            if os.path.exists(dst_item):
                                shutil.rmtree(dst_item)
                            shutil.copytree(src_item, dst_item)
                    self._report_best_effort(
                        "info",
                        f"目录内容复制成功: {src_path} -> {dst_path}",
                    )
                else:
                    # 目标是文件路径或不存在，复制整个目录
                    if os.path.exists(dst_path):
                        if os.path.isfile(dst_path):
                            os.remove(dst_path)
                        else:
                            shutil.rmtree(dst_path)
                    shutil.copytree(src_path, dst_path)
                    self._report_best_effort(
                        "info",
                        f"目录复制成功: {src_path} -> {dst_path}",
                    )

            # 处理源路径是文件的情况
            elif os.path.isfile(src_path):
                # 确保目标目录存在
                dst_dir = os.path.dirname(dst_path)
                if not os.path.exists(dst_dir):
                    os.makedirs(dst_dir)

                shutil.copy2(src_path, dst_path)
                self._report_best_effort(
                    "info",
                    f"文件复制成功: {src_path} -> {dst_path}",
                )

            return True

        except Exception as e:
            self._report_best_effort(
                "error",
                f"复制失败 {src_path} -> {dst_path}: {e}",
            )
            return False

    @staticmethod
    def _snapshot_entry_is_link_or_reparse(entry_stat):
        reparse_flag = getattr(
            stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
        file_attributes = getattr(entry_stat, "st_file_attributes", 0)
        return stat.S_ISLNK(entry_stat.st_mode) or bool(
            file_attributes & reparse_flag
        )

    def _snapshot_ancestor_chain_is_safe(self, path, trusted_anchor):
        try:
            absolute_path = os.path.abspath(path)
            absolute_anchor = os.path.abspath(trusted_anchor)
            if not self._path_is_contained(
                absolute_path,
                absolute_anchor,
            ):
                raise ValueError("path is outside its trusted anchor")

            resolved_anchor = os.path.realpath(absolute_anchor)
            anchor_stat = os.stat(absolute_anchor)
            visited_identities = {
                (anchor_stat.st_dev, anchor_stat.st_ino)
            }
            relative_path = os.path.relpath(
                absolute_path,
                absolute_anchor,
            )
        except (OSError, TypeError, ValueError) as e:
            self._report_best_effort(
                "error",
                f"快照祖先安全检查失败 {path}: {e}",
                include_print=False,
            )
            return False

        if relative_path == os.curdir:
            return True

        current_path = absolute_anchor
        for path_component in relative_path.split(os.sep):
            current_path = os.path.join(current_path, path_component)
            try:
                os.lstat(current_path)
            except FileNotFoundError:
                break
            except (OSError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照祖先安全检查失败 {current_path}: {e}",
                    include_print=False,
                )
                return False

            try:
                resolved_path = os.path.realpath(current_path)
                if not self._path_is_contained(
                    resolved_path,
                    resolved_anchor,
                ):
                    self._report_best_effort(
                        "error",
                        f"快照祖先逃逸可信目录: {current_path}",
                        include_print=False,
                    )
                    return False

                followed_stat = os.stat(current_path)
                identity = (
                    followed_stat.st_dev,
                    followed_stat.st_ino,
                )
            except (OSError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照祖先安全检查失败 {current_path}: {e}",
                    include_print=False,
                )
                return False

            if identity in visited_identities:
                self._report_best_effort(
                    "error",
                    f"快照祖先循环链接: {current_path}",
                    include_print=False,
                )
                return False
            visited_identities.add(identity)

        return True

    def _inspect_snapshot_path(self, root_path):
        try:
            absolute_root = os.path.abspath(root_path)
            resolved_root = os.path.realpath(absolute_root)
            root_stat = os.stat(absolute_root)
        except (OSError, TypeError, ValueError) as e:
            self._report_best_effort(
                "error",
                f"快照安全检查失败 {root_path}: {e}",
                include_print=False,
            )
            return False, ()

        if not stat.S_ISDIR(root_stat.st_mode):
            return True, ()

        root_identity = (root_stat.st_dev, root_stat.st_ino)
        pending_directories = [
            (absolute_root, frozenset((root_identity,)))
        ]
        visited_directory_identities = {root_identity}
        aliases = []

        while pending_directories:
            current_path, ancestor_identities = (
                pending_directories.pop()
            )
            try:
                with os.scandir(current_path) as directory_entries:
                    entries = list(directory_entries)
            except (OSError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照安全检查失败 {current_path}: {e}",
                    include_print=False,
                )
                return False, ()

            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except (OSError, ValueError) as e:
                    self._report_best_effort(
                        "error",
                        f"快照安全检查失败 {entry.path}: {e}",
                        include_print=False,
                    )
                    return False, ()

                if self._snapshot_entry_is_link_or_reparse(entry_stat):
                    is_junction = bool(
                        os.name == "nt"
                        and (
                            getattr(
                                os.path,
                                "isjunction",
                                lambda _path: False,
                            )(entry.path)
                            or getattr(entry_stat, "st_reparse_tag", None)
                            == getattr(
                                stat,
                                "IO_REPARSE_TAG_MOUNT_POINT",
                                0xA0000003,
                            )
                        )
                    )
                    if not is_junction and not entry.is_symlink():
                        self._report_best_effort(
                            "error",
                            f"快照拒绝不支持的重解析点: {entry.path}",
                            include_print=False,
                        )
                        return False, ()

                    try:
                        resolved_target = os.path.realpath(entry.path)
                        if not self._path_is_contained(
                            resolved_target,
                            resolved_root,
                        ):
                            self._report_best_effort(
                                "error",
                                f"快照拒绝外部链接或重解析点: {entry.path}",
                                include_print=False,
                            )
                            return False, ()

                        followed_stat = os.stat(entry.path)
                        target_identity = (
                            followed_stat.st_dev,
                            followed_stat.st_ino,
                        )
                    except (OSError, ValueError) as e:
                        self._report_best_effort(
                            "error",
                            f"快照链接安全检查失败 {entry.path}: {e}",
                            include_print=False,
                        )
                        return False, ()

                    target_is_directory = stat.S_ISDIR(
                        followed_stat.st_mode
                    )
                    if (
                        target_is_directory
                        and target_identity in ancestor_identities
                    ):
                        self._report_best_effort(
                            "error",
                            f"快照拒绝循环链接或重解析点: {entry.path}",
                            include_print=False,
                        )
                        return False, ()

                    try:
                        relative_entry = os.path.relpath(
                            entry.path,
                            absolute_root,
                        )
                        relative_target = os.path.relpath(
                            resolved_target,
                            resolved_root,
                        )
                        link_target = (
                            None
                            if is_junction
                            else os.readlink(entry.path)
                        )
                    except (OSError, ValueError) as e:
                        self._report_best_effort(
                            "error",
                            f"快照链接安全检查失败 {entry.path}: {e}",
                            include_print=False,
                        )
                        return False, ()

                    aliases.append(
                        (
                            relative_entry,
                            relative_target,
                            "junction" if is_junction else "symlink",
                            link_target,
                            target_is_directory,
                        )
                    )
                    continue

                if not stat.S_ISDIR(entry_stat.st_mode):
                    continue

                identity = (entry_stat.st_dev, entry_stat.st_ino)
                if identity in ancestor_identities:
                    self._report_best_effort(
                        "error",
                        f"快照拒绝目录循环: {entry.path}",
                        include_print=False,
                    )
                    return False, ()
                if identity in visited_directory_identities:
                    continue
                visited_directory_identities.add(identity)
                pending_directories.append(
                    (
                        entry.path,
                        ancestor_identities | frozenset((identity,)),
                    )
                )

        aliases.sort(key=lambda alias: alias[0])
        return True, tuple(aliases)

    def _snapshot_path_is_safe(self, root_path):
        path_is_safe, _aliases = self._inspect_snapshot_path(root_path)
        return path_is_safe

    def _recreate_snapshot_aliases(
        self,
        staging_path,
        destination_path,
        aliases,
    ):
        for (
            relative_entry,
            relative_target,
            alias_kind,
            link_target,
            target_is_directory,
        ) in sorted(
            aliases,
            key=lambda alias: alias[0].count(os.sep),
            reverse=True,
        ):
            staged_alias = os.path.join(staging_path, relative_entry)
            try:
                if os.path.isdir(staged_alias):
                    shutil.rmtree(staged_alias)
                elif os.path.lexists(staged_alias):
                    os.remove(staged_alias)

                if alias_kind == "junction":
                    future_target = os.path.join(
                        destination_path,
                        relative_target,
                    )
                    result = subprocess.run(
                        [
                            "cmd.exe",
                            "/d",
                            "/c",
                            "mklink",
                            "/J",
                            staged_alias,
                            future_target,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode != 0:
                        raise OSError(
                            result.stderr.strip()
                            or result.stdout.strip()
                            or "junction creation failed"
                        )
                else:
                    if os.path.isabs(link_target):
                        link_target = os.path.join(
                            destination_path,
                            relative_target,
                        )
                    os.symlink(
                        link_target,
                        staged_alias,
                        target_is_directory=target_is_directory,
                    )
            except (OSError, TypeError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照链接重建失败 {relative_entry}: {e}",
                    include_print=False,
                )
                return False

        return True

    def _cleanup_snapshot_artifact(self, path):
        path_exists = self._snapshot_path_lexists(path)
        if path_exists is False:
            return True
        if path_exists is None:
            return False

        try:
            if self.smart_remove(path):
                return True
        except Exception as e:
            self._report_best_effort(
                "error",
                f"快照临时路径清理失败 {path}: {e}",
                include_print=False,
            )
            return False

        self._report_best_effort(
            "error",
            f"快照临时路径清理失败: {path}",
            include_print=False,
        )
        return False

    @staticmethod
    def _snapshot_path_lexists(path):
        if path is None:
            return False
        try:
            return os.path.lexists(path)
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _atomic_move_no_replace(source_path, destination_path):
        if os.name == "nt":
            os.rename(source_path, destination_path)
            return

        if sys.platform.startswith("linux"):
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                raise OSError("atomic no-replace rename is unavailable")
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source_path),
                -100,
                os.fsencode(destination_path),
                1,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(
                    error_number,
                    os.strerror(error_number),
                    destination_path,
                )
            return

        if sys.platform == "darwin":
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renamex_np = getattr(libc, "renamex_np", None)
            if renamex_np is None:
                raise OSError("atomic no-replace rename is unavailable")
            renamex_np.argtypes = (
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renamex_np.restype = ctypes.c_int
            result = renamex_np(
                os.fsencode(source_path),
                os.fsencode(destination_path),
                0x00000004,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(
                    error_number,
                    os.strerror(error_number),
                    destination_path,
                )
            return

        raise OSError("atomic no-replace rename is unavailable")

    def _recover_old_snapshot_destination(
        self,
        old_path,
        destination_path,
    ):
        if old_path is None:
            return None, False, None, False

        old_exists = self._snapshot_path_lexists(old_path)
        destination_exists = self._snapshot_path_lexists(
            destination_path
        )
        if old_exists is False:
            return None, False, None, False
        if old_exists is None:
            return old_path, True, None, True
        if destination_exists is True:
            return old_path, True, None, False
        if destination_exists is None:
            return old_path, True, None, True

        try:
            self._atomic_move_no_replace(
                old_path,
                destination_path,
            )
        except BaseException as recovery_error:
            old_exists = self._snapshot_path_lexists(old_path)
            destination_exists = self._snapshot_path_lexists(
                destination_path
            )
            if (
                destination_exists is True
                and old_exists is False
            ):
                return None, False, recovery_error, False
            return (
                old_path,
                True,
                recovery_error,
                old_exists is None or destination_exists is None,
            )

        return None, False, None, False

    def _replace_with_staged_snapshot(
        self,
        src_path,
        dst_path,
        source_anchor=None,
        destination_anchor=None,
    ):
        destination_path = os.path.normpath(dst_path)
        parent_path = os.path.dirname(destination_path) or os.curdir
        destination_name = os.path.basename(destination_path)
        staging_path = None
        old_path = None
        preserve_old_path = False
        preserve_staging_path = False
        source_aliases = ()
        initial_destination_identity = None

        if source_anchor is None and destination_anchor is None:
            if (
                self._path_is_contained(src_path, DEFAULT_TARGET_DIR)
                and self._path_is_contained(parent_path, DEFAULT_DIR)
            ):
                source_anchor = DEFAULT_TARGET_DIR
                destination_anchor = DEFAULT_DIR
            elif (
                self._path_is_contained(src_path, DEFAULT_DIR)
                and self._path_is_contained(
                    parent_path,
                    DEFAULT_TARGET_DIR,
                )
            ):
                source_anchor = DEFAULT_DIR
                destination_anchor = DEFAULT_TARGET_DIR

        if (
            source_anchor is not None
            and not self._snapshot_ancestor_chain_is_safe(
                src_path,
                source_anchor,
            )
        ):
            return False
        if (
            destination_anchor is not None
            and not self._snapshot_ancestor_chain_is_safe(
                parent_path,
                destination_anchor,
            )
        ):
            return False
        source_is_safe, source_aliases = self._inspect_snapshot_path(
            src_path
        )
        if not source_is_safe:
            return False
        try:
            destination_exists = os.path.lexists(destination_path)
        except (OSError, ValueError) as e:
            self._report_best_effort(
                "error",
                f"快照目标安全检查失败 {destination_path}: {e}",
                include_print=False,
            )
            return False
        if (
            destination_exists
            and not self._snapshot_path_is_safe(destination_path)
        ):
            return False
        if destination_exists:
            try:
                initial_destination_identity = (
                    self._ordinary_config_stat_signature(
                        os.lstat(destination_path)
                    )
                )
            except (OSError, TypeError, ValueError) as e:
                self._report_best_effort(
                    "error",
                    f"快照目标身份检查失败 {destination_path}: {e}",
                    include_print=False,
                )
                return False

        try:
            os.makedirs(parent_path, exist_ok=True)
            if (
                source_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    src_path,
                    source_anchor,
                )
            ):
                return False
            if (
                destination_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    parent_path,
                    destination_anchor,
                )
            ):
                return False
            source_is_safe, source_aliases = self._inspect_snapshot_path(
                src_path
            )
            if not source_is_safe:
                return False
            staging_path = tempfile.mkdtemp(
                prefix=f".{destination_name}.staging-",
                dir=parent_path,
            )
            if not self.smart_copy(src_path, staging_path):
                self._report_best_effort(
                    "error",
                    f"快照暂存复制失败: {src_path} -> {staging_path}",
                    include_print=False,
                )
                return False

            source_is_safe, current_source_aliases = (
                self._inspect_snapshot_path(src_path)
            )
            if (
                not source_is_safe
                or current_source_aliases != source_aliases
            ):
                return False
            if not self._recreate_snapshot_aliases(
                staging_path,
                destination_path,
                source_aliases,
            ):
                return False

            if (
                source_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    src_path,
                    source_anchor,
                )
            ):
                return False
            if (
                destination_anchor is not None
                and not self._snapshot_ancestor_chain_is_safe(
                    parent_path,
                    destination_anchor,
                )
            ):
                return False
            source_is_safe, current_source_aliases = (
                self._inspect_snapshot_path(src_path)
            )
            if (
                not source_is_safe
                or current_source_aliases != source_aliases
            ):
                return False
            if (
                os.path.lexists(destination_path)
                and not self._snapshot_path_is_safe(destination_path)
            ):
                return False

            if destination_exists:
                old_path = tempfile.mkdtemp(
                    prefix=f".{destination_name}.old-",
                    dir=parent_path,
                )
                os.rmdir(old_path)
                try:
                    current_destination_identity = (
                        self._ordinary_config_stat_signature(
                            os.lstat(destination_path)
                        )
                    )
                except (OSError, TypeError, ValueError):
                    current_destination_identity = None
                if (
                    current_destination_identity
                    != initial_destination_identity
                ):
                    raise OSError(
                        "snapshot destination identity changed before move"
                    )
                self._atomic_move_no_replace(
                    destination_path,
                    old_path,
                )
                try:
                    moved_destination_identity = (
                        self._ordinary_config_stat_signature(
                            os.lstat(old_path)
                        )
                    )
                except (OSError, TypeError, ValueError):
                    moved_destination_identity = None
                if (
                    moved_destination_identity
                    != initial_destination_identity
                ):
                    try:
                        self._atomic_move_no_replace(
                            old_path,
                            destination_path,
                        )
                        old_path = None
                    except BaseException:
                        preserve_old_path = True
                    raise OSError(
                        "snapshot destination identity changed during move"
                    )

            self._atomic_move_no_replace(
                staging_path,
                destination_path,
            )
            staging_path = None

            if old_path is not None:
                if not self._cleanup_snapshot_artifact(old_path):
                    preserve_old_path = True
                    return False
                old_path = None

            return True
        except Exception as e:
            (
                old_path,
                preserve_old_path,
                recovery_error,
                recovery_is_indeterminate,
            ) = self._recover_old_snapshot_destination(
                old_path,
                destination_path,
            )
            preserve_staging_path = recovery_is_indeterminate
            if (
                recovery_error is not None
                and not isinstance(recovery_error, Exception)
            ):
                raise recovery_error
            if recovery_error is not None:
                self._report_best_effort(
                    "error",
                    "快照替换回滚失败，旧目标保留在 "
                    f"{old_path}: {recovery_error}",
                    include_print=False,
                )
            self._report_best_effort(
                "error",
                f"快照替换失败 {src_path} -> {destination_path}: {e}",
                include_print=False,
            )
            return False
        except BaseException:
            (
                old_path,
                preserve_old_path,
                _recovery_error,
                recovery_is_indeterminate,
            ) = self._recover_old_snapshot_destination(
                old_path,
                destination_path,
            )
            preserve_staging_path = recovery_is_indeterminate
            raise
        finally:
            if not preserve_staging_path:
                self._cleanup_snapshot_artifact(staging_path)
            if (
                old_path is not None
                and not preserve_old_path
                and self._snapshot_path_lexists(destination_path)
            ):
                self._cleanup_snapshot_artifact(old_path)

    @staticmethod
    def _ordinary_config_stat_signature(entry_stat):
        return (
            entry_stat.st_mode,
            entry_stat.st_dev,
            entry_stat.st_ino,
            entry_stat.st_size,
            getattr(entry_stat, "st_mtime_ns", None),
            getattr(entry_stat, "st_ctime_ns", None),
            getattr(entry_stat, "st_file_attributes", None),
            getattr(entry_stat, "st_reparse_tag", None),
        )

    def _ordinary_config_reparse_kind(self, path, entry_stat):
        if not self._snapshot_entry_is_link_or_reparse(entry_stat):
            return None, None

        is_junction = bool(
            os.name == "nt"
            and (
                getattr(
                    os.path,
                    "isjunction",
                    lambda _path: False,
                )(path)
                or getattr(entry_stat, "st_reparse_tag", None)
                == getattr(
                    stat,
                    "IO_REPARSE_TAG_MOUNT_POINT",
                    0xA0000003,
                )
            )
        )
        if is_junction:
            return "junction", None
        if stat.S_ISLNK(entry_stat.st_mode):
            return "symlink", None
        return None, "unsupported_reparse"

    @staticmethod
    def _ordinary_config_file_digest(path, expected_stat):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(file_descriptor)
            expected_identity = (
                expected_stat.st_dev,
                expected_stat.st_ino,
                stat.S_IFMT(expected_stat.st_mode),
            )
            opened_identity = (
                opened_stat.st_dev,
                opened_stat.st_ino,
                stat.S_IFMT(opened_stat.st_mode),
            )
            if opened_identity != expected_identity:
                raise OSError("file identity changed while opening")

            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)

            final_stat = os.fstat(file_descriptor)
            if (
                ConfigManager._ordinary_config_stat_signature(final_stat)
                != ConfigManager._ordinary_config_stat_signature(opened_stat)
            ):
                raise OSError("file changed while hashing")
            return digest.hexdigest()
        finally:
            os.close(file_descriptor)

    @staticmethod
    @contextmanager
    def _ordinary_config_bound_directory(path, expected_stat):
        if (
            not stat.S_ISDIR(expected_stat.st_mode)
            or ConfigManager._snapshot_entry_is_link_or_reparse(
                expected_stat
            )
        ):
            raise OSError("directory binding rejected unsafe entry")

        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class ByHandleFileInformation(ctypes.Structure):
                _fields_ = (
                    ("file_attributes", wintypes.DWORD),
                    ("creation_time", wintypes.FILETIME),
                    ("last_access_time", wintypes.FILETIME),
                    ("last_write_time", wintypes.FILETIME),
                    ("volume_serial_number", wintypes.DWORD),
                    ("file_size_high", wintypes.DWORD),
                    ("file_size_low", wintypes.DWORD),
                    ("number_of_links", wintypes.DWORD),
                    ("file_index_high", wintypes.DWORD),
                    ("file_index_low", wintypes.DWORD),
                )

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            get_file_information = (
                kernel32.GetFileInformationByHandle
            )
            get_file_information.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(ByHandleFileInformation),
            )
            get_file_information.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = create_file(
                path,
                0x00000001,
                0x00000001 | 0x00000002,
                None,
                3,
                0x00200000 | 0x02000000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                information = ByHandleFileInformation()
                if not get_file_information(
                    handle,
                    ctypes.byref(information),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                file_attributes = information.file_attributes
                opened_inode = (
                    information.file_index_high << 32
                ) | information.file_index_low
                if (
                    not file_attributes & 0x00000010
                    or file_attributes & 0x00000400
                    or opened_inode != expected_stat.st_ino
                ):
                    raise OSError(
                        "directory handle identity or type changed"
                    )
                current_stat = os.lstat(path)
                if (
                    ConfigManager._ordinary_config_stat_signature(
                        current_stat
                    )
                    != ConfigManager._ordinary_config_stat_signature(
                        expected_stat
                    )
                ):
                    raise OSError(
                        "directory path identity changed after binding"
                    )
                yield path
            finally:
                close_handle(handle)
            return

        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(file_descriptor)
            expected_identity = (
                expected_stat.st_dev,
                expected_stat.st_ino,
                stat.S_IFMT(expected_stat.st_mode),
            )
            opened_identity = (
                opened_stat.st_dev,
                opened_stat.st_ino,
                stat.S_IFMT(opened_stat.st_mode),
            )
            if opened_identity != expected_identity:
                raise OSError("directory identity changed while opening")
            yield file_descriptor
        finally:
            os.close(file_descriptor)

    def _ordinary_config_manifest(self, root_path, reject_reparse):
        absolute_root = os.path.abspath(root_path)
        entries = []
        root_kind = None

        def inspect_entry(current_path, relative_path):
            nonlocal root_kind
            try:
                entry_stat = os.lstat(current_path)
            except FileNotFoundError:
                return "missing_path"
            except (OSError, TypeError, ValueError):
                return "identity_check_failed"

            reparse_kind, reparse_error = (
                self._ordinary_config_reparse_kind(
                    current_path,
                    entry_stat,
                )
            )
            if reparse_error is not None:
                return reparse_error
            if reparse_kind is not None:
                if reject_reparse:
                    return "unsupported_reparse"
                try:
                    link_value = os.readlink(current_path)
                except (OSError, TypeError, ValueError):
                    return "identity_check_failed"
                entry_kind = reparse_kind
                digest = link_value
            elif stat.S_ISREG(entry_stat.st_mode):
                entry_kind = "file"
                try:
                    digest = self._ordinary_config_file_digest(
                        current_path,
                        entry_stat,
                    )
                except (OSError, TypeError, ValueError):
                    return "identity_check_failed"
            elif stat.S_ISDIR(entry_stat.st_mode):
                entry_kind = "directory"
                digest = None
            else:
                return "unsupported_type"

            if relative_path == os.curdir:
                root_kind = entry_kind
            entries.append(
                (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    self._ordinary_config_stat_signature(entry_stat),
                    digest,
                )
            )

            if entry_kind != "directory":
                return None
            try:
                with self._ordinary_config_bound_directory(
                    current_path,
                    entry_stat,
                ) as enumeration_target:
                    with os.scandir(
                        enumeration_target
                    ) as directory_entries:
                        child_names = sorted(
                            entry.name for entry in directory_entries
                        )
                    for child_name in child_names:
                        child_relative_path = (
                            child_name
                            if relative_path == os.curdir
                            else os.path.join(
                                relative_path,
                                child_name,
                            )
                        )
                        child_error = inspect_entry(
                            os.path.join(current_path, child_name),
                            child_relative_path,
                        )
                        if child_error is not None:
                            return child_error
            except (OSError, TypeError, ValueError):
                return "identity_check_failed"
            return None

        manifest_error = inspect_entry(absolute_root, os.curdir)
        if manifest_error is not None:
            return None, manifest_error

        entries.sort(key=lambda entry: entry[0])
        return (root_kind, tuple(entries)), None

    @staticmethod
    def _ordinary_config_content_manifest(manifest):
        root_kind, entries = manifest
        return (
            root_kind,
            tuple(
                (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    stat.S_IMODE(entry_signature[0]),
                    entry_signature[3] if entry_kind == "file" else None,
                    digest,
                )
                for (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    entry_signature,
                    digest,
                ) in entries
            ),
        )

    @staticmethod
    def _ordinary_config_stable_manifest(manifest):
        root_kind, entries = manifest
        return (
            root_kind,
            tuple(
                (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    (
                        entry_signature[0],
                        entry_signature[1],
                        entry_signature[2],
                        entry_signature[3],
                        entry_signature[4],
                        entry_signature[6],
                        entry_signature[7],
                    ),
                    digest,
                )
                for (
                    relative_path,
                    entry_kind,
                    reparse_kind,
                    entry_signature,
                    digest,
                ) in entries
            ),
        )

    def _ordinary_config_source_state(self, source_path):
        try:
            lexical_root = os.path.abspath(CONFIG_DIR)
            lexical_path = os.path.abspath(source_path)
            resolved_root = os.path.realpath(lexical_root)
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"
        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"

        manifest, manifest_error = self._ordinary_config_manifest(
            lexical_path,
            reject_reparse=True,
        )
        if manifest_error is not None:
            return None, manifest_error
        return (
            manifest[0],
            os.path.normcase(os.path.normpath(lexical_path)),
            manifest,
        ), None

    def _ordinary_config_staging_is_safe(
        self,
        staging_path,
        expected_content_manifest,
    ):
        manifest, manifest_error = self._ordinary_config_manifest(
            staging_path,
            reject_reparse=True,
        )
        return (
            manifest_error is None
            and self._ordinary_config_content_manifest(manifest)
            == expected_content_manifest
        )

    def _ordinary_config_destination_is_current(self, destination_path):
        try:
            destination_relative_path = os.path.relpath(
                destination_path,
                DEFAULT_TARGET_DIR,
            )
        except (OSError, TypeError, ValueError):
            return False, "invalid_path"

        current_path, path_error = (
            self._resolve_ordinary_config_destination(
                destination_relative_path
            )
        )
        if path_error is not None:
            return False, path_error
        if os.path.normcase(os.path.normpath(current_path)) != os.path.normcase(
            os.path.normpath(destination_path)
        ):
            return False, "identity_check_failed"
        return True, None

    def _ordinary_config_destination_chain_paths(self, destination_path):
        try:
            trusted_root = os.path.abspath(DEFAULT_TARGET_DIR)
            parent_path = os.path.abspath(
                os.path.dirname(destination_path) or os.curdir
            )
            relative_parent = os.path.relpath(parent_path, trusted_root)
        except (OSError, TypeError, ValueError):
            return None
        if not self._path_is_contained(parent_path, trusted_root):
            return None

        chain_paths = [trusted_root]
        if relative_parent != os.curdir:
            current_path = trusted_root
            for component in relative_parent.split(os.sep):
                current_path = os.path.join(current_path, component)
                chain_paths.append(current_path)
        return tuple(chain_paths)

    def _ordinary_config_destination_binding(self, destination_path):
        chain_paths = self._ordinary_config_destination_chain_paths(
            destination_path
        )
        if chain_paths is None:
            return None, "identity_check_failed"

        chain_entries = []
        for chain_path in chain_paths:
            try:
                entry_stat = os.lstat(chain_path)
            except (OSError, TypeError, ValueError):
                return None, "identity_check_failed"
            if (
                self._snapshot_entry_is_link_or_reparse(entry_stat)
                or not stat.S_ISDIR(entry_stat.st_mode)
            ):
                return None, "identity_check_failed"
            chain_entries.append(
                (
                    os.path.normcase(os.path.normpath(chain_path)),
                    (
                        stat.S_IFMT(entry_stat.st_mode),
                        entry_stat.st_dev,
                        entry_stat.st_ino,
                        getattr(
                            entry_stat,
                            "st_file_attributes",
                            None,
                        ),
                        getattr(entry_stat, "st_reparse_tag", None),
                    ),
                )
            )

        destination_exists = self._snapshot_path_lexists(destination_path)
        if destination_exists is None:
            return None, "identity_check_failed"
        destination_manifest = None
        if destination_exists:
            destination_manifest, manifest_error = (
                self._ordinary_config_manifest(
                    destination_path,
                    reject_reparse=False,
                )
            )
            if manifest_error is not None:
                return None, "identity_check_failed"
            destination_manifest = (
                self._ordinary_config_stable_manifest(
                    destination_manifest
                )
            )
        return (
            destination_exists,
            destination_manifest,
            tuple(chain_entries),
        ), None

    def _ordinary_config_binding_matches(
        self,
        destination_path,
        initial_binding,
        expected_destination,
    ):
        current_binding, binding_error = (
            self._ordinary_config_destination_binding(destination_path)
        )
        if binding_error is not None:
            return False
        if current_binding[2] != initial_binding[2]:
            return False
        expected_exists, expected_manifest = expected_destination
        return (
            current_binding[0] is expected_exists
            and current_binding[1] == expected_manifest
        )

    def _ordinary_config_acquire_chain_handles(self, destination_path):
        chain_paths = self._ordinary_config_destination_chain_paths(
            destination_path
        )
        if chain_paths is None:
            raise OSError("invalid destination parent chain")

        handles = []
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL(
                    "kernel32",
                    use_last_error=True,
                )
                create_file = kernel32.CreateFileW
                create_file.argtypes = (
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                )
                create_file.restype = wintypes.HANDLE
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL
                invalid_handle = ctypes.c_void_p(-1).value
                for chain_path in chain_paths:
                    handle = create_file(
                        chain_path,
                        0x80000000,
                        0x00000001 | 0x00000002,
                        None,
                        3,
                        0x02000000 | 0x00200000,
                        None,
                    )
                    if handle == invalid_handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    handles.append(("windows", handle, close_handle))
            else:
                flags = os.O_RDONLY
                flags |= getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                for chain_path in chain_paths:
                    handles.append(
                        ("posix", os.open(chain_path, flags), None)
                    )
            return handles
        except BaseException:
            self._ordinary_config_release_chain_handles(handles)
            raise

    @staticmethod
    def _ordinary_config_release_chain_handles(handles):
        for handle_kind, handle, close_handle in reversed(handles):
            try:
                if handle_kind == "windows":
                    close_handle(handle)
                else:
                    os.close(handle)
            except BaseException:
                pass

    @staticmethod
    def _reserve_sibling_artifact(parent_path, prefix):
        file_descriptor, artifact_path = tempfile.mkstemp(
            prefix=prefix,
            dir=parent_path,
        )
        os.close(file_descriptor)
        os.remove(artifact_path)
        return artifact_path

    def _replace_with_staged_ordinary_config(
        self,
        source_path,
        destination_path,
    ):
        destination_path = os.path.normpath(destination_path)
        parent_path = os.path.dirname(destination_path) or os.curdir
        destination_name = os.path.basename(destination_path)
        staging_path = None
        old_path = None
        preserve_old_path = False
        preserve_staging_path = False
        destination_existed = False
        initial_destination_binding = None
        chain_handles = []
        staged_destination_manifest = None

        source_state, source_error = (
            self._ordinary_config_source_state(source_path)
        )
        if source_error is not None:
            self._report_ordinary_config_source_error(source_error)
            return False
        expected_source_content = (
            self._ordinary_config_content_manifest(source_state[2])
        )

        destination_is_current, destination_error = (
            self._ordinary_config_destination_is_current(
                destination_path
            )
        )
        if not destination_is_current:
            self._report_ordinary_config_destination_error(
                destination_error
            )
            return False

        try:
            os.makedirs(parent_path, exist_ok=True)
            (
                initial_destination_binding,
                destination_error,
            ) = self._ordinary_config_destination_binding(
                destination_path
            )
            if destination_error is not None:
                self._report_ordinary_config_destination_error(
                    destination_error
                )
                return False
            destination_existed = initial_destination_binding[0]
            chain_handles = (
                self._ordinary_config_acquire_chain_handles(
                    destination_path
                )
            )
            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                initial_destination_binding[:2],
            ):
                self._report_ordinary_config_destination_error(
                    "identity_changed"
                )
                return False

            staging_path = self._reserve_sibling_artifact(
                parent_path,
                f".{destination_name}.staging-",
            )

            current_source_state, source_error = (
                self._ordinary_config_source_state(source_path)
            )
            if (
                source_error is not None
                or current_source_state != source_state
            ):
                self._report_ordinary_config_source_error(
                    source_error or "identity_changed"
                )
                return False

            if not self.smart_copy(source_path, staging_path):
                self._report_best_effort(
                    "error",
                    "配置文件暂存复制失败: "
                    f"{source_path} -> {staging_path}",
                    include_print=False,
                )
                return False

            current_source_state, source_error = (
                self._ordinary_config_source_state(source_path)
            )
            if (
                source_error is not None
                or current_source_state != source_state
            ):
                self._report_ordinary_config_source_error(
                    source_error or "identity_changed"
                )
                return False
            if not self._ordinary_config_staging_is_safe(
                staging_path,
                expected_source_content,
            ):
                self._report_best_effort(
                    "error",
                    "配置文件暂存安全检查失败",
                    include_print=False,
                )
                return False

            current_source_state, source_error = (
                self._ordinary_config_source_state(source_path)
            )
            if (
                source_error is not None
                or current_source_state != source_state
            ):
                self._report_ordinary_config_source_error(
                    source_error or "identity_changed"
                )
                return False

            staged_destination_manifest, staging_error = (
                self._ordinary_config_manifest(
                    staging_path,
                    reject_reparse=True,
                )
            )
            if staging_error is not None:
                self._report_best_effort(
                    "error",
                    "配置文件暂存安全检查失败",
                    include_print=False,
                )
                return False
            staged_destination_manifest = (
                self._ordinary_config_stable_manifest(
                    staged_destination_manifest
                )
            )

            destination_is_current, destination_error = (
                self._ordinary_config_destination_is_current(
                    destination_path
                )
            )
            if not destination_is_current:
                self._report_ordinary_config_destination_error(
                    destination_error
                )
                return False
            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                initial_destination_binding[:2],
            ):
                self._report_ordinary_config_destination_error(
                    "identity_changed"
                )
                return False

            if destination_existed:
                old_path = self._reserve_sibling_artifact(
                    parent_path,
                    f".{destination_name}.old-",
                )
                if not self._ordinary_config_binding_matches(
                    destination_path,
                    initial_destination_binding,
                    initial_destination_binding[:2],
                ):
                    raise OSError(
                        "destination changed before recovery transition"
                    )
                self._atomic_move_no_replace(
                    destination_path,
                    old_path,
                )
                if not self._ordinary_config_binding_matches(
                    destination_path,
                    initial_destination_binding,
                    (False, None),
                ):
                    raise OSError(
                        "destination parent changed during recovery transition"
                    )
                old_manifest, old_error = self._ordinary_config_manifest(
                    old_path,
                    reject_reparse=False,
                )
                if (
                    old_error is not None
                    or self._ordinary_config_stable_manifest(
                        old_manifest
                    )
                    != initial_destination_binding[1]
                ):
                    try:
                        self._atomic_move_no_replace(
                            old_path,
                            destination_path,
                        )
                        old_path = None
                    except BaseException:
                        preserve_old_path = True
                    raise OSError(
                        "old destination identity changed during transition"
                    )

            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                (False, None),
            ):
                raise OSError(
                    "destination changed before staging transition"
                )
            self._atomic_move_no_replace(
                staging_path,
                destination_path,
            )
            staging_path = None
            if not self._ordinary_config_binding_matches(
                destination_path,
                initial_destination_binding,
                (True, staged_destination_manifest),
            ):
                raise OSError(
                    "destination changed during staging transition"
                )

            if old_path is not None:
                if not self._cleanup_snapshot_artifact(old_path):
                    preserve_old_path = True
                    raise OSError("旧配置清理失败")
                old_path = None

            return True
        except BaseException as replacement_error:
            recovery_error = None
            replacement_artifact = staging_path
            old_exists = self._snapshot_path_lexists(old_path)
            destination_exists = self._snapshot_path_lexists(
                destination_path
            )

            if old_exists is None or destination_exists is None:
                preserve_old_path = old_path is not None
                preserve_staging_path = replacement_artifact is not None
            elif old_exists is True:
                try:
                    if destination_exists is True:
                        if (
                            initial_destination_binding is None
                            or staged_destination_manifest is None
                            or not self._ordinary_config_binding_matches(
                                destination_path,
                                initial_destination_binding,
                                (
                                    True,
                                    staged_destination_manifest,
                                ),
                            )
                        ):
                            raise OSError(
                                "replacement destination identity is indeterminate"
                            )
                        replacement_exists = (
                            self._snapshot_path_lexists(
                                replacement_artifact
                            )
                        )
                        if replacement_exists is None:
                            raise OSError(
                                "replacement artifact state is indeterminate"
                            )
                        if replacement_artifact is None:
                            replacement_artifact = (
                                self._reserve_sibling_artifact(
                                    parent_path,
                                    f".{destination_name}.staging-",
                                )
                            )
                        elif replacement_exists:
                            replacement_artifact = (
                                self._reserve_sibling_artifact(
                                    parent_path,
                                    f".{destination_name}.staging-",
                                )
                            )
                        self._atomic_move_no_replace(
                            destination_path,
                            replacement_artifact,
                        )
                        if not self._ordinary_config_binding_matches(
                            destination_path,
                            initial_destination_binding,
                            (False, None),
                        ):
                            raise OSError(
                                "destination parent changed during recovery"
                            )
                    old_manifest, old_error = (
                        self._ordinary_config_manifest(
                            old_path,
                            reject_reparse=False,
                        )
                    )
                    if (
                        initial_destination_binding is None
                        or old_error is not None
                        or self._ordinary_config_stable_manifest(
                            old_manifest
                        )
                        != initial_destination_binding[1]
                    ):
                        raise OSError(
                            "old destination identity changed before recovery"
                        )
                    if not self._ordinary_config_binding_matches(
                        destination_path,
                        initial_destination_binding,
                        (False, None),
                    ):
                        raise OSError(
                            "destination changed before recovery"
                        )
                    self._atomic_move_no_replace(
                        old_path,
                        destination_path,
                    )
                    if not self._ordinary_config_binding_matches(
                        destination_path,
                        initial_destination_binding,
                        initial_destination_binding[:2],
                    ):
                        raise OSError(
                            "destination changed during recovery"
                        )
                    old_path = None
                    preserve_old_path = False
                except BaseException as error:
                    recovery_error = error
                    preserve_old_path = True
            elif (
                not destination_existed
                and destination_exists is True
            ):
                try:
                    if (
                        initial_destination_binding is None
                        or staged_destination_manifest is None
                        or not self._ordinary_config_binding_matches(
                            destination_path,
                            initial_destination_binding,
                            (True, staged_destination_manifest),
                        )
                    ):
                        raise OSError(
                            "new destination identity is indeterminate"
                        )
                    replacement_exists = self._snapshot_path_lexists(
                        replacement_artifact
                    )
                    if replacement_exists is None:
                        raise OSError(
                            "replacement artifact state is indeterminate"
                        )
                    if (
                        replacement_artifact is None
                        or replacement_exists
                    ):
                        replacement_artifact = (
                            self._reserve_sibling_artifact(
                                parent_path,
                                f".{destination_name}.staging-",
                            )
                        )
                    self._atomic_move_no_replace(
                        destination_path,
                        replacement_artifact,
                    )
                    if not self._ordinary_config_binding_matches(
                        destination_path,
                        initial_destination_binding,
                        (False, None),
                    ):
                        raise OSError(
                            "destination parent changed during recovery"
                        )
                except BaseException as error:
                    recovery_error = error

            staging_path = replacement_artifact
            if recovery_error is not None:
                self._report_best_effort(
                    "error",
                    "配置文件替换回滚失败，恢复数据保留在 "
                    f"{old_path}: {recovery_error}",
                    include_print=False,
                )
            self._report_best_effort(
                "error",
                "配置文件暂存替换失败 "
                f"{source_path} -> {destination_path}: "
                f"{replacement_error}",
                include_print=False,
            )
            if not isinstance(replacement_error, Exception):
                raise replacement_error
            if (
                recovery_error is not None
                and not isinstance(recovery_error, Exception)
            ):
                raise recovery_error
            return False
        finally:
            try:
                if not preserve_staging_path:
                    self._cleanup_snapshot_artifact(staging_path)
                if (
                    old_path is not None
                    and not preserve_old_path
                    and self._snapshot_path_lexists(destination_path)
                    is True
                ):
                    self._cleanup_snapshot_artifact(old_path)
            finally:
                self._ordinary_config_release_chain_handles(
                    chain_handles
                )

    def smart_remove(self, path):
        """
        智能删除方法，自动判断是文件还是目录

        Args:
            path (str): 要删除的路径

        Returns:
            bool: 删除是否成功
        """
        try:
            path_exists = self._snapshot_path_lexists(path)
            if path_exists is False:
                self._report_best_effort(
                    "warning",
                    f"路径不存在: {path}",
                    include_print=False,
                )
                return True  # 不存在也算"删除成功"
            if path_exists is None:
                self._report_best_effort(
                    "error",
                    f"无法确定路径是否存在: {path}",
                    include_print=False,
                )
                return False

            entry_stat = os.lstat(path)
            if self._snapshot_entry_is_link_or_reparse(entry_stat):
                is_junction = bool(
                    os.name == "nt"
                    and (
                        getattr(
                            os.path,
                            "isjunction",
                            lambda _path: False,
                        )(path)
                        or getattr(entry_stat, "st_reparse_tag", None)
                        == getattr(
                            stat,
                            "IO_REPARSE_TAG_MOUNT_POINT",
                            0xA0000003,
                        )
                    )
                )
                if (
                    is_junction
                    or stat.S_ISDIR(entry_stat.st_mode)
                ):
                    os.rmdir(path)
                else:
                    os.remove(path)
                self._report_best_effort(
                    "info",
                    f"链接或重解析点删除成功: {path}",
                )
            elif stat.S_ISREG(entry_stat.st_mode):
                # 删除文件
                os.remove(path)
                self._report_best_effort(
                    "info",
                    f"文件删除成功: {path}",
                )
            elif stat.S_ISDIR(entry_stat.st_mode):
                # 删除目录及其内容
                shutil.rmtree(path)
                self._report_best_effort(
                    "info",
                    f"目录删除成功: {path}",
                )
            else:
                self._report_best_effort(
                    "warning",
                    f"未知的路径类型: {path}",
                    include_print=False,
                )
                return False

            return True

        except PermissionError as e:
            self._report_best_effort(
                "error",
                f"权限错误，无法删除 {path}: {e}",
            )
            return False
        except Exception as e:
            self._report_best_effort(
                "error",
                f"删除失败 {path}: {e}",
            )
            return False

    def init_upgrade_version_list(self):
        print("初始化升级版本列表")
        self.logger.info("初始化升级版本列表")
        for version in ALL_VERSIONS:
            if version > TARGET_VERSION:
                break
            if version <= self.current_version:
                continue
            self.upgrade_version_list.append(version)

        self.logger.info(f"升级版本列表：{self.upgrade_version_list}")

    def _ordinary_config_path_error(self, relative_path):
        if not isinstance(relative_path, str):
            return "non_string_path"
        if not relative_path.strip():
            return "empty_path"
        if "\x00" in relative_path:
            return "nul_path"
        if relative_path.startswith(("/", "\\")):
            return "rooted_path"
        if ntpath.splitdrive(relative_path)[0]:
            return "drive_qualified_path"
        if os.path.isabs(relative_path):
            return "absolute_path"
        return None

    def _destination_entry_state(self, path):
        try:
            path_lexists = os.path.lexists(path)
        except (OSError, ValueError):
            return None, "identity_check_failed"

        try:
            os.lstat(path)
        except FileNotFoundError:
            if path_lexists:
                return None, "identity_check_failed"
            return False, None
        except (OSError, ValueError):
            return None, "identity_check_failed"

        try:
            if not os.path.exists(path):
                return None, "dangling_link"
        except (OSError, ValueError):
            return None, "identity_check_failed"
        return True, None

    def _resolve_ordinary_config_destination(self, relative_path):
        path_error = self._ordinary_config_path_error(relative_path)
        if path_error is not None:
            return None, path_error

        try:
            destination_path = os.path.abspath(
                os.path.join(DEFAULT_TARGET_DIR, relative_path)
            )
            lexical_roots = tuple(
                os.path.abspath(root)
                for root in (
                    UI_CONFIG_PATH,
                    UI_PIC_PATH,
                    AI_MODEL_CONFIG_PATH,
                    SCANNER_BARCODE_CONFIG_PATH,
                )
            )
        except (OSError, ValueError):
            return None, "invalid_path"

        lexical_root = next(
            (
                root
                for root in lexical_roots
                if self._path_is_contained(destination_path, root)
            ),
            None,
        )
        if lexical_root is None:
            return None, "outside_allowed_root"

        _, state_error = self._destination_entry_state(destination_path)
        if state_error is not None:
            return None, state_error

        try:
            resolved_root = os.path.realpath(lexical_root)
            resolved_destination = os.path.realpath(destination_path)
            resolved_parent = os.path.realpath(
                os.path.dirname(destination_path)
            )
        except (OSError, ValueError):
            return None, "invalid_path"

        destination_is_managed_root = os.path.normcase(
            os.path.normpath(destination_path)
        ) == os.path.normcase(os.path.normpath(lexical_root))
        if (
            not self._path_is_contained(resolved_destination, resolved_root)
            or (
                not destination_is_managed_root
                and not self._path_is_contained(resolved_parent, resolved_root)
            )
        ):
            return None, "symlink_escape"
        return destination_path, None

    def _report_ordinary_config_destination_error(self, error):
        message = f"ordinary_config error: destination_path:{error}"
        self._report_best_effort("error", message)

    def _ordinary_config_source_parent_chain_state(self, lexical_root):
        try:
            trusted_root = os.path.abspath(DEFAULT_DIR)
            expected_root = os.path.abspath(
                os.path.join(trusted_root, "resources", "config")
            )
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if os.path.normcase(os.path.normpath(lexical_root)) != os.path.normcase(
            os.path.normpath(expected_root)
        ):
            return None, "outside_allowed_root"

        chain = []
        for parent_path in (
            trusted_root,
            os.path.join(trusted_root, "resources"),
            expected_root,
        ):
            try:
                lexical_exists = os.path.lexists(parent_path)
            except (OSError, TypeError, ValueError):
                return None, "identity_check_failed"

            try:
                parent_stat = os.lstat(parent_path)
            except FileNotFoundError:
                if lexical_exists is False:
                    return None, "missing_parent"
                return None, "identity_check_failed"
            except (OSError, TypeError, ValueError):
                return None, "identity_check_failed"

            if lexical_exists is False:
                return None, "identity_check_failed"
            if self._snapshot_entry_is_link_or_reparse(parent_stat):
                return None, "symlink_escape"
            if not stat.S_ISDIR(parent_stat.st_mode):
                return None, "identity_check_failed"

            chain.append(
                (
                    os.path.normcase(os.path.normpath(parent_path)),
                    parent_stat.st_dev,
                    parent_stat.st_ino,
                    parent_stat.st_mode,
                    getattr(parent_stat, "st_file_attributes", None),
                    getattr(parent_stat, "st_reparse_tag", None),
                )
            )

        return tuple(chain), None

    def _ordinary_config_source_entry_state(self, lexical_path):
        try:
            lexical_exists = os.path.lexists(lexical_path)
        except (OSError, TypeError, ValueError):
            return None, "identity_check_failed"

        try:
            source_stat = os.lstat(lexical_path)
        except FileNotFoundError:
            if lexical_exists is False:
                return None, "missing_path"
            return None, "identity_check_failed"
        except (OSError, TypeError, ValueError):
            return None, "identity_check_failed"

        if lexical_exists is False:
            return None, "identity_check_failed"
        if self._snapshot_entry_is_link_or_reparse(source_stat):
            if stat.S_ISLNK(source_stat.st_mode):
                try:
                    os.stat(lexical_path)
                except FileNotFoundError:
                    return None, "dangling_link"
                except (OSError, TypeError, ValueError):
                    return None, "identity_check_failed"
            return None, "unsupported_type"
        if not (
            stat.S_ISREG(source_stat.st_mode)
            or stat.S_ISDIR(source_stat.st_mode)
        ):
            return None, "unsupported_type"
        return source_stat, None

    def _resolve_ordinary_config_source(self, destination_path):
        try:
            lexical_root = os.path.abspath(CONFIG_DIR)
            source_name = os.path.basename(os.path.normpath(destination_path))
            lexical_path = os.path.abspath(
                os.path.join(lexical_root, source_name)
            )
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"

        parent_chain, parent_error = (
            self._ordinary_config_source_parent_chain_state(lexical_root)
        )
        if parent_error is not None:
            return None, parent_error

        try:
            resolved_root = os.path.realpath(lexical_root)
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, TypeError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"

        _, source_error = self._ordinary_config_source_entry_state(
            lexical_path
        )

        confirmed_parent_chain, confirmed_parent_error = (
            self._ordinary_config_source_parent_chain_state(lexical_root)
        )
        if confirmed_parent_error is not None:
            return None, confirmed_parent_error
        if confirmed_parent_chain != parent_chain:
            return None, "identity_check_failed"

        if source_error == "missing_path":
            return lexical_path, source_error
        if source_error is not None:
            return None, source_error
        return lexical_path, None

    def _report_ordinary_config_source_error(self, error):
        message = f"ordinary_config error: source_path:{error}"
        self._report_best_effort("error", message)

    @staticmethod
    def _delete_normalized_path(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def _delete_chain_paths(self, anchor_path, leaf_path):
        try:
            anchor_path = os.path.normpath(os.path.abspath(anchor_path))
            leaf_path = os.path.normpath(os.path.abspath(leaf_path))
            if not self._path_is_contained(leaf_path, anchor_path):
                return None
            relative_path = os.path.relpath(leaf_path, anchor_path)
        except (OSError, TypeError, ValueError):
            return None

        paths = [anchor_path]
        if relative_path != os.curdir:
            current_path = anchor_path
            for component in relative_path.split(os.sep):
                current_path = os.path.join(current_path, component)
                paths.append(current_path)
        return tuple(paths)

    @staticmethod
    def _delete_identity_is_directory(identity):
        return bool(identity[2] & _FILE_ATTRIBUTE_DIRECTORY)

    @staticmethod
    def _delete_identity_is_reparse(identity):
        return bool(identity[2] & _FILE_ATTRIBUTE_REPARSE_POINT)

    def _delete_capture_directory_chain(
        self,
        ops,
        owner,
        chain_paths,
        *,
        allow_missing_tail,
    ):
        if not chain_paths:
            raise WindowsDeleteFailure("invalid_directory_chain")

        handles = []
        records = []
        missing = ()
        root_handle = owner.acquire(
            ops.open_absolute(chain_paths[0], directory=True)
        )
        handles.append(root_handle)
        root_identity = ops.identity(root_handle)
        if (
            not self._delete_identity_is_directory(root_identity)
            or self._delete_identity_is_reparse(root_identity)
        ):
            raise WindowsDeleteFailure("directory_chain_changed")
        records.append(
            (
                self._delete_normalized_path(chain_paths[0]),
                root_identity,
            )
        )

        parent_handle = root_handle
        for index, chain_path in enumerate(chain_paths[1:], start=1):
            component = os.path.basename(chain_path)
            if ops.name_is_absent(parent_handle, component):
                if not allow_missing_tail:
                    raise WindowsDeleteFailure("directory_chain_missing")
                missing = tuple(
                    os.path.basename(path)
                    for path in chain_paths[index:]
                )
                break
            child_handle = owner.acquire(
                ops.open_relative(
                    parent_handle,
                    component,
                    directory=True,
                )
            )
            handles.append(child_handle)
            identity = ops.identity(child_handle)
            if (
                not self._delete_identity_is_directory(identity)
                or self._delete_identity_is_reparse(identity)
            ):
                raise WindowsDeleteFailure("directory_chain_changed")
            records.append(
                (
                    self._delete_normalized_path(chain_path),
                    identity,
                )
            )
            parent_handle = child_handle
        return tuple(records), tuple(handles), missing

    def _delete_prepare_recovery_chain(
        self,
        ops,
        owner,
        source_chain,
        source_handles,
    ):
        trusted_root = os.path.normpath(os.path.abspath(DEFAULT_DIR))
        recovery_path = os.path.normpath(
            os.path.abspath(os.path.join(BACKUP_DIR, "recovery"))
        )
        chain_paths = self._delete_chain_paths(
            trusted_root,
            recovery_path,
        )
        if chain_paths is None:
            raise WindowsDeleteFailure("untrusted_recovery_directory")

        source_handles_by_path = {
            path: handle
            for (path, _identity), handle in zip(
                source_chain,
                source_handles,
            )
        }
        records = []
        handles = []
        parent_handle = None
        for index, chain_path in enumerate(chain_paths):
            normalized_path = self._delete_normalized_path(chain_path)
            existing_handle = source_handles_by_path.get(normalized_path)
            if existing_handle is not None:
                identity = ops.identity(existing_handle)
                records.append((normalized_path, identity))
                parent_handle = existing_handle
                continue

            if index == 0 or parent_handle is None:
                raise WindowsDeleteFailure("untrusted_recovery_directory")
            component = os.path.basename(chain_path)
            if ops.name_is_absent(parent_handle, component):
                try:
                    os.mkdir(chain_path)
                except FileExistsError:
                    pass
                except OSError:
                    raise WindowsDeleteFailure(
                        "recovery_directory_create_failed"
                    )
            handle = owner.acquire(
                ops.open_relative(
                    parent_handle,
                    component,
                    directory=True,
                )
            )
            handles.append(handle)
            identity = ops.identity(handle)
            if (
                not self._delete_identity_is_directory(identity)
                or self._delete_identity_is_reparse(identity)
            ):
                raise WindowsDeleteFailure(
                    "untrusted_recovery_directory"
                )
            records.append((normalized_path, identity))
            parent_handle = handle

        if not records or parent_handle is None:
            raise WindowsDeleteFailure("untrusted_recovery_directory")
        return (
            recovery_path,
            tuple(records),
            tuple(handles),
            parent_handle,
        )

    def _delete_probe_volume_capability(
        self,
        ops,
        recovery_handle,
        recovery_path,
        expected_volume_serial,
    ):
        source_name = f".delete-capability-source-{uuid.uuid4().hex}"
        target_name = f".delete-capability-target-{uuid.uuid4().hex}"
        source_path = os.path.join(recovery_path, source_name)
        target_path = os.path.join(recovery_path, target_name)
        probe_handle = None
        probe_identity = None
        probe_error = None
        cleanup_error = None
        previous_last_rename = ops.last_rename
        previous_disposition_flags = ops.last_disposition_flags

        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            descriptor = os.open(source_path, flags, 0o600)
            os.close(descriptor)
            probe_handle = ops.open_relative(
                recovery_handle,
                source_name,
                directory=False,
                delete=True,
            )
            probe_identity = ops.identity(probe_handle)
            if probe_identity[0] != expected_volume_serial:
                raise WindowsDeleteFailure(
                    "capability_probe_volume_mismatch"
                )

            ops.rename_relative_no_replace(
                probe_handle,
                recovery_handle,
                target_name,
            )
            if not ops.name_is_absent(recovery_handle, source_name):
                raise WindowsDeleteFailure(
                    "capability_probe_rename_failed"
                )
            verification_handle = ops.open_relative_attributes(
                recovery_handle,
                target_name,
            )
            try:
                if ops.identity(verification_handle) != probe_identity:
                    raise WindowsDeleteFailure(
                        "capability_probe_identity_changed"
                    )
            finally:
                ops.close(verification_handle)

            ops.set_delete_disposition(probe_handle)
            ops.close(probe_handle)
            probe_handle = None
            if not ops.name_is_absent(recovery_handle, target_name):
                raise WindowsDeleteFailure(
                    "capability_probe_disposition_failed"
                )
        except WindowsDeleteFailure as error:
            probe_error = error
        except (OSError, TypeError, ValueError):
            probe_error = WindowsDeleteFailure(
                "capability_probe_failed"
            )
        finally:
            if probe_handle is not None:
                try:
                    ops.close(probe_handle)
                except WindowsDeleteFailure as error:
                    cleanup_error = error
            for probe_path in (source_path, target_path):
                try:
                    if os.path.lexists(probe_path):
                        os.remove(probe_path)
                except (OSError, TypeError, ValueError):
                    cleanup_error = WindowsDeleteFailure(
                        "capability_probe_cleanup_failed"
                    )
            for probe_name in (source_name, target_name):
                try:
                    if not ops.name_is_absent(
                        recovery_handle,
                        probe_name,
                    ):
                        cleanup_error = WindowsDeleteFailure(
                            "capability_probe_cleanup_failed"
                        )
                except WindowsDeleteFailure:
                    cleanup_error = WindowsDeleteFailure(
                        "capability_probe_cleanup_failed"
                    )
            ops.last_rename = previous_last_rename
            ops.last_disposition_flags = previous_disposition_flags

        if cleanup_error is not None:
            raise cleanup_error
        if probe_error is not None:
            raise probe_error
        return _WindowsDeleteCapability(
            int(expected_volume_serial),
            True,
            True,
        )

    def _delete_managed_root(self, destination_path):
        destination_path = self._delete_normalized_path(destination_path)
        roots = (
            UI_CONFIG_PATH,
            UI_PIC_PATH,
            AI_MODEL_CONFIG_PATH,
            SCANNER_BARCODE_CONFIG_PATH,
        )
        return next(
            (
                os.path.normpath(os.path.abspath(root))
                for root in roots
                if self._path_is_contained(
                    destination_path,
                    self._delete_normalized_path(root),
                )
            ),
            None,
        )

    def _delete_target_manifest(self, ops, handle, *, directory):
        root_entry = ops.manifest_entry(handle)
        descendants = ops.capture_manifest(
            handle,
            directory=directory,
        )
        return root_entry, descendants

    def _preflight_ordinary_config_delete(
        self,
        destination_path,
        source_path,
        *,
        handle_ops=None,
    ):
        ops = handle_ops or WindowsHandleOps()
        if not ops.is_supported():
            return None, (
                ops.unsupported_category or "unsupported_api"
            )

        try:
            destination_path = os.path.normpath(
                os.path.abspath(destination_path)
            )
            source_path = os.path.normpath(os.path.abspath(source_path))
            resolved_destination, destination_error = (
                self._resolve_ordinary_config_destination(
                    os.path.relpath(
                        destination_path,
                        DEFAULT_TARGET_DIR,
                    )
                )
            )
            if (
                destination_error is not None
                or self._delete_normalized_path(resolved_destination)
                != self._delete_normalized_path(destination_path)
            ):
                try:
                    destination_stat = os.lstat(destination_path)
                except (OSError, TypeError, ValueError):
                    destination_stat = None
                if (
                    destination_stat is not None
                    and self._snapshot_entry_is_link_or_reparse(
                        destination_stat
                    )
                ):
                    return None, "unsupported_destination_reparse"
                return None, (
                    destination_error or "invalid_destination"
                )

            config_path = os.path.normpath(os.path.abspath(CONFIG_DIR))
            expected_source = os.path.normpath(
                os.path.abspath(
                    os.path.join(
                        config_path,
                        os.path.basename(destination_path),
                    )
                )
            )
            if (
                self._delete_normalized_path(source_path)
                != self._delete_normalized_path(expected_source)
            ):
                return None, "invalid_source"

            source_chain_paths = self._delete_chain_paths(
                DEFAULT_DIR,
                config_path,
            )
            if source_chain_paths is None:
                return None, "invalid_source_chain"
            managed_root_path = self._delete_managed_root(
                destination_path
            )
            if managed_root_path is None:
                return None, "outside_allowed_root"
            destination_parent = os.path.dirname(destination_path)
            destination_chain_paths = self._delete_chain_paths(
                DEFAULT_TARGET_DIR,
                destination_parent,
            )
            if destination_chain_paths is None:
                return None, "invalid_destination_chain"

            with WindowsHandleOwner(ops.close) as owner:
                (
                    source_chain,
                    source_handles,
                    source_missing,
                ) = self._delete_capture_directory_chain(
                    ops,
                    owner,
                    source_chain_paths,
                    allow_missing_tail=False,
                )
                if source_missing:
                    raise WindowsDeleteFailure("source_parent_changed")
                config_handle = source_handles[-1]
                if not ops.name_is_absent(
                    config_handle,
                    os.path.basename(source_path),
                ):
                    raise WindowsDeleteFailure("source_appeared")

                (
                    recovery_path,
                    recovery_chain,
                    _recovery_handles,
                    recovery_handle,
                ) = self._delete_prepare_recovery_chain(
                    ops,
                    owner,
                    source_chain,
                    source_handles,
                )
                recovery_identity = ops.identity(recovery_handle)

                (
                    destination_chain,
                    destination_handles,
                    destination_missing,
                ) = self._delete_capture_directory_chain(
                    ops,
                    owner,
                    destination_chain_paths,
                    allow_missing_tail=True,
                )
                destination_exists = False
                destination_directory = None
                destination_identity = None
                destination_manifest = None
                target_handle = None
                if not destination_missing:
                    destination_parent_handle = (
                        destination_handles[-1]
                    )
                    destination_name = os.path.basename(
                        destination_path
                    )
                    destination_exists = not ops.name_is_absent(
                        destination_parent_handle,
                        destination_name,
                    )
                    if destination_exists:
                        try:
                            target_stat = os.lstat(destination_path)
                        except (OSError, TypeError, ValueError):
                            raise WindowsDeleteFailure(
                                "destination_identity_failed"
                            )
                        destination_directory = stat.S_ISDIR(
                            target_stat.st_mode
                        )
                        target_handle = owner.acquire(
                            ops.open_relative(
                                destination_parent_handle,
                                destination_name,
                                directory=destination_directory,
                            )
                        )
                        destination_identity = ops.identity(
                            target_handle
                        )
                        if self._delete_identity_is_reparse(
                            destination_identity
                        ):
                            raise WindowsDeleteFailure(
                                "unsupported_destination_reparse"
                            )
                        if (
                            self._delete_identity_is_directory(
                                destination_identity
                            )
                            is not destination_directory
                        ):
                            raise WindowsDeleteFailure(
                                "destination_type_changed"
                            )
                        destination_manifest = (
                            self._delete_target_manifest(
                                ops,
                                target_handle,
                                directory=destination_directory,
                            )
                        )

                normalized_managed_root = (
                    self._delete_normalized_path(managed_root_path)
                )
                if (
                    self._delete_normalized_path(destination_path)
                    == normalized_managed_root
                ):
                    managed_root_exists = destination_exists
                else:
                    managed_root_exists = any(
                        path == normalized_managed_root
                        for path, _identity in destination_chain
                    )

                destination_volume = (
                    destination_identity[0]
                    if destination_identity is not None
                    else destination_chain[-1][1][0]
                )
                if recovery_identity[0] != destination_volume:
                    raise WindowsDeleteFailure(
                        "recovery_volume_mismatch"
                    )
                capability = self._delete_probe_volume_capability(
                    ops,
                    recovery_handle,
                    recovery_path,
                    destination_volume,
                )

                snapshot = _OrdinaryConfigDeleteSnapshot(
                    source_path,
                    os.path.basename(source_path),
                    source_chain,
                    destination_path,
                    os.path.basename(destination_path),
                    destination_chain,
                    destination_missing,
                    destination_exists,
                    destination_directory,
                    destination_identity,
                    destination_manifest,
                    os.path.normpath(managed_root_path),
                    managed_root_exists,
                    recovery_path,
                    recovery_chain,
                    recovery_identity,
                    capability,
                )
                return snapshot, None
        except WindowsDeleteFailure as error:
            return None, error.category
        except (OSError, TypeError, ValueError):
            return None, "snapshot_failed"

    def _delete_reacquire_chain(
        self,
        ops,
        owner,
        expected_chain,
        expected_missing=(),
    ):
        if not expected_chain:
            raise WindowsDeleteFailure("invalid_directory_chain")
        paths = tuple(path for path, _identity in expected_chain)
        if expected_missing:
            current = paths[-1]
            paths += tuple(
                os.path.join(current, *expected_missing[: index + 1])
                for index in range(len(expected_missing))
            )
        records, handles, missing = (
            self._delete_capture_directory_chain(
                ops,
                owner,
                paths,
                allow_missing_tail=bool(expected_missing),
            )
        )
        if records != expected_chain or missing != expected_missing:
            raise WindowsDeleteFailure("directory_chain_changed")
        return handles

    @staticmethod
    def _delete_held_chain_matches(ops, handles, expected_chain):
        if len(handles) != len(expected_chain):
            return False
        return all(
            ops.identity(handle) == expected_identity
            for handle, (_path, expected_identity) in zip(
                handles,
                expected_chain,
            )
        )

    def _delete_validate_before_rename_attempt(
        self,
        ops,
        owner,
        snapshot,
        artifact_handle,
        source_handles,
        destination_handles,
        recovery_handles,
    ):
        if not self._delete_held_chain_matches(
            ops,
            source_handles,
            snapshot.source_chain,
        ):
            raise WindowsDeleteFailure("source_chain_changed")
        if not self._delete_held_chain_matches(
            ops,
            destination_handles,
            snapshot.destination_chain,
        ):
            raise WindowsDeleteFailure(
                "destination_chain_changed"
            )
        if not self._delete_held_chain_matches(
            ops,
            recovery_handles,
            snapshot.recovery_chain,
        ):
            raise WindowsDeleteFailure("recovery_chain_changed")

        config_handle = source_handles[-1]
        destination_parent_handle = destination_handles[-1]
        recovery_handle = recovery_handles[-1]
        if not ops.name_is_absent(
            config_handle,
            snapshot.source_name,
        ):
            raise WindowsDeleteFailure("source_appeared")
        if (
            ops.identity(recovery_handle)[0]
            != snapshot.capability.volume_serial
        ):
            raise WindowsDeleteFailure("recovery_volume_changed")
        if not self._delete_verify_named_identity(
            ops,
            owner,
            destination_parent_handle,
            snapshot.destination_name,
            snapshot,
        ):
            raise WindowsDeleteFailure(
                "destination_binding_changed"
            )

        current_identity = ops.identity(artifact_handle)
        if (
            current_identity != snapshot.destination_identity
            or self._delete_identity_is_directory(current_identity)
            is not snapshot.destination_directory
            or self._delete_identity_is_reparse(current_identity)
            or self._delete_target_manifest(
                ops,
                artifact_handle,
                directory=snapshot.destination_directory,
            )
            != snapshot.destination_manifest
        ):
            raise WindowsDeleteFailure("destination_changed")

    def _delete_artifact_is_intact(
        self,
        ops,
        artifact_handle,
        snapshot,
    ):
        try:
            return (
                ops.identity(artifact_handle)
                == snapshot.destination_identity
                and self._delete_target_manifest(
                    ops,
                    artifact_handle,
                    directory=snapshot.destination_directory,
                )
                == snapshot.destination_manifest
            )
        except WindowsDeleteFailure:
            return False

    def _delete_verify_named_identity(
        self,
        ops,
        owner,
        parent_handle,
        name,
        snapshot,
    ):
        if ops.name_is_absent(parent_handle, name):
            return False
        verification_handle = owner.acquire(
            ops.open_relative_attributes(
                parent_handle,
                name,
            )
        )
        try:
            return (
                ops.identity(verification_handle)
                == snapshot.destination_identity
            )
        finally:
            ops.close(verification_handle)
            owner.release(verification_handle)

    def _delete_try_local_restore(
        self,
        ops,
        owner,
        snapshot,
        artifact_handle,
        artifact_name,
        source_handles,
        destination_handles,
        recovery_handles,
        artifact_state,
    ):
        destination_parent_handle = destination_handles[-1]
        recovery_handle = recovery_handles[-1]
        rename_back_completed = False
        try:
            if not self._delete_held_chain_matches(
                ops,
                source_handles,
                snapshot.source_chain,
            ):
                return _OrdinaryConfigDeleteRestoreResult(False, None)
            if not self._delete_held_chain_matches(
                ops,
                destination_handles,
                snapshot.destination_chain,
            ):
                return _OrdinaryConfigDeleteRestoreResult(False, None)
            if not self._delete_held_chain_matches(
                ops,
                recovery_handles,
                snapshot.recovery_chain,
            ):
                return _OrdinaryConfigDeleteRestoreResult(False, None)
            if not self._delete_artifact_is_intact(
                ops,
                artifact_handle,
                snapshot,
            ):
                return _OrdinaryConfigDeleteRestoreResult(False, None)
            if not ops.name_is_absent(
                destination_parent_handle,
                snapshot.destination_name,
            ):
                return _OrdinaryConfigDeleteRestoreResult(False, None)
            if not self._delete_verify_named_identity(
                ops,
                owner,
                recovery_handle,
                artifact_name,
                snapshot,
            ):
                return _OrdinaryConfigDeleteRestoreResult(False, None)

            ops.rename_relative_no_replace(
                artifact_handle,
                destination_parent_handle,
                snapshot.destination_name,
            )
            rename_back_completed = True
            artifact_state.path_valid = False
            if not ops.name_is_absent(recovery_handle, artifact_name):
                return self._delete_redetach_after_restore_failure(
                    ops,
                    owner,
                    snapshot,
                    artifact_handle,
                    destination_handles,
                    recovery_handles,
                    artifact_state,
                )
            restored = self._delete_verify_named_identity(
                ops,
                owner,
                destination_parent_handle,
                snapshot.destination_name,
                snapshot,
            )
            if restored:
                return _OrdinaryConfigDeleteRestoreResult(
                    True,
                    None,
                    False,
                )
            return self._delete_redetach_after_restore_failure(
                ops,
                owner,
                snapshot,
                artifact_handle,
                destination_handles,
                recovery_handles,
                artifact_state,
            )
        except WindowsDeleteFailure:
            if rename_back_completed:
                return self._delete_redetach_after_restore_failure(
                    ops,
                    owner,
                    snapshot,
                    artifact_handle,
                    destination_handles,
                    recovery_handles,
                    artifact_state,
                )
            return _OrdinaryConfigDeleteRestoreResult(False, None)

    def _delete_redetach_after_restore_failure(
        self,
        ops,
        owner,
        snapshot,
        artifact_handle,
        destination_handles,
        recovery_handles,
        artifact_state,
    ):
        destination_parent_handle = destination_handles[-1]
        recovery_handle = recovery_handles[-1]
        while True:
            candidate = (
                f".{snapshot.destination_name}.recovery-"
                f"{uuid.uuid4().hex}"
            )
            candidate_path = os.path.join(
                snapshot.recovery_path,
                candidate,
            )
            rename_accepted = False
            try:
                self._delete_validate_before_recovery_redetach_attempt(
                    ops,
                    snapshot,
                    artifact_handle,
                    destination_handles,
                    recovery_handles,
                )
                ops.rename_relative_no_replace(
                    artifact_handle,
                    recovery_handle,
                    candidate,
                )
                rename_accepted = True
                artifact_state.name = candidate
                artifact_state.path = candidate_path
                artifact_state.path_valid = True
                artifact_state.retain_only = True
            except WindowsDeleteFailure as error:
                if error.category == "rename_collision":
                    continue
                self._report_best_effort(
                    "error",
                    "delete_compatibility recovery re-detach failed: "
                    f"{error.category}; retained location indeterminate",
                )
                return _OrdinaryConfigDeleteRestoreResult(
                    False,
                    None,
                    False,
                )

            try:
                destination_is_absent = ops.name_is_absent(
                    destination_parent_handle,
                    snapshot.destination_name,
                )
                recovery_name_matches = (
                    self._delete_verify_named_identity(
                        ops,
                        owner,
                        recovery_handle,
                        candidate,
                        snapshot,
                    )
                )
                artifact_is_intact = self._delete_artifact_is_intact(
                    ops,
                    artifact_handle,
                    snapshot,
                )
            except WindowsDeleteFailure as error:
                self._report_best_effort(
                    "error",
                    "delete_compatibility recovery re-detach "
                    f"verification failed: {error.category}; "
                    "indeterminate recovery artifact retained: "
                    f"{candidate_path}",
                )
                return _OrdinaryConfigDeleteRestoreResult(
                    False,
                    candidate_path if rename_accepted else None,
                    False,
                )

            if not (
                destination_is_absent
                and recovery_name_matches
                and artifact_is_intact
            ):
                self._report_best_effort(
                    "error",
                    "delete_compatibility recovery re-detach "
                    "verification failed: recovery_binding_indeterminate; "
                    "indeterminate recovery artifact retained: "
                    f"{candidate_path}",
                )
                return _OrdinaryConfigDeleteRestoreResult(
                    False,
                    candidate_path if rename_accepted else None,
                    False,
                )
            return _OrdinaryConfigDeleteRestoreResult(
                False,
                candidate_path,
                False,
            )

    def _delete_validate_before_recovery_redetach_attempt(
        self,
        ops,
        snapshot,
        artifact_handle,
        destination_handles,
        recovery_handles,
    ):
        if not self._delete_held_chain_matches(
            ops,
            destination_handles,
            snapshot.destination_chain,
        ):
            raise WindowsDeleteFailure(
                "destination_chain_changed"
            )
        if not self._delete_held_chain_matches(
            ops,
            recovery_handles,
            snapshot.recovery_chain,
        ):
            raise WindowsDeleteFailure("recovery_chain_changed")

        recovery_identity = ops.identity(recovery_handles[-1])
        if (
            recovery_identity != snapshot.recovery_identity
            or recovery_identity[0]
            != snapshot.capability.volume_serial
        ):
            raise WindowsDeleteFailure(
                "recovery_directory_changed"
            )

        current_identity = ops.identity(artifact_handle)
        if (
            current_identity != snapshot.destination_identity
            or self._delete_identity_is_directory(current_identity)
            is not snapshot.destination_directory
            or self._delete_identity_is_reparse(current_identity)
            or self._delete_target_manifest(
                ops,
                artifact_handle,
                directory=snapshot.destination_directory,
            )
            != snapshot.destination_manifest
        ):
            raise WindowsDeleteFailure("recovery_object_changed")

    def _delete_retain_location(self, retained_path):
        normalized_path = os.path.normpath(retained_path)
        self._ordinary_config_delete_retained_artifacts.add(
            normalized_path
        )
        self._report_best_effort(
            "error",
            "delete_compatibility retained recovery object: "
            f"{normalized_path}",
        )

    def _delete_interrupted_restore_location(
        self,
        ops,
        owner,
        snapshot,
        artifact_handle,
        artifact_name,
        destination_handles,
        recovery_handles,
    ):
        destination_parent_handle = destination_handles[-1]
        recovery_handle = recovery_handles[-1]
        recovery_is_absent = ops.name_is_absent(
            recovery_handle,
            artifact_name,
        )
        if (
            not recovery_is_absent
            and self._delete_verify_named_identity(
                ops,
                owner,
                recovery_handle,
                artifact_name,
                snapshot,
            )
        ):
            return "recovery"

        destination_is_absent = ops.name_is_absent(
            destination_parent_handle,
            snapshot.destination_name,
        )
        if (
            recovery_is_absent
            and not destination_is_absent
            and self._delete_verify_named_identity(
                ops,
                owner,
                destination_parent_handle,
                snapshot.destination_name,
                snapshot,
            )
            and self._delete_artifact_is_intact(
                ops,
                artifact_handle,
                snapshot,
            )
        ):
            return "destination"
        return "indeterminate"

    def _delete_recover_after_restore_interruption(
        self,
        ops,
        owner,
        snapshot,
        artifact_handle,
        artifact_state,
        source_handles,
        destination_handles,
        recovery_handles,
        *,
        retry=True,
    ):
        if artifact_state.retain_only and artifact_state.path_valid:
            return _OrdinaryConfigDeleteRestoreResult(
                False,
                artifact_state.path,
                False,
            )

        try:
            interrupted_location = (
                self._delete_interrupted_restore_location(
                    ops,
                    owner,
                    snapshot,
                    artifact_handle,
                    artifact_state.name,
                    destination_handles,
                    recovery_handles,
                )
            )
        except BaseException:
            interrupted_location = "indeterminate"

        if interrupted_location == "destination":
            return _OrdinaryConfigDeleteRestoreResult(
                True,
                None,
                False,
            )
        if interrupted_location != "recovery":
            return _OrdinaryConfigDeleteRestoreResult(
                False,
                None,
                artifact_state.path_valid,
            )
        if not retry:
            return _OrdinaryConfigDeleteRestoreResult(
                False,
                artifact_state.path,
                False,
            )

        try:
            return self._delete_try_local_restore(
                ops,
                owner,
                snapshot,
                artifact_handle,
                artifact_state.name,
                source_handles,
                destination_handles,
                recovery_handles,
                artifact_state,
            )
        except BaseException:
            return self._delete_recover_after_restore_interruption(
                ops,
                owner,
                snapshot,
                artifact_handle,
                artifact_state,
                source_handles,
                destination_handles,
                recovery_handles,
                retry=False,
            )

    def _delete_ordinary_config_compatibility(
        self,
        snapshot,
        *,
        handle_ops=None,
    ):
        ops = handle_ops or WindowsHandleOps()
        if (
            not isinstance(snapshot, _OrdinaryConfigDeleteSnapshot)
            or not isinstance(
                snapshot.capability,
                _WindowsDeleteCapability,
            )
            or not snapshot.capability.relative_rename
            or not snapshot.capability.disposition
            or snapshot.capability.volume_serial
            != snapshot.recovery_identity[0]
        ):
            self._report_best_effort(
                "error",
                "delete_compatibility error: invalid_snapshot",
            )
            return False
        if not ops.is_supported():
            self._report_best_effort(
                "error",
                "delete_compatibility error: "
                f"{ops.unsupported_category or 'unsupported_api'}",
            )
            return False

        artifact_name = None
        artifact_path = None
        artifact_handle = None
        artifact_state = None
        detached = False
        root_disposition_accepted = False
        failure_category = "transaction_failed"
        propagated_error = None
        propagated_traceback = None

        owner = WindowsHandleOwner(ops.close)
        try:
            if owner:
                source_handles = self._delete_reacquire_chain(
                    ops,
                    owner,
                    snapshot.source_chain,
                )
                destination_handles = self._delete_reacquire_chain(
                    ops,
                    owner,
                    snapshot.destination_chain,
                    snapshot.destination_missing,
                )
                recovery_handles = self._delete_reacquire_chain(
                    ops,
                    owner,
                    snapshot.recovery_chain,
                )
                recovery_handle = recovery_handles[-1]
                if (
                    ops.identity(recovery_handle)
                    != snapshot.recovery_identity
                ):
                    raise WindowsDeleteFailure(
                        "recovery_directory_changed"
                    )

                config_handle = source_handles[-1]
                destination_parent_handle = destination_handles[-1]
                if snapshot.destination_missing:
                    if snapshot.destination_exists:
                        raise WindowsDeleteFailure(
                            "destination_state_changed"
                        )
                else:
                    destination_is_absent = ops.name_is_absent(
                        destination_parent_handle,
                        snapshot.destination_name,
                    )
                    if (
                        destination_is_absent
                        is snapshot.destination_exists
                    ):
                        raise WindowsDeleteFailure(
                            "destination_state_changed"
                        )

                if not snapshot.destination_exists:
                    if not ops.name_is_absent(
                        config_handle,
                        snapshot.source_name,
                    ):
                        raise WindowsDeleteFailure("source_appeared")
                    owner.close_all()
                    self._report_best_effort(
                        "info",
                        "delete_compatibility no-op: "
                        f"{snapshot.destination_path}",
                    )
                    return True

                artifact_handle = owner.acquire(
                    ops.open_relative(
                        destination_parent_handle,
                        snapshot.destination_name,
                        directory=snapshot.destination_directory,
                        delete=True,
                    )
                )
                if (
                    ops.identity(artifact_handle)
                    != snapshot.destination_identity
                    or self._delete_target_manifest(
                        ops,
                        artifact_handle,
                        directory=snapshot.destination_directory,
                    )
                    != snapshot.destination_manifest
                ):
                    raise WindowsDeleteFailure(
                        "destination_changed"
                    )

                while True:
                    candidate = (
                        f".{snapshot.destination_name}.delete-"
                        f"{uuid.uuid4().hex}"
                    )
                    self._delete_validate_before_rename_attempt(
                        ops,
                        owner,
                        snapshot,
                        artifact_handle,
                        source_handles,
                        destination_handles,
                        recovery_handles,
                    )
                    try:
                        ops.rename_relative_no_replace(
                            artifact_handle,
                            recovery_handle,
                            candidate,
                        )
                    except WindowsDeleteFailure as error:
                        if error.category == "rename_collision":
                            continue
                        raise
                    artifact_name = candidate
                    artifact_path = os.path.join(
                        snapshot.recovery_path,
                        artifact_name,
                    )
                    artifact_state = (
                        _OrdinaryConfigDeleteArtifactState(
                            artifact_name,
                            artifact_path,
                        )
                    )
                    detached = True
                    break

                if not ops.name_is_absent(
                    destination_parent_handle,
                    snapshot.destination_name,
                ):
                    raise WindowsDeleteFailure(
                        "destination_not_absent"
                    )
                if not self._delete_verify_named_identity(
                    ops,
                    owner,
                    recovery_handle,
                    artifact_name,
                    snapshot,
                ):
                    raise WindowsDeleteFailure(
                        "artifact_binding_changed"
                    )
                if (
                    ops.identity(artifact_handle)
                    != snapshot.destination_identity
                    or self._delete_target_manifest(
                        ops,
                        artifact_handle,
                        directory=snapshot.destination_directory,
                    )
                    != snapshot.destination_manifest
                ):
                    raise WindowsDeleteFailure(
                        "post_rename_manifest_changed"
                    )

                if snapshot.destination_directory:
                    ops.delete_descendants(
                        artifact_handle,
                        snapshot.destination_manifest[1],
                    )
                    if ops.enumerate(artifact_handle):
                        raise WindowsDeleteFailure(
                            "directory_not_empty"
                        )
                    if (
                        ops.identity(artifact_handle)
                        != snapshot.destination_identity
                    ):
                        raise WindowsDeleteFailure(
                            "artifact_identity_changed"
                        )
                elif (
                    ops.manifest_entry(artifact_handle)
                    != snapshot.destination_manifest[0]
                ):
                    raise WindowsDeleteFailure(
                        "artifact_manifest_changed"
                    )

                ops.set_delete_disposition(artifact_handle)
                root_disposition_accepted = True
                ops.close(artifact_handle)
                owner.release(artifact_handle)
                artifact_handle = None

                if not ops.name_is_absent(
                    destination_parent_handle,
                    snapshot.destination_name,
                ):
                    raise WindowsDeleteFailure(
                        "destination_not_absent"
                    )
                if not ops.name_is_absent(
                    recovery_handle,
                    artifact_name,
                ):
                    raise WindowsDeleteFailure(
                        "artifact_not_absent"
                    )
                self._ordinary_config_delete_retained_artifacts.discard(
                    os.path.normpath(artifact_path)
                )
                detached = False
                artifact_path = None
                owner.close_all()
                self._report_best_effort(
                    "info",
                    "delete_compatibility success: "
                    f"{snapshot.destination_path}",
                )
                return True
        except BaseException as error:
            if isinstance(error, WindowsDeleteFailure):
                failure_category = error.category
            else:
                failure_category = "transaction_failed"
                if not isinstance(
                    error,
                    (OSError, TypeError, ValueError),
                ):
                    propagated_error = error
                    propagated_traceback = error.__traceback__
            restore_result = _OrdinaryConfigDeleteRestoreResult(
                False,
                None,
            )
            if (
                detached
                and root_disposition_accepted
                and artifact_name is not None
            ):
                try:
                    artifact_path_is_valid = not ops.name_is_absent(
                        recovery_handle,
                        artifact_name,
                    )
                except BaseException:
                    artifact_path_is_valid = False
                if not artifact_path_is_valid:
                    detached = False
                    artifact_path = None
            if (
                detached
                and not root_disposition_accepted
                and artifact_handle is not None
            ):
                try:
                    restore_result = self._delete_try_local_restore(
                        ops,
                        owner,
                        snapshot,
                        artifact_handle,
                        artifact_name,
                        source_handles,
                        destination_handles,
                        recovery_handles,
                        artifact_state,
                    )
                except BaseException as recovery_error:
                    if (
                        propagated_error is None
                        or (
                            not isinstance(recovery_error, Exception)
                            and isinstance(propagated_error, Exception)
                        )
                    ):
                        propagated_error = recovery_error
                        propagated_traceback = (
                            recovery_error.__traceback__
                        )
                    restore_result = (
                        self._delete_recover_after_restore_interruption(
                            ops,
                            owner,
                            snapshot,
                            artifact_handle,
                            artifact_state,
                            source_handles,
                            destination_handles,
                            recovery_handles,
                        )
                    )
            if restore_result.restored:
                detached = False
                artifact_path = None
            elif restore_result.retained_path is not None:
                self._delete_retain_location(
                    restore_result.retained_path
                )
                detached = False
                artifact_path = None
            elif not restore_result.previous_artifact_path_valid:
                detached = False
                artifact_path = None
            elif detached and artifact_path is not None:
                self._delete_retain_location(artifact_path)
        finally:
            try:
                owner.close_all()
            except BaseException as close_error:
                if isinstance(close_error, WindowsDeleteFailure):
                    failure_category = close_error.category
                elif propagated_error is None:
                    propagated_error = close_error
                    propagated_traceback = close_error.__traceback__

        if propagated_error is not None:
            raise propagated_error.with_traceback(propagated_traceback)

        self._report_best_effort(
            "error",
            f"delete_compatibility error: {failure_category}",
        )
        return False

    def backup_config_file(self, config_file_source_path):
        managed_roots = (
            (
                AI_MODEL_CONFIG_PATH,
                BACKUP_AI_MODEL_CONFIG_PATH,
                "ai_model_config_flag",
            ),
            (
                UI_CONFIG_PATH,
                BACKUP_UI_CONFIG_PATH,
                "ui_config_flag",
            ),
            (
                UI_PIC_PATH,
                BACKUP_UI_PIC_PATH,
                "ui_pic_flag",
            ),
            (
                SCANNER_BARCODE_CONFIG_PATH,
                BACKUP_SCANNER_BARCODE_CONFIG_PATH,
                "scannre_barcode_config_flag",
            ),
        )

        for file_path in config_file_source_path:
            file_path, path_error = self._resolve_ordinary_config_destination(
                file_path
            )
            if path_error is not None:
                self._report_ordinary_config_destination_error(path_error)
                return False
            managed_root = next(
                (
                    (backup_source_path, backup_path, flag_name)
                    for backup_source_path, backup_path, flag_name in managed_roots
                    if self.is_parent_path_os(backup_source_path, file_path)
                ),
                None,
            )
            if managed_root is None:
                message = f"配置文件不在可备份目录中: {file_path}"
                self._report_best_effort("error", message)
                return False

            backup_source_path, backup_path, flag_name = managed_root
            if getattr(self, flag_name) is False:
                continue

            if not self._snapshot_ancestor_chain_is_safe(
                backup_source_path,
                DEFAULT_TARGET_DIR,
            ):
                message = (
                    "配置文件备份安全检查失败: "
                    f"{backup_source_path}"
                )
                self._report_best_effort("error", message)
                return False
            if not self._snapshot_ancestor_chain_is_safe(
                os.path.dirname(os.path.normpath(backup_path)),
                DEFAULT_DIR,
            ):
                message = (
                    "配置文件备份目标安全检查失败: "
                    f"{backup_path}"
                )
                self._report_best_effort("error", message)
                return False
            if not self._snapshot_path_is_safe(backup_source_path):
                message = (
                    "配置文件备份安全检查失败: "
                    f"{backup_source_path}"
                )
                self._report_best_effort("error", message)
                return False

            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                if not self._replace_with_staged_snapshot(
                    backup_source_path,
                    backup_path,
                ):
                    message = f"配置文件备份失败: {backup_source_path} -> {backup_path}"
                    self._report_best_effort("error", message)
                    return False
            except Exception as e:
                self._report_best_effort(
                    "error",
                    f"备份配置文件时发生错误：{e}",
                )
                return False

            setattr(self, flag_name, False)
            self._report_best_effort(
                "info",
                f"配置文件备份已保存到 {backup_path}",
            )
        return True

    def _normalize_json_merge_entries(self, raw_entries):
        if not isinstance(raw_entries, list):
            return None, "section_must_be_list"

        normalized_entries = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                return None, "entry_must_be_object"
            if set(entry) != {"target", "source"}:
                return None, "entry_fields_invalid"

            target = entry["target"]
            if not isinstance(target, str) or not target.strip():
                return None, "target_must_be_non_empty_string"

            source = entry["source"]
            if not isinstance(source, str) or not source.strip():
                return None, "source_must_be_non_empty_string"

            normalized_entries.append(
                {
                    "target": target,
                    "source": source,
                }
            )

        return normalized_entries, None

    def _relative_json_merge_path_error(self, relative_path):
        if "\x00" in relative_path:
            return "invalid_path"
        if os.path.isabs(relative_path) or os.path.splitdrive(relative_path)[0]:
            return "absolute_path"

        path_components = relative_path.replace("\\", "/").split("/")
        if ".." in path_components:
            return "parent_reference"
        return None

    def _path_is_contained(self, path, root):
        try:
            normalized_path = os.path.normcase(os.path.abspath(path))
            normalized_root = os.path.normcase(os.path.abspath(root))
            return (
                os.path.commonpath([normalized_root, normalized_path])
                == normalized_root
            )
        except (OSError, ValueError):
            return False

    def _resolve_json_merge_target(self, relative_path):
        path_error = self._relative_json_merge_path_error(relative_path)
        if path_error is not None:
            return None, path_error

        try:
            lexical_path = os.path.abspath(
                os.path.join(DEFAULT_TARGET_DIR, relative_path)
            )
            resolved_path = os.path.realpath(lexical_path)
        except (OSError, ValueError):
            return None, "invalid_path"

        allowed_roots = (
            UI_CONFIG_PATH,
            AI_MODEL_CONFIG_PATH,
            SCANNER_BARCODE_CONFIG_PATH,
        )
        for allowed_root in allowed_roots:
            lexical_root = os.path.abspath(allowed_root)
            if not self._path_is_contained(lexical_path, lexical_root):
                continue

            try:
                resolved_root = os.path.realpath(lexical_root)
            except (OSError, ValueError):
                return None, "invalid_path"
            if not self._path_is_contained(resolved_path, resolved_root):
                return None, "symlink_escape"
            try:
                if not os.path.isfile(lexical_path):
                    return None, "missing_file"
            except OSError:
                return None, "missing_file"
            return lexical_path, None

        return None, "outside_allowed_root"

    def _resolve_json_merge_source(self, relative_path):
        path_error = self._relative_json_merge_path_error(relative_path)
        if path_error is not None:
            return None, path_error

        try:
            lexical_path = os.path.abspath(os.path.join(DEFAULT_DIR, relative_path))
            lexical_root = os.path.abspath(CONFIG_DIR)
            resolved_path = os.path.realpath(lexical_path)
            resolved_root = os.path.realpath(lexical_root)
        except (OSError, ValueError):
            return None, "invalid_path"

        if not self._path_is_contained(lexical_path, lexical_root):
            return None, "outside_allowed_root"
        if not self._path_is_contained(resolved_path, resolved_root):
            return None, "symlink_escape"
        try:
            if not os.path.isfile(lexical_path):
                return None, "missing_file"
        except OSError:
            return None, "missing_file"
        return lexical_path, None

    def _json_merge_manifest_path_key(self, relative_path):
        if not isinstance(relative_path, str) or not relative_path.strip():
            return None
        if self._relative_json_merge_path_error(relative_path) is not None:
            return None
        try:
            return os.path.normcase(
                os.path.normpath(
                    os.path.abspath(
                        os.path.join(DEFAULT_TARGET_DIR, relative_path)
                    )
                )
            )
        except (OSError, ValueError):
            return None

    def _destination_identity_key(self, path, path_exists):
        try:
            absolute_path = os.path.abspath(path)
            if path_exists:
                resolved_path = os.path.realpath(absolute_path)
            else:
                resolved_parent = os.path.realpath(
                    os.path.dirname(absolute_path)
                )
                resolved_path = os.path.join(
                    resolved_parent,
                    os.path.basename(absolute_path),
                )
            return os.path.normcase(os.path.normpath(resolved_path)), None
        except (OSError, ValueError):
            return None, "identity_check_failed"

    def _destination_paths_are_aliases(self, first_path, second_path):
        first_exists, first_state_error = self._destination_entry_state(first_path)
        if first_state_error is not None:
            return False, first_state_error
        second_exists, second_state_error = self._destination_entry_state(second_path)
        if second_state_error is not None:
            return False, second_state_error

        try:
            if first_exists and second_exists:
                if os.path.samefile(first_path, second_path):
                    return True, None
        except (OSError, ValueError):
            return False, "identity_check_failed"

        first_key, first_key_error = self._destination_identity_key(
            first_path,
            first_exists,
        )
        if first_key_error is not None:
            return False, first_key_error
        second_key, second_key_error = self._destination_identity_key(
            second_path,
            second_exists,
        )
        if second_key_error is not None:
            return False, second_key_error
        return (
            first_key is not None
            and second_key is not None
            and first_key == second_key
        ), None

    def _destination_paths_are_related(self, first_path, second_path):
        paths_are_aliases, identity_error = (
            self._destination_paths_are_aliases(
                first_path,
                second_path,
            )
        )
        if identity_error is not None or paths_are_aliases:
            return paths_are_aliases, identity_error

        first_exists, first_state_error = self._destination_entry_state(
            first_path
        )
        if first_state_error is not None:
            return False, first_state_error
        second_exists, second_state_error = self._destination_entry_state(
            second_path
        )
        if second_state_error is not None:
            return False, second_state_error
        first_key, first_key_error = self._destination_identity_key(
            first_path,
            first_exists,
        )
        if first_key_error is not None:
            return False, first_key_error
        second_key, second_key_error = self._destination_identity_key(
            second_path,
            second_exists,
        )
        if second_key_error is not None:
            return False, second_key_error

        try:
            common_path = os.path.normcase(
                os.path.normpath(
                    os.path.commonpath([first_key, second_key])
                )
            )
        except (OSError, TypeError, ValueError):
            return False, "identity_check_failed"
        return common_path in (first_key, second_key), None

    def _preflight_ordinary_config_delete_conflicts(self, operations):
        if not any(
            operation["kind"] == "delete"
            for operation in operations
        ):
            return True

        for first_index, first_operation in enumerate(operations):
            for second_operation in operations[first_index + 1 :]:
                paths_are_related, identity_error = (
                    self._destination_paths_are_related(
                        first_operation["destination_path"],
                        second_operation["destination_path"],
                    )
                )
                if identity_error is not None:
                    self._report_best_effort(
                        "error",
                        "delete_compatibility:"
                        "target_identity_check_failed",
                    )
                    return False
                if paths_are_related:
                    self._report_best_effort(
                        "error",
                        "delete_compatibility:target_conflict",
                    )
                    return False
        return True

    def _report_json_merge(self, level, message):
        print(message)
        getattr(self.logger, level)(message)

    def _preflight_json_merge_conflicts(self, entries, config_operations):
        for entry in entries:
            target_path = self._json_merge_manifest_path_key(entry["target"])
            if target_path is None:
                continue
            for operation in config_operations:
                if operation["kind"] == "delete":
                    paths_conflict, identity_error = (
                        self._destination_paths_are_related(
                            target_path,
                            operation["destination_path"],
                        )
                    )
                else:
                    paths_conflict, identity_error = (
                        self._destination_paths_are_aliases(
                            target_path,
                            operation["destination_path"],
                        )
                    )
                if identity_error is not None:
                    error_category = (
                        "destination_alias_dangling_link"
                        if identity_error == "dangling_link"
                        else "destination_identity_check_failed"
                    )
                    self._report_json_merge(
                        "error",
                        f"json_merge error: {error_category}",
                    )
                    return False
                if not paths_conflict:
                    continue
                if operation["kind"] == "delete":
                    self._report_best_effort(
                        "error",
                        "delete_compatibility:json_merge_conflict",
                    )
                    self._report_json_merge(
                        "error",
                        "json_merge error: target_conflicts_with_config",
                    )
                else:
                    self._report_json_merge(
                        "error",
                        "json_merge error: target_conflicts_with_config",
                    )
                return False
        return True

    def _apply_json_merges(self, entries):
        for entry in entries:
            target_relative_path = entry["target"]
            source_relative_path = entry["source"]

            target_path, target_error = self._resolve_json_merge_target(
                target_relative_path
            )
            if target_error is not None:
                self._report_json_merge(
                    "error",
                    f"json_merge error: target_path:{target_error}",
                )
                return False

            source_path, source_error = self._resolve_json_merge_source(
                source_relative_path
            )
            if source_error is not None:
                self._report_json_merge(
                    "error",
                    f"json_merge error: source_path:{source_error}",
                )
                return False

            try:
                paths_are_aliases = os.path.samefile(target_path, source_path)
            except OSError:
                self._report_json_merge(
                    "error",
                    "json_merge error: target_source_alias_check_failed",
                )
                return False
            if paths_are_aliases:
                self._report_json_merge(
                    "error",
                    "json_merge error: target_source_alias",
                )
                return False

            self._report_json_merge(
                "info",
                "json_merge start: "
                f"target={target_relative_path} source={source_relative_path}",
            )
            if not self.backup_config_file([target_relative_path]):
                self._report_json_merge(
                    "error",
                    "json_merge error: backup_failed",
                )
                return False

            merged, reason = merge_json_files_with_reason(target_path, source_path)
            if not merged:
                self._report_json_merge(
                    "error",
                    f"json_merge error: merge_failed:{reason}",
                )
                return False

            self._report_json_merge(
                "info",
                "json_merge success: "
                f"target={target_relative_path} source={source_relative_path}",
            )

        return True

    def update_config_file(self):
        self._report_best_effort("info", "正在更新配置文件...")
        try:
            for upgrade_version in self.upgrade_version_list:
                target_version_path = DEFAULT_DIR + f"resources/version_{upgrade_version}.yml"
                data = Readyml.load_yaml(target_version_path)
                config_file_source_path = data["config"]

                json_merge_entries, schema_error = (
                    self._normalize_json_merge_entries(
                        data.get("json_merge", [])
                    )
                )
                if schema_error is not None:
                    self._report_json_merge(
                        "error",
                        f"json_merge schema error: {schema_error}",
                    )
                    return False

                config_operations = []
                for config_file_path in config_file_source_path:
                    target_file_path, path_error = (
                        self._resolve_ordinary_config_destination(
                            config_file_path
                        )
                    )
                    if path_error is not None:
                        if path_error == "symlink_escape":
                            lexical_target = os.path.abspath(
                                os.path.join(
                                    DEFAULT_TARGET_DIR,
                                    config_file_path,
                                )
                            )
                            (
                                _delete_source_path,
                                delete_source_error,
                            ) = self._resolve_ordinary_config_source(
                                lexical_target
                            )
                            if delete_source_error == "missing_path":
                                try:
                                    lexical_target_stat = os.lstat(
                                        lexical_target
                                    )
                                except (
                                    OSError,
                                    TypeError,
                                    ValueError,
                                ):
                                    lexical_target_stat = None
                                if (
                                    lexical_target_stat is not None
                                    and self._snapshot_entry_is_link_or_reparse(
                                        lexical_target_stat
                                    )
                                ):
                                    self._report_best_effort(
                                        "error",
                                        "delete_compatibility:"
                                        "unsupported_destination_reparse",
                                    )
                                    return False
                        self._report_ordinary_config_destination_error(
                            path_error
                        )
                        return False
                    new_config_file_path, source_error = (
                        self._resolve_ordinary_config_source(
                            target_file_path
                        )
                    )
                    if source_error == "missing_path":
                        config_operations.append(
                            {
                                "kind": "delete",
                                "relative_path": config_file_path,
                                "destination_path": target_file_path,
                                "source_path": new_config_file_path,
                            }
                        )
                        continue
                    if source_error is not None:
                        self._report_ordinary_config_source_error(
                            source_error
                        )
                        return False
                    config_operations.append(
                        {
                            "kind": "replace",
                            "relative_path": config_file_path,
                            "destination_path": target_file_path,
                            "source_path": new_config_file_path,
                        }
                    )

                if not self._preflight_ordinary_config_delete_conflicts(
                    config_operations
                ):
                    return False
                if not self._preflight_json_merge_conflicts(
                    json_merge_entries,
                    config_operations,
                ):
                    return False

                for operation in config_operations:
                    if operation["kind"] != "delete":
                        continue
                    snapshot, delete_error = (
                        self._preflight_ordinary_config_delete(
                            operation["destination_path"],
                            operation["source_path"],
                        )
                    )
                    if delete_error is not None:
                        self._report_best_effort(
                            "error",
                            "delete_compatibility:"
                            f"{delete_error}",
                        )
                        return False
                    operation["snapshot"] = snapshot

                for operation in config_operations:
                    if operation["kind"] == "delete":
                        self._report_best_effort(
                            "info",
                            "delete_compatibility selected: "
                            f"{operation['relative_path']}",
                        )

                backup_paths = [
                    operation["relative_path"]
                    for operation in config_operations
                    if operation["kind"] == "replace"
                    or operation["snapshot"].destination_exists
                    or operation["snapshot"].managed_root_exists
                ]
                if backup_paths and not self.backup_config_file(
                    backup_paths
                ):
                    self._report_best_effort(
                        "error",
                        "配置文件备份错误",
                    )
                    return False

                for operation in config_operations:
                    config_file_path = operation["relative_path"]
                    target_file_path = operation["destination_path"]
                    if operation["kind"] == "delete":
                        if not self._delete_ordinary_config_compatibility(
                            operation["snapshot"]
                        ):
                            return False
                        continue

                    self._report_best_effort(
                        "info",
                        f"更新配置文件: {config_file_path}",
                    )
                    (
                        new_config_file_path,
                        source_error,
                    ) = self._resolve_ordinary_config_source(
                        target_file_path
                    )
                    if source_error is not None:
                        self._report_ordinary_config_source_error(
                            source_error
                        )
                        return False
                    if not self._replace_with_staged_ordinary_config(
                        new_config_file_path,
                        target_file_path,
                    ):
                        self._report_best_effort(
                            "error",
                            "配置文件升级失败",
                        )
                        return False
                    self._report_best_effort(
                        "info",
                        "配置文件升级成功",
                    )

                if not self._apply_json_merges(json_merge_entries):
                    return False
            return True
        except Exception as e:
            self._report_best_effort(
                "error",
                f"配置文件升级失败: {e}",
            )
            return False

    def restore_config(self):
        restore_targets = (
            (
                "ui_pic_flag",
                BACKUP_UI_PIC_PATH,
                UI_PIC_PATH,
                "ui图片配置文件",
            ),
            (
                "ai_model_config_flag",
                BACKUP_AI_MODEL_CONFIG_PATH,
                AI_MODEL_CONFIG_PATH,
                "ai模型配置文件",
            ),
            (
                "scannre_barcode_config_flag",
                BACKUP_SCANNER_BARCODE_CONFIG_PATH,
                SCANNER_BARCODE_CONFIG_PATH,
                "条码扫描配置文件",
            ),
            (
                "ui_config_flag",
                BACKUP_UI_CONFIG_PATH,
                UI_CONFIG_PATH,
                "UI配置文件",
            ),
        )
        restore_succeeded = True

        for flag_name, backup_path, target_path, display_name in restore_targets:
            if getattr(self, flag_name) is not False:
                continue

            try:
                copy_succeeded = self._replace_with_staged_snapshot(
                    backup_path,
                    target_path,
                )
            except Exception as e:
                copy_succeeded = False
                self._report_best_effort(
                    "error",
                    f"{display_name}恢复失败: {e}",
                )

            if copy_succeeded:
                self._report_best_effort(
                    "info",
                    f"{display_name}已恢复",
                )
            else:
                restore_succeeded = False
                message = f"{display_name}恢复失败: {backup_path} -> {target_path}"
                self._report_best_effort("error", message)

        if not restore_succeeded:
            self._report_best_effort(
                "error",
                "配置文件恢复失败，保留备份文件",
            )
            self._report_ordinary_config_manual_recovery_paths(
                restore_targets
            )
            return False

        self._report_best_effort("info", "配置文件已恢复")
        cleanup_succeeded = self.delete_backup_file()
        if not cleanup_succeeded:
            self._report_ordinary_config_manual_recovery_paths(
                restore_targets
            )
        return cleanup_succeeded

    def _report_ordinary_config_manual_recovery_paths(
        self,
        restore_targets,
    ):
        retained_paths = set(
            self._ordinary_config_delete_retained_artifacts
        )
        for flag_name, backup_path, _target_path, _display_name in (
            restore_targets
        ):
            if getattr(self, flag_name) is False:
                try:
                    if os.path.lexists(backup_path):
                        retained_paths.add(os.path.normpath(backup_path))
                except (OSError, TypeError, ValueError):
                    retained_paths.add(os.path.normpath(backup_path))

        for retained_path in sorted(retained_paths):
            self._report_best_effort(
                "error",
                "delete_compatibility manual recovery retained: "
                f"{retained_path}",
            )

    def delete_backup_file(self):
        self._report_best_effort("info", "正在删除备份文件...")
        backup_targets = (
            ("ui_pic_flag", BACKUP_UI_PIC_PATH, "ui图片配置备份文件"),
            (
                "ai_model_config_flag",
                BACKUP_AI_MODEL_CONFIG_PATH,
                "ai模型配置备份文件",
            ),
            (
                "scannre_barcode_config_flag",
                BACKUP_SCANNER_BARCODE_CONFIG_PATH,
                "条码扫描配置备份文件",
            ),
            ("ui_config_flag", BACKUP_UI_CONFIG_PATH, "UI配置备份文件"),
        )
        delete_succeeded = True

        for flag_name, backup_path, display_name in backup_targets:
            if getattr(self, flag_name) is not False:
                continue

            try:
                removed = self.smart_remove(backup_path)
            except Exception as e:
                removed = False
                self._report_best_effort(
                    "error",
                    f"{display_name}删除失败: {e}",
                )

            if removed:
                self._report_best_effort(
                    "info",
                    f"{display_name}已删除",
                )
            else:
                delete_succeeded = False
                message = f"{display_name}删除失败，保留备份: {backup_path}"
                self._report_best_effort("error", message)

        return delete_succeeded


if __name__ == "__main__":
    from log_model.log_manager import LogManager

    log_manager = LogManager()
    config_manager = ConfigManager(log_manager, "0.25.06.01")
    config_manager.update_config_file()
