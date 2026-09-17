import builtins
from backend.features.data import dataloaders


def test_preview_and_batch_do_not_import_torch_in_api(monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name != 'torch_loader' and not name.startswith('torch'), 'Native runtime imported in API'
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    rows = [{'value': i} for i in range(5)]
    monkeypatch.setattr(dataloaders, '_prepare_cached', lambda config: (rows, {'output_records': 5}))
    assert dataloaders.prepare({})[0] == rows
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException): dataloaders.preview({})
    assert list(dataloaders.iter_batches({'read_batch_size': 2})) == [rows[:2], rows[2:4], rows[4:]]
