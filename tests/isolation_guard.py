"""Reusable fail-closed audit policy for pytest and inherited Python children."""
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

DATA_KEYS = {
    'SAM_DATA_ROOT', 'SAM_MAIN_DB', 'SAM_APP_DB', 'SAM_2A_ROOT',
    'SAM_MEMORY_ROOT', 'SAM_OBJECTS_DIR', 'SAM_L2_IMAGE_ROOT',
    'SAM_OPENCLAW_STATE_DIR', 'SAM_OPENCLAW_CONFIG',
}


def data_keys(env):
    return DATA_KEYS | {key for key in env if key.startswith('SAM_') and key.endswith('DB')}


def self_check(env, root):
    root = Path(root).resolve()
    for key in data_keys(env):
        value = env.get(key)
        if not value or not Path(value).resolve().is_relative_to(root):
            raise RuntimeError(f'pytest isolation self-check failed: {key}')


def install(protected, temporary):
    import sys
    protected = tuple(Path(p).resolve() for p in protected)
    temporary = Path(temporary).resolve()

    def denied(path, dir_fd=None, follow=True):
        if isinstance(path, int):
            try:
                path = os.readlink(f'/proc/self/fd/{path}')
            except OSError:
                return True
            if path.startswith(('pipe:', 'socket:', 'anon_inode:')):
                return False
        if not isinstance(path, (str, bytes, os.PathLike)):
            return True
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if path in {':memory:', '/dev/null'}:
            return False
        try:
            candidate = Path(path)
            if not candidate.is_absolute() and dir_fd not in {None, -1}:
                candidate = Path(os.readlink(f'/proc/self/fd/{dir_fd}')) / candidate
            resolved = candidate.resolve() if follow else candidate.parent.resolve() / candidate.name
            return (any(resolved == p or resolved.is_relative_to(p) for p in protected)
                    or not resolved.is_relative_to(temporary))
        except (OSError, ValueError):
            return True

    def audit(event, args):
        if event == 'socket.connect' and len(args) > 1:
            address = args[1]
            if isinstance(address, tuple) and len(address) > 1 and address[1] == 18790:
                raise PermissionError('pytest refused production gateway port 18790')
        paths = ()
        if event == 'sqlite3.connect' and args:
            path = str(args[0])
            if path.startswith('file:'):
                uri = urlsplit(path)
                if parse_qs(uri.query).get('mode') == ['ro']:
                    return
                path = unquote(uri.path)
            paths = (path,)
        elif event == 'open' and len(args) >= 3:
            if not (args[2] or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                return
            paths = (args[0],)
        elif event in {'os.remove', 'os.rmdir'}:
            if denied(args[0], args[1], follow=False):
                raise PermissionError('pytest refused write outside isolated temporary storage')
            return
        elif event == 'os.symlink':
            # The source is read, not written; only the new directory entry is writable.
            if denied(args[1], args[2], follow=False):
                raise PermissionError('pytest refused write outside isolated temporary storage')
            return
        elif event in {'os.rename', 'os.link'}:
            if any(denied(args[i], args[i + 2], follow=False) for i in (0, 1)):
                raise PermissionError('pytest refused write outside isolated temporary storage')
            return
        elif event in {'os.mkdir', 'os.chmod', 'os.chown'}:
            fd_index = 3 if event == 'os.chown' else 2
            if denied(args[0], args[fd_index] if len(args) > fd_index else None):
                raise PermissionError('pytest refused write outside isolated temporary storage')
            return
        elif event == 'os.utime':
            if denied(args[0], args[3]):
                raise PermissionError('pytest refused write outside isolated temporary storage')
            return
        elif event == 'os.truncate':
            paths = args[:1]
        if any(denied(path) for path in paths):
            raise PermissionError('pytest refused write outside isolated temporary storage')
    sys.addaudithook(audit)
