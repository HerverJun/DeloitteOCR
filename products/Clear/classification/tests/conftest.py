import pytest
from fastapi.testclient import TestClient

from image_platform.api import create_app
from image_platform.settings import Settings
from image_platform.store import Store, dump, now


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "data", lease_seconds=45)


@pytest.fixture
def store(settings):
    instance = Store(settings)
    with instance.connect(write=True) as con:
        con.execute("INSERT INTO projects(id,name,labels,created_at) VALUES('p','测试项目',?,?)",
                    (dump([{"label_id": "a", "display_name": "类别 A", "definition": "A"}, {"label_id": "b", "display_name": "类别 B", "definition": "B"}]), now()))
    return instance


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765", headers={"X-Platform-Request": "1"}) as session:
        yield session
