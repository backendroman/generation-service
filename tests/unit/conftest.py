import pytest


@pytest.fixture(autouse=True)
def stack_guard():
    # без docker
    yield
