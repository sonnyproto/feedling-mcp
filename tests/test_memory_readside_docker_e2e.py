from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_memory_sandbox_compose_uses_dev_seed_not_dstack_socket():
    compose = (ROOT / "deploy" / "docker-compose.memory-sandbox.yaml").read_text()

    assert "FEEDLING_DEV_DSTACK_SEED" in compose
    assert "DSTACK_SIMULATOR_ENDPOINT" not in compose
    assert "dstack.sock" not in compose
