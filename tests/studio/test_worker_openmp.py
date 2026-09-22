"""Workers start with one OpenMP thread and tolerate duplicate runtimes, so
user code importing several native packages (torch, scikit-learn, faiss —
each bundles its own libomp) does not abort with OMP Error #15."""
from backend.features.chat import chat_control


def test_worker_env_sets_openmp_defaults(monkeypatch):
    for key in chat_control.OPENMP_ENV:
        monkeypatch.delenv(key, raising=False)
    env = chat_control.worker_env()
    assert env["OMP_NUM_THREADS"] == "1" and env["KMP_DUPLICATE_LIB_OK"] == "TRUE"
    assert env["STUDIO_WORKER_PARENT"]


def test_an_exported_value_wins(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    assert chat_control.worker_env()["OMP_NUM_THREADS"] == "4"


def test_a_dataset_mixing_openmp_packages_runs_in_a_worker():
    code = '''
import numpy as np, torch
from sklearn.cluster import KMeans
KMeans(4, n_init=1).fit(np.random.rand(5000, 8))
x = torch.randn(400, 400); x @ x
from torch.utils.data import Dataset
class D(Dataset):
    def __len__(self): return 2
    def __getitem__(self, i): return {"i": i}
def build_dataset(resource, config): return D()
'''
    rows = chat_control.worker('dataset', {'code': code, 'resource': {}, 'config': {},
                                           'batch_size': 10, 'sample_limit': None}, timeout=120)
    assert rows == [{"i": 0}, {"i": 1}]
