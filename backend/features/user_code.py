"""Errors raised by user-supplied Python (evaluators, Datasets), told in terms
of that code: the line it happened on and what it printed before failing."""
import io
import traceback

# How much of the code's printed output an error message keeps.
ERROR_LOG_LIMIT = 4000


class TailBuffer(io.TextIOBase):
    """A stdout/stderr replacement that keeps only the last `limit`
    characters, so a long-running stream that prints cannot grow memory."""

    def __init__(self, limit=ERROR_LOG_LIMIT):
        self.limit, self.text = limit, ''

    def writable(self):
        return True

    def write(self, text):
        self.text = (self.text + text)[-self.limit:]
        return len(text)

    def getvalue(self):
        return self.text


class UserCodeError(ValueError):
    """An exception raised by the user's code. Isolated workers report its
    message as is (see chat_worker), without prefixing the exception type."""
    user_code = True


def explain(exc, code, logs='', filename='<user_code.py>', label=None, root=None):
    """Describe exc raised while running code compiled under filename.

    With a label (for example "Dataset code"), the first line names the
    innermost line of the user's code: "Dataset code line 7: KeyError: 'x'".
    With a root (an uploaded folder), frames in the folder's other modules
    count as the user's code too, named by their path inside it.
    """
    import linecache
    import os
    lines = code.splitlines()
    prefix = os.path.join(str(root), '') if root else None

    def mine(frame):
        return frame.filename == filename or (prefix is not None and frame.filename.startswith(prefix))

    frames = [frame for frame in traceback.extract_tb(exc.__traceback__) if mine(frame)]

    def place(frame):
        if frame.filename == filename:
            return f'line {frame.lineno}'
        return f'{os.path.relpath(frame.filename, str(root))} line {frame.lineno}'

    where = []
    for frame in frames:
        if frame.filename == filename:
            text = lines[frame.lineno - 1].strip() if frame.lineno and 0 < frame.lineno <= len(lines) else ''
        else:
            text = linecache.getline(frame.filename, frame.lineno or 0).strip()
        where.append(f'  {place(frame)}, in {frame.name}: {text}')
    message = f'{type(exc).__name__}: {exc}'
    if isinstance(exc, SyntaxError) and exc.filename == filename and exc.lineno and not frames:
        # A syntax error has no frame in the code it is about.
        message = f'{label} line {exc.lineno}: {message}' if label else message
    elif label:
        message = f'{label} {place(frames[-1])}: {message}' if frames else f'{label}: {message}'
    if where:
        message += '\nIn your code:\n' + '\n'.join(where)
    advice = hint(exc)
    if advice:
        message += '\n' + advice
    if logs.strip():
        message += '\nOutput before the error:\n' + logs[-ERROR_LOG_LIMIT:]
    return message


# Import names whose package on PyPI is called something else.
PACKAGE_NAMES = {'sklearn': 'scikit-learn', 'cv2': 'opencv-python', 'PIL': 'Pillow', 'yaml': 'PyYAML',
                 'bs4': 'beautifulsoup4', 'skimage': 'scikit-image', 'dateutil': 'python-dateutil',
                 'docx': 'python-docx', 'pptx': 'python-pptx', 'fitz': 'PyMuPDF', 'dotenv': 'python-dotenv',
                 'Crypto': 'pycryptodome', 'Levenshtein': 'python-Levenshtein', 'magic': 'python-magic'}


def install_command(module):
    """How to install the package providing `module` into the Python that
    runs Studio's workers (this interpreter). Studio never installs it."""
    import shlex
    import sys
    top = module.split('.')[0]
    return f'{shlex.quote(sys.executable)} -m pip install {PACKAGE_NAMES.get(top, top)}'


def spacy_model_command(model):
    import shlex
    import sys
    return f'{shlex.quote(sys.executable)} -m spacy download {model}'


def hint(exc):
    """What to do about an error whose cause is the environment rather than
    the code: a package or a spaCy model that is not installed."""
    import importlib.util
    import re
    import sys
    shadow = _studio_shadow(exc)
    if shadow:
        name, where = shadow
        return (f"Module '{name}' was loaded from Studio's own folder {where}, not from an installed package or "
                f"your uploaded files. Install the package into Studio's environment ({sys.prefix}) with:\n"
                f"  {install_command(name)}\nor upload your module '{name}' with the data resource (its folder comes first on the import path).")
    text = str(exc)
    if isinstance(exc, ImportError) and not isinstance(exc, ModuleNotFoundError) and any(
            marker in text for marker in NATIVE_LOAD_ERRORS):
        name = (exc.name or '').split('.')[0] or 'the package'
        where = f' (loaded from {exc.path})' if getattr(exc, 'path', None) else ''
        return (f"A native library of '{name}'{where} could not be loaded: it was built for another Python, "
                f"architecture or environment (for example a conda installation). Reinstall it into Studio's "
                f"environment ({sys.prefix}) with:\n  {install_command(name)} --force-reinstall --no-cache-dir")
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        top = exc.name.split('.')[0]
        try:
            installed = importlib.util.find_spec(top) is not None
        except (ImportError, ValueError):
            installed = False
        if installed and top != exc.name:
            return (f"Package '{top}' is installed but has no module '{exc.name}': check the import path "
                    f"or the installed version ({install_command(top)} --upgrade).")
        return (f"Python package for '{top}' is not installed in Studio's environment. Install it with:\n"
                f"  {install_command(top)}\nthen run again (no restart needed for new imports).")
    match = re.search(r"\[E050\] Can't find model '([^']+)'", str(exc))
    if match and '/' not in match.group(1) and '\\' not in match.group(1):
        return (f"spaCy model '{match.group(1)}' is not installed in Studio's environment. Install it with:\n"
                f"  {spacy_model_command(match.group(1))}")
    return ''


NATIVE_LOAD_ERRORS = ('Library not loaded', 'symbol not found', 'Symbol not found', 'undefined symbol',
                      'incompatible architecture', 'image not found', 'wrong ELF class', 'DLL load failed',
                      'mach-o file, but is an incompatible')


def _studio_shadow(exc):
    """(module, folder) when the module an ImportError / AttributeError is
    about was resolved to a folder of Studio's checkout rather than to an
    installed package or the user's files."""
    import re
    import sys
    from pathlib import Path
    name = None
    if isinstance(exc, ImportError) and exc.name:
        name = exc.name
    elif isinstance(exc, AttributeError):
        found = re.match(r"module '([\w.]+)' has no attribute", str(exc))
        name = found and found.group(1)
    if not name:
        return None
    top = name.split('.')[0]
    if top in ('backend', 'evoagentx'):
        return None
    module = sys.modules.get(top)
    if module is None:
        return None
    places = [*list(getattr(module, '__path__', None) or []), *([module.__file__] if getattr(module, '__file__', None) else [])]
    if places and all(in_studio_checkout(place) for place in places):
        return top, str(places[0])
    return None


def in_studio_checkout(place):
    """Inside Studio's source checkout, and not inside the Python environment
    (a .venv may live in the checkout: its site-packages are installed code)."""
    import sys
    from pathlib import Path
    try:
        path = Path(place).resolve()
    except (OSError, TypeError):
        return False
    repo = Path(__file__).resolve().parents[2]
    environments = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    inside = lambda folder: path == folder or folder in path.parents
    return inside(repo) and not any(inside(folder) for folder in environments)
