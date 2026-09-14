"""Parse uploaded JSON records by content, independently of .json/.jsonl names."""
import json


def parse_json_records(text, max_records=0):
    decoder = json.JSONDecoder()
    documents = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        try:
            value, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f'Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}. '
                'Use a JSON array or complete JSON objects; no records were imported.'
            ) from None
        documents.append(value)
        if max_records and len(documents) > max_records:
            raise ValueError(f'Dataset exceeds {max_records:,} records. Split the file first.')
        position = end
    if len(documents) == 1:
        value = documents[0]
        if isinstance(value, list):
            records = value
        elif isinstance(value, dict) and isinstance(value.get('records'), list):
            records = value['records']
        else:
            records = [value]
    else:
        records = documents
    if not records:
        raise ValueError('Uploaded file contains no records.')
    if max_records and len(records) > max_records:
        raise ValueError(f'Dataset exceeds {max_records:,} records. Split the file first.')
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict) or not record:
            raise ValueError(f'Record {index}: expected a non-empty JSON object. Put records in one array or supply individual objects.')
    try:
        json.dumps(records, allow_nan=False)
    except ValueError:
        raise ValueError('JSON numbers must be finite; NaN and Infinity are not supported.') from None
    return records
