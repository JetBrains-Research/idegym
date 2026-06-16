"""Unit tests for build-backend selection (`build_image_builder`) and config validation."""

import pytest
from idegym.api.config import BuildConfig
from idegym.api.image_build import BuildBackend
from idegym.backend.utils.image_builder import build_image_builder
from idegym.backend.utils.image_builder.cloudbuild_gke import CloudBuildGKEImageBuilder
from idegym.backend.utils.image_builder.kaniko import KanikoImageBuilder

pytestmark = pytest.mark.unit


def test_default_backend_is_kaniko():
    config = BuildConfig()
    assert config.backend == BuildBackend.KANIKO

    builder = build_image_builder(config)
    assert isinstance(builder, KanikoImageBuilder)


def test_factory_passes_kaniko_runtime_knobs():
    builder = build_image_builder(
        BuildConfig(),
        insecure_registry=True,
        node_pool_taint_key="jetbrains.com/idegym",
        node_pool_preference_weight=42,
    )
    assert isinstance(builder, KanikoImageBuilder)
    assert builder._insecure_registry is True
    assert builder._node_pool_taint_key == "jetbrains.com/idegym"
    assert builder._node_pool_preference_weight == 42


def test_factory_builds_cloudbuild_backend():
    config = BuildConfig(
        backend=BuildBackend.CLOUDBUILD_GKE,
        cloudbuild_gke={
            "project_id": "my-project",
            "region": "europe-west1",
            "staging_bucket": "my-bucket",
            "machine_type": "E2_HIGHCPU_8",
            "disk_size_gb": 100,
            "skip_existing": True,
        },
    )
    builder = build_image_builder(config)
    assert isinstance(builder, CloudBuildGKEImageBuilder)
    assert builder._project_id == "my-project"
    assert builder._region == "europe-west1"
    assert builder._staging_bucket == "my-bucket"
    assert builder._machine_type == "E2_HIGHCPU_8"
    assert builder._disk_size_gb == 100
    assert builder._skip_existing is True


@pytest.mark.parametrize("missing", ["project_id", "region", "staging_bucket"])
def test_cloudbuild_backend_requires_core_settings(missing):
    settings = {"project_id": "p", "region": "r", "staging_bucket": "b"}
    del settings[missing]
    with pytest.raises(ValueError, match=missing):
        BuildConfig(backend=BuildBackend.CLOUDBUILD_GKE, cloudbuild_gke=settings)
