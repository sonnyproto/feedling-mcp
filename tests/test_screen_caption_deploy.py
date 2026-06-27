from pathlib import Path


ROOT = Path(__file__).parent.parent
VLM_ENV = "FEEDLING_SCREEN_VLM_API_KEY"


def _service_block(compose_path: str, service: str, next_service: str) -> str:
    text = (ROOT / compose_path).read_text()
    start = f"\n  {service}:\n"
    end = f"\n  {next_service}:\n"
    assert start in text and end in text
    return text.split(start, 1)[1].split(end, 1)[0]


def test_prod_and_test_enclaves_receive_screen_vlm_key():
    for compose_path in (
        "deploy/docker-compose.phala.yaml",
        "deploy/docker-compose.phala.test.yaml",
    ):
        enclave = _service_block(compose_path, "enclave", "backend")
        assert f'{VLM_ENV}: "${{{VLM_ENV}:-}}"' in enclave


def test_prod_and_test_deploys_forward_screen_vlm_key():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert workflow.count(
        f"{VLM_ENV}: ${{{{ secrets.OPENROUTER_API_KEY }}}}"
    ) == 2
    assert workflow.count(f'-e "{VLM_ENV}=${VLM_ENV}"') == 2
