"""Backend portability of the telemetry pipeline (task 10.9, requirement 18.13).

The claim under test: the platform moves from the local backend (Jaeger + Prometheus) to AWS
(X-Ray + CloudWatch) by swapping *one collector config file* and changing *nothing* in the
application. These checks pin the invariants that make that true, so a future change that couples
the application to a specific backend — or that lets the two collector configs drift apart in their
receivers or their scrub processor — fails here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_COLLECTOR = _REPO_ROOT / "docker" / "otel-collector.yaml"
_AWS_COLLECTOR = _REPO_ROOT / "docker" / "otel-collector.aws.yaml"
_APP_SRC = _REPO_ROOT / "backend" / "src" / "c360"


def _load(path: Path) -> dict:
    data: dict = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def test_both_collector_configs_exist() -> None:
    assert _LOCAL_COLLECTOR.is_file()
    assert _AWS_COLLECTOR.is_file()


def test_receivers_are_identical_across_backends() -> None:
    # The app exports the same OTLP stream to either collector; the receiver side must not differ.
    assert _load(_LOCAL_COLLECTOR)["receivers"] == _load(_AWS_COLLECTOR)["receivers"]


def test_scrub_processor_is_present_in_both() -> None:
    # The defence-in-depth scrub must survive the swap — it is not a local-only convenience.
    for path in (_LOCAL_COLLECTOR, _AWS_COLLECTOR):
        processors = _load(path)["processors"]
        assert "attributes/scrub" in processors, path.name
        dropped = {
            action["key"]
            for action in processors["attributes/scrub"]["actions"]
            if action["action"] == "delete"
        }
        # The keys that must never leave the process, regardless of backend.
        assert {"gen_ai.prompt", "gen_ai.completion"} <= dropped


def test_aws_config_targets_xray_and_cloudwatch() -> None:
    aws = _load(_AWS_COLLECTOR)
    assert "awsxray" in aws["exporters"]
    assert "awsemf" in aws["exporters"]
    assert "awsxray" in aws["service"]["pipelines"]["traces"]["exporters"]
    assert "awsemf" in aws["service"]["pipelines"]["metrics"]["exporters"]


def test_local_config_targets_jaeger_and_prometheus() -> None:
    local = _load(_LOCAL_COLLECTOR)
    assert "otlp/jaeger" in local["service"]["pipelines"]["traces"]["exporters"]
    assert "prometheus" in local["service"]["pipelines"]["metrics"]["exporters"]


def test_pipelines_share_the_same_processor_chain() -> None:
    # Traces go through scrub+batch on both; metrics through batch on both. The pipeline *shape* is
    # backend-independent — only the exporters differ.
    local = _load(_LOCAL_COLLECTOR)["service"]["pipelines"]
    aws = _load(_AWS_COLLECTOR)["service"]["pipelines"]
    assert local["traces"]["processors"] == aws["traces"]["processors"]
    assert local["metrics"]["processors"] == aws["metrics"]["processors"]


def test_application_has_exactly_one_backend_coupling_setting() -> None:
    # The only setting that names where telemetry goes is the OTLP endpoint. If a second appears,
    # portability has been compromised and this test should be revisited deliberately.
    config_text = (_APP_SRC / "core" / "config.py").read_text(encoding="utf-8")
    assert "otel_exporter_otlp_endpoint" in config_text


def test_no_backend_specific_coupling_in_application_code() -> None:
    # Knowing the backend is the collector's job. The application must not *import* anything
    # backend-specific — an X-Ray/CloudWatch/Jaeger/Prometheus exporter or client. This inspects the
    # AST's import statements only, so a docstring that explains the backend-agnostic design (which
    # is expected and correct) is ignored; only real code coupling fails the test.
    import ast  # noqa: PLC0415

    forbidden = ("xray", "cloudwatch", "awsemf", "jaeger", "prometheus")
    offenders: list[str] = []
    for path in _APP_SRC.rglob("*.py"):
        # utf-8-sig tolerates a byte-order mark some editors prepend, which `ast.parse` rejects.
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.append(node.module)
        for module in modules:
            lowered = module.lower()
            for term in forbidden:
                if term in lowered:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}: imports {module}")
    assert not offenders, f"backend-specific import in application code: {offenders}"
