import pytest


@pytest.fixture(autouse=True)
def no_benchmarks(tmp_path, monkeypatch):
    """Tests run without the real benchmarks.yaml; tests about benchmarks write their own."""
    path = tmp_path / 'benchmarks.yaml'
    path.write_text('benchmarks: {}\nmarket: {}\n')
    monkeypatch.setenv('SCREENER_BENCHMARKS', str(path))
    return path
