from pathlib import Path

import pytest

from helpers import E2EConfig, E2EEnvironment


def pytest_addoption(parser):
    parser.addoption("--run-checker-e2e", action="store_true", default=False)


def pytest_configure(config):
    config.addinivalue_line("markers", "checker_e2e: real checker lifecycle test")


@pytest.fixture(scope="session")
def e2e_config(pytestconfig):
    if not pytestconfig.getoption("--run-checker-e2e"):
        pytest.skip("Use --run-checker-e2e")

    return E2EConfig.from_env()


@pytest.fixture(scope="session")
def env(e2e_config,tmp_path_factory):
    workdir = tmp_path_factory.mktemp("checker-e2e")

    environment = E2EEnvironment(e2e_config, workdir)

    environment.assert_ready()

    return environment


@pytest.fixture(scope="session")
def partial_solution():
    return (Path(__file__).parent / "solutions" / "partial_solution.py").read_text()


@pytest.fixture(scope="session")
def correct_solution():
    return (Path(__file__).parent / "solutions" / "correct_solution.py").read_text()
