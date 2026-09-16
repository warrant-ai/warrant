import pytest

from warrant import AgentInfo, Warrant


@pytest.fixture
def agent() -> AgentInfo:
    return AgentInfo(name="credit-underwriter", version="2.3.1", instance="test")


@pytest.fixture
def client(tmp_path, agent):
    w = Warrant("lending", tenant="demo-bank", store=tmp_path / "records.db", agent=agent, currency="INR", flush_interval=0.05)
    yield w
    w.close()
