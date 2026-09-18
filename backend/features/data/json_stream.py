"""Read a JSON array incrementally using only the standard library."""
import json


def read_json_records(path, chunk_size=65536):
    decoder=json.JSONDecoder()
    with open(path,encoding='utf-8-sig') as file:
        buffer='';ended=False
        def more():
            nonlocal buffer,ended
            chunk=file.read(chunk_size)
            if not chunk: ended=True
            buffer+=chunk
        def whitespace():
            nonlocal buffer
            buffer=buffer.lstrip()
            while not buffer and not ended:
                more();buffer=buffer.lstrip()
        def value():
            nonlocal buffer
            while True:
                try: row,index=decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    if ended: raise
                    more();continue
                buffer=buffer[index:]
                return row
        whitespace()
        if buffer.startswith('['):
            buffer=buffer[1:];whitespace()
            if buffer.startswith(']'): buffer=buffer[1:]
            else:
                while True:
                    whitespace();row=value()
                    if not isinstance(row,dict): raise ValueError('Dataset JSON array items must be objects.')
                    yield row
                    whitespace()
                    if buffer.startswith(']'): buffer=buffer[1:];break
                    if not buffer.startswith(','): raise ValueError('Expected comma or closing bracket in JSON array.')
                    buffer=buffer[1:]
        else:
            row=value()
            if not isinstance(row,dict): raise ValueError('Dataset JSON must be an object or array of objects.')
            yield row
        whitespace()
        if buffer: raise ValueError('Extra data after JSON dataset.')
