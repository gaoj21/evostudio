"""HTTP details shared by the API routes."""
import re
import unicodedata
from urllib.parse import quote


def content_disposition(filename, disposition='attachment'):
    """A Content-Disposition header value for `filename`, whatever its script.

    HTTP headers are Latin-1, so `数据.csv` cannot be sent as is (Starlette
    raises, and the download is a 500). RFC 6266: an ASCII `filename` for old
    clients, and the real name UTF-8 percent-encoded in `filename*`.
    """
    name = str(filename).replace('\r', '').replace('\n', '')
    ascii_name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    ascii_name = re.sub(r'["\\]', '_', ascii_name).strip() or 'download'
    if ascii_name.startswith('.'):
        ascii_name = 'download' + ascii_name
    value = f'{disposition}; filename="{ascii_name}"'
    if ascii_name != name:
        value += f"; filename*=UTF-8''{quote(name, safe='')}"
    return value
