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


def explain(exc, code, logs='', filename='<user_code.py>', label=None):
    """Describe exc raised while running code compiled under filename.

    With a label (for example "Dataset code"), the first line names the
    innermost line of the user's code: "Dataset code line 7: KeyError: 'x'".
    """
    lines = code.splitlines()
    frames = [frame for frame in traceback.extract_tb(exc.__traceback__) if frame.filename == filename]
    where = []
    for frame in frames:
        text = lines[frame.lineno - 1].strip() if frame.lineno and 0 < frame.lineno <= len(lines) else ''
        where.append(f'  line {frame.lineno}, in {frame.name}: {text}')
    message = f'{type(exc).__name__}: {exc}'
    if label:
        message = f'{label} line {frames[-1].lineno}: {message}' if frames else f'{label}: {message}'
    if where:
        message += '\nIn your code:\n' + '\n'.join(where)
    if logs.strip():
        message += '\nOutput before the error:\n' + logs[-ERROR_LOG_LIMIT:]
    return message
