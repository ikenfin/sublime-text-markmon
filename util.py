# coding=utf8
#
# util.py
# Part of SublimeLinter3, a code checking framework for Sublime Text 3
#
# Written by Ryan Hileman and Aparajita Fishman
#
# Project: https://github.com/SublimeLinter/SublimeLinter3
# License: MIT
#
# Extracted from SublimeLinter3, for env stuff

"""This module provides general utility methods."""

from functools import lru_cache
from glob import glob
import json
from numbers import Number
import os
import re
import shutil
from string import Template
import sublime
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

#
# Public constants
#
STREAM_STDOUT = 1
STREAM_STDERR = 2
STREAM_BOTH = STREAM_STDOUT + STREAM_STDERR

PYTHON_CMD_RE = re.compile(r'(?P<script>[^@]+)?@python(?P<version>[\d\.]+)?')
VERSION_RE = re.compile(r'(?P<major>\d+)(?:\.(?P<minor>\d+))?')

INLINE_SETTINGS_RE = re.compile(r'(?i).*?\[sublimelinter[ ]+(?P<settings>[^\]]+)\]')
INLINE_SETTING_RE = re.compile(r'(?P<key>[@\w][\w\-]*)\s*:\s*(?P<value>[^\s]+)')

MENU_INDENT_RE = re.compile(r'^(\s+)\$menus', re.MULTILINE)

MARK_COLOR_RE = (
    r'(\s*<string>sublimelinter\.{}</string>\s*\r?\n'
    r'\s*<key>settings</key>\s*\r?\n'
    r'\s*<dict>\s*\r?\n'
    r'\s*<key>foreground</key>\s*\r?\n'
    r'\s*<string>)#.+?(</string>\s*\r?\n)'
)

ANSI_COLOR_RE = re.compile(r'\033\[[0-9;]*m')

# file/directory/environment utils

def climb(start_dir, limit=None):
    """
    Generate directories, starting from start_dir.

    If limit is None or <= 0, stop at the root directory.
    Otherwise return a maximum of limit directories.

    """

    right = True

    while right and (limit is None or limit > 0):
        yield start_dir
        start_dir, right = os.path.split(start_dir)

        if limit is not None:
            limit -= 1


def find_file(start_dir, name, parent=False, limit=None, aux_dirs=[]):
    """
    Find the given file by searching up the file hierarchy from start_dir.

    If the file is found and parent is False, returns the path to the file.
    If parent is True the path to the file's parent directory is returned.

    If limit is None or <= 0, the search will continue up to the root directory.
    Otherwise a maximum of limit directories will be checked.

    If aux_dirs is not empty and the file hierarchy search failed,
    those directories are also checked.

    """

    for d in climb(start_dir, limit=limit):
        target = os.path.join(d, name)

        if os.path.exists(target):
            if parent:
                return d

            return target

    for d in aux_dirs:
        d = os.path.expanduser(d)
        target = os.path.join(d, name)

        if os.path.exists(target):
            if parent:
                return d

            return target


def run_shell_cmd(cmd):
    """Run a shell command and return stdout."""
    proc = popen(cmd, env=os.environ)
    try:
        timeout = 10
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = b''

    return out


def extract_path(cmd, delim=':'):
    """Return the user's PATH as a colon-delimited list."""

    out = run_shell_cmd(cmd).decode()
    path = out.split('__SUBL_PATH__', 2)

    if len(path) > 1:
        path = path[1]
        return ':'.join(path.strip().split(delim))
    else:
        sublime.error_message(
            'SublimeLinter could not determine your shell PATH. '
            'It is unlikely that any linters will work. '
            '\n\n'
            'Please see the troubleshooting guide for info on how to debug PATH problems.')
        return ''


def get_shell_path(env):
    """
    Return the user's shell PATH using shell --login.

    This method is only used on Posix systems.

    """

    if 'SHELL' in env:
        shell_path = env['SHELL']
        shell = os.path.basename(shell_path)

        # We have to delimit the PATH output with markers because
        # text might be output during shell startup.
        if shell in ('bash', 'zsh'):
            return extract_path(
                (shell_path, '-l', '-c', 'echo "__SUBL_PATH__${PATH}__SUBL_PATH__"')
            )
        elif shell == 'fish':
            return extract_path(
                (shell_path, '-l', '-c', 'echo "__SUBL_PATH__"; for p in $PATH; echo $p; end; echo "__SUBL_PATH__"'),
                '\n'
            )
        else:
            pass
    # guess PATH if we haven't returned yet
    split = env['PATH'].split(':')
    p = env['PATH']

    for path in (
        '/usr/bin', '/usr/local/bin',
        '/usr/local/php/bin', '/usr/local/php5/bin'
    ):
        if path not in split:
            p += (':' + path)

    return p


@lru_cache(maxsize=None)
def get_environment_variable(name):
    """Return the value of the given environment variable, or None if not found."""

    if os.name == 'posix':
        value = None

        if 'SHELL' in os.environ:
            shell_path = os.environ['SHELL']

            # We have to delimit the output with markers because
            # text might be output during shell startup.
            out = run_shell_cmd((shell_path, '-l', '-c', 'echo "__SUBL_VAR__${{{}}}__SUBL_VAR__"'.format(name))).strip()

            if out:
                value = out.decode().split('__SUBL_VAR__', 2)[1].strip() or None
    else:
        value = os.environ.get(name, None)

    return value


def get_path_components(path):
    """Split a file path into its components and return the list of components."""
    components = []

    while path:
        head, tail = os.path.split(path)

        if tail:
            components.insert(0, tail)

        if head:
            if head == os.path.sep or head == os.path.altsep:
                components.insert(0, head)
                break

            path = head
        else:
            break

    return components


def packages_relative_path(path, prefix_packages=True):
    """
    Return a Packages-relative version of path with '/' as the path separator.

    Sublime Text wants Packages-relative paths used in settings and in the plugin API
    to use '/' as the path separator on all platforms. This method converts platform
    path separators to '/'. If insert_packages = True, 'Packages' is prefixed to the
    converted path.

    """

    components = get_path_components(path)

    if prefix_packages and components and components[0] != 'Packages':
        components.insert(0, 'Packages')

    return '/'.join(components)


@lru_cache(maxsize=None)
def create_environment():
    """
    Return a dict with os.environ augmented with a better PATH.

    On Posix systems, the user's shell PATH is added to PATH.

    Platforms paths are then added to PATH by getting the
    "paths" user settings for the current platform. If "paths"
    has a "*" item, it is added to PATH on all platforms.

    """

    env = {}
    env.update(os.environ)

    if os.name == 'posix':
        env['PATH'] = get_shell_path(os.environ)

    paths = {}

    if sublime.platform() in paths:
        paths = convert_type(paths[sublime.platform()], [])
    else:
        paths = []

    if paths:
        env['PATH'] = os.pathsep.join(paths) + os.pathsep + env['PATH']

    pandoc = sublime.load_settings(
        'sublime-text-markmon.sublime-settings').get("pandoc_path", None)
    if pandoc:
        env['PATH'] = pandoc + os.pathsep + env['PATH']

    # Many linters use stdin, and we convert text to utf-8
    # before sending to stdin, so we have to make sure stdin
    # in the target executable is looking for utf-8.
    env['PYTHONIOENCODING'] = 'utf8'

    return env


def can_exec(path):
    """Return whether the given path is a file and is executable."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


@lru_cache(maxsize=None)
def which(cmd, module=None):
    """
    Return the full path to the given command, or None if not found.

    If cmd is in the form [script]@python[version], find_python is
    called to locate the appropriate version of python. The result
    is a tuple of the full python path and the full path to the script
    (or None if there is no script).

    """

    match = PYTHON_CMD_RE.match(cmd)

    if match:
        args = match.groupdict()
        args['module'] = module
        return find_python(**args)[0:2]
    else:
        return find_executable(cmd)


def extract_major_minor_version(version):
    """Extract and return major and minor versions from a string version."""

    match = VERSION_RE.match(version)

    if match:
        return {key: int(value) if value is not None else None for key, value in match.groupdict().items()}
    else:
        return {'major': None, 'minor': None}


@lru_cache(maxsize=None)
def get_python_version(path):
    """Return a dict with the major/minor version of the python at path."""

    try:
        # Different python versions use different output streams, so check both
        output = communicate((path, '-V'), '', output_stream=STREAM_BOTH)

        # 'python -V' returns 'Python <version>', extract the version number
        return extract_major_minor_version(output.split(' ')[1])
    except Exception as ex:
        return {'major': None, 'minor': None}


@lru_cache(maxsize=None)
def find_python(version=None, script=None, module=None):
    """
    Return the path to and version of python and an optional related script.

    If not None, version should be a string/numeric version of python to locate, e.g.
    '3' or '3.3'. Only major/minor versions are examined. This method then does
    its best to locate a version of python that satisfies the requested version.
    If module is not None, Sublime Text's python version is tested against the
    requested version.

    If version is None, the path to the default system python is used, unless
    module is not None, in which case '<builtin>' is returned.

    If not None, script should be the name of a python script that is typically
    installed with easy_install or pip, e.g. 'pep8' or 'pyflakes'.

    A tuple of the python path, script path, major version, minor version is returned.

    """

    path = None
    script_path = None

    requested_version = {'major': None, 'minor': None}

    if module is None:
        available_version = {'major': None, 'minor': None}
    else:
        available_version = {
            'major': sys.version_info.major,
            'minor': sys.version_info.minor
        }

    if version is None:
        # If no specific version is requested and we have a module,
        # assume the linter will run using ST's python.
        if module is not None:
            result = ('<builtin>', script, available_version['major'], available_version['minor'])
            return result

        # No version was specified, get the default python
        path = find_executable('python')
    else:
        version = str(version)
        requested_version = extract_major_minor_version(version)

        # If there is no module, we will use a system python.
        # If there is a module, a specific version was requested,
        # and the builtin version does not fulfill the request,
        # use the system python.
        if module is None:
            need_system_python = True
        else:
            need_system_python = not version_fulfills_request(available_version, requested_version)
            path = '<builtin>'

        if need_system_python:
            if sublime.platform() in ('osx', 'linux'):
                path = find_posix_python(version)
            else:
                path = find_windows_python(version)


    if path and path != '<builtin>':
        available_version = get_python_version(path)

        if version_fulfills_request(available_version, requested_version):
            if script:
                script_path = find_python_script(path, script)

                if script_path is None:
                    path = None
        else:
            path = script_path = None

    result = (path, script_path, available_version['major'], available_version['minor'])
    return result


def version_fulfills_request(available_version, requested_version):
    """
    Return whether available_version fulfills requested_version.

    Both are dicts with 'major' and 'minor' items.

    """

    # No requested major version is fulfilled by anything
    if requested_version['major'] is None:
        return True

    # If major version is requested, that at least must match
    if requested_version['major'] != available_version['major']:
        return False

    # Major version matches, if no requested minor version it's a match
    if requested_version['minor'] is None:
        return True

    # If a minor version is requested, the available minor version must be >=
    return (
        available_version['minor'] is not None and
        available_version['minor'] >= requested_version['minor']
    )


@lru_cache(maxsize=None)
def find_posix_python(version):
    """Find the nearest version of python and return its path."""

    if version:
        # Try the exact requested version first
        path = find_executable('python' + version)

        # If that fails, try the major version
        if not path:
            path = find_executable('python' + version[0])

            # If the major version failed, see if the default is available
            if not path:
                path = find_executable('python')
    else:
        path = find_executable('python')

    return path


@lru_cache(maxsize=None)
def find_windows_python(version):
    """Find the nearest version of python and return its path."""

    if version:
        # On Windows, there may be no separately named python/python3 binaries,
        # so it seems the only reliable way to check for a given version is to
        # check the root drive for 'Python*' directories, and try to match the
        # version based on the directory names. The 'Python*' directories end
        # with the <major><minor> version number, so for matching with the version
        # passed in, strip any decimal points.
        stripped_version = version.replace('.', '')
        prefix = os.path.abspath('\\Python')
        prefix_len = len(prefix)
        dirs = glob(prefix + '*')

        # Try the exact version first, then the major version
        for version in (stripped_version, stripped_version[0]):
            for python_dir in dirs:
                path = os.path.join(python_dir, 'python.exe')
                python_version = python_dir[prefix_len:]

                # Try the exact version first, then the major version
                if python_version.startswith(version) and can_exec(path):
                    return path

    # No version or couldn't find a version match, try the default python
    path = find_executable('python')
    return path


@lru_cache(maxsize=None)
def find_python_script(python_path, script):
    """Return the path to the given script, or None if not found."""
    if sublime.platform() in ('osx', 'linux'):
        return which(script)
    else:
        # On Windows, scripts are .py files in <python directory>/Scripts
        script_path = os.path.join(os.path.dirname(python_path), 'Scripts', script + '-script.py')

        if os.path.exists(script_path):
            return script_path
        else:
            return None


@lru_cache(maxsize=None)
def get_python_paths():
    """
    Return sys.path for the system version of python 3.

    If python 3 cannot be found on the system, [] is returned.

    """

    python_path = which('@python3')[0]

    if python_path:
        code = r'import sys;print("\n".join(sys.path).strip())'
        out = communicate(python_path, code)
        paths = out.splitlines()

    else:
        paths = []

    return paths


@lru_cache(maxsize=None)
def find_executable(executable):
    """
    Return the path to the given executable, or None if not found.

    create_environment is used to augment PATH before searching
    for the executable.

    """

    env = create_environment()

    for base in env.get('PATH', '').split(os.pathsep):
        path = os.path.join(os.path.expanduser(base), executable)

        # On Windows, if path does not have an extension, try .exe, .cmd, .bat
        if sublime.platform() == 'windows' and not os.path.splitext(path)[1]:
            for extension in ('.exe', '.cmd', '.bat'):
                path_ext = path + extension

                if can_exec(path_ext):
                    return path_ext
        elif can_exec(path):
            return path

    return None


def touch(path):
    """Perform the equivalent of touch on Posix systems."""
    with open(path, 'a'):
        os.utime(path, None)


def open_directory(path):
    """Open the directory at the given path in a new window."""

    cmd = (get_subl_executable_path(), path)
    subprocess.Popen(cmd, cwd=path)


def get_subl_executable_path():
    """Return the path to the subl command line binary."""

    executable_path = sublime.executable_path()

    if sublime.platform() == 'osx':
        suffix = '.app/'
        app_path = executable_path[:executable_path.rfind(suffix) + len(suffix)]
        executable_path = app_path + 'Contents/SharedSupport/bin/subl'

    return executable_path


# popen utils

def combine_output(out, sep=''):
    """Return stdout and/or stderr combined into a string, stripped of ANSI colors."""
    output = sep.join((
        (out[0].decode('utf8') or '') if out[0] else '',
        (out[1].decode('utf8') or '') if out[1] else '',
    ))

    return ANSI_COLOR_RE.sub('', output)


def communicate(cmd, code='', output_stream=STREAM_STDOUT, env=None):
    """
    Return the result of sending code via stdin to an executable.

    The result is a string which comes from stdout, stderr or the
    combining of the two, depending on the value of output_stream.
    If env is not None, it is merged with the result of create_environment.

    """

    out = popen(cmd, output_stream=output_stream, extra_env=env)

    if out is not None:
        code = code.encode('utf8')
        out = out.communicate(code)
        return combine_output(out)
    else:
        return ''


def tmpfile(cmd, code, suffix='', output_stream=STREAM_STDOUT, env=None):
    """
    Return the result of running an executable against a temporary file containing code.

    It is assumed that the executable launched by cmd can take one more argument
    which is a filename to process.

    The result is a string combination of stdout and stderr.
    If env is not None, it is merged with the result of create_environment.

    """

    f = None

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            if isinstance(code, str):
                code = code.encode('utf-8')

            f.write(code)
            f.flush()

        cmd = list(cmd)

        if '@' in cmd:
            cmd[cmd.index('@')] = f.name
        else:
            cmd.append(f.name)

        out = popen(cmd, output_stream=output_stream, extra_env=env)

        if out:
            out = out.communicate()
            return combine_output(out)
        else:
            return ''
    finally:
        if f:
            os.remove(f.name)


def tmpdir(cmd, files, filename, code, output_stream=STREAM_STDOUT, env=None):
    """
    Run an executable against a temporary file containing code.

    It is assumed that the executable launched by cmd can take one more argument
    which is a filename to process.

    Returns a string combination of stdout and stderr.
    If env is not None, it is merged with the result of create_environment.

    """

    filename = os.path.basename(filename)
    d = tempfile.mkdtemp()
    out = None

    try:
        for f in files:
            try:
                os.makedirs(os.path.join(d, os.path.dirname(f)))
            except OSError:
                pass

            target = os.path.join(d, f)

            if os.path.basename(target) == filename:
                # source file hasn't been saved since change, so update it from our live buffer
                f = open(target, 'wb')

                if isinstance(code, str):
                    code = code.encode('utf8')

                f.write(code)
                f.close()
            else:
                shutil.copyfile(f, target)

        os.chdir(d)
        out = popen(cmd, output_stream=output_stream, extra_env=env)

        if out:
            out = out.communicate()
            out = combine_output(out, sep='\n')

            # filter results from build to just this filename
            # no guarantee all syntaxes are as nice about this as Go
            # may need to improve later or just defer to communicate()
            out = '\n'.join([
                line for line in out.split('\n') if filename in line.split(':', 1)[0]
            ])
        else:
            out = ''
    finally:
        shutil.rmtree(d, True)

    return out or ''


def popen(cmd, output_stream=STREAM_BOTH, env=None, extra_env=None):
    """Open a pipe to an external process and return a Popen object."""

    info = None

    if os.name == 'nt':
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE

    if output_stream == STREAM_BOTH:
        stdout = stderr = subprocess.PIPE
    elif output_stream == STREAM_STDOUT:
        stdout = subprocess.PIPE
        stderr = subprocess.DEVNULL
    else:  # STREAM_STDERR
        stdout = subprocess.DEVNULL
        stderr = subprocess.PIPE

    if env is None:
        env = create_environment()

    if extra_env is not None:
        env.update(extra_env)

    try:
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=stdout, stderr=stderr,
            startupinfo=info, env=env)
    except Exception as err:
        pass

# view utils

def apply_to_all_views(callback):
    """Apply callback to all views in all windows."""
    for window in sublime.windows():
        for view in window.views():
            callback(view)


# misc utils

def clear_caches():
    """Clear the caches of all methods in this module that use an lru_cache."""
    create_environment.cache_clear()
    which.cache_clear()
    find_python.cache_clear()
    get_python_paths.cache_clear()
    find_executable.cache_clear()


def convert_type(value, type_value, sep=None, default=None):
    """
    Convert value to the type of type_value.

    If the value cannot be converted to the desired type, default is returned.
    If sep is not None, strings are split by sep (plus surrounding whitespace)
    to make lists/tuples, and tuples/lists are joined by sep to make strings.

    """

    if type_value is None or isinstance(value, type(type_value)):
        return value

    if isinstance(value, str):
        if isinstance(type_value, (tuple, list)):
            if sep is None:
                return [value]
            else:
                if value:
                    return re.split(r'\s*{}\s*'.format(sep), value)
                else:
                    return []
        elif isinstance(type_value, Number):
            return float(value)
        else:
            return default

    if isinstance(value, Number):
        if isinstance(type_value, str):
            return str(value)
        elif isinstance(type_value, (tuple, list)):
            return [value]
        else:
            return default

    if isinstance(value, (tuple, list)):
        if isinstance(type_value, str):
            return sep.join(value)
        else:
            return list(value)

    return default


def get_user_fullname():
    """Return the user's full name (or at least first name)."""

    if sublime.platform() in ('osx', 'linux'):
        import pwd
        return pwd.getpwuid(os.getuid()).pw_gecos
    else:
        return os.environ.get('USERNAME', 'Me')


def center_region_in_view(region, view):
    """
    Center the given region in view.

    There is a bug in ST3 that prevents a selection change
    from being drawn when a quick panel is open unless the
    viewport moves. So we get the current viewport position,
    move it down 1.0, center the region, see if the viewport
    moved, and if not, move it up 1.0 and center again.

    """

    x1, y1 = view.viewport_position()
    view.set_viewport_position((x1, y1 + 1.0))
    view.show_at_center(region)
    x2, y2 = view.viewport_position()

    if y2 == y1:
        view.set_viewport_position((x1, y1 - 1.0))
        view.show_at_center(region)


# color-related constants

DEFAULT_MARK_COLORS = {'warning': 'EDBA00', 'error': 'DA2000', 'gutter': 'FFFFFF'}

COLOR_SCHEME_PREAMBLE = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
'''

COLOR_SCHEME_STYLES = {
    'warning': '''
        <dict>
            <key>name</key>
            <string>SublimeLinter Warning</string>
            <key>scope</key>
            <string>sublimelinter.mark.warning</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#{}</string>
            </dict>
        </dict>
    ''',

    'error': '''
        <dict>
            <key>name</key>
            <string>SublimeLinter Error</string>
            <key>scope</key>
            <string>sublimelinter.mark.error</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#{}</string>
            </dict>
        </dict>
    ''',

    'gutter': '''
        <dict>
            <key>name</key>
            <string>SublimeLinter Gutter Mark</string>
            <key>scope</key>
            <string>sublimelinter.gutter-mark</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#FFFFFF</string>
            </dict>
        </dict>
    '''
}


# menu command constants

CHOOSERS = (
    'Lint Mode',
    'Mark Style'
)

CHOOSER_MENU = '''{
    "caption": "$caption",
    "children":
    [
        $menus,
        $toggleItems
    ]
}'''

CHOOSER_COMMAND = '''{{
    "command": "sublimelinter_choose_{}", "args": {{"value": "{}"}}
}}'''

TOGGLE_ITEMS = {
    'Mark Style': '''
{
    "caption": "-"
},
{
    "caption": "No Column Highlights Line",
    "command": "sublimelinter_toggle_setting", "args":
    {
        "setting": "no_column_highlights_line",
        "checked": true
    }
}'''
}
