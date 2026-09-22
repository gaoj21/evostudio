"""One Input batch size for PyTorch, workflow chunks and native API batches."""
def loader_batch_size(source):
    config = (source or {}).get('config') or {}
    if config.get('type') != 'dataloader':
        return None
    size = config.get('read_batch_size', 100)
    if type(size) is not int or not 1 <= size <= 1024:
        raise ValueError('Input batch size must be a whole number between 1 and 1024.')
    return size
