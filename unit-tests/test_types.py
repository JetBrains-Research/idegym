from idegym.api.type import KubernetesObjectName, OCIImageName
from pydantic import BaseModel, ValidationError
from pytest import mark, param, raises


class KubernetesObject(BaseModel):
    name: KubernetesObjectName


class OCIImage(BaseModel):
    image: OCIImageName


@mark.parametrize(
    "value",
    [
        param("alpine:3.19", id="short"),
        param("docker.io/alpine:3.19", id="docker"),
        param("ghcr.io/org/image:latest", id="github"),
        param("registry.example.com/image:v1.0.0", id="example"),
        param("image@sha256:abcdef1234567890", id="digest"),
    ],
)
def test_oci_image_name_valid(value: str):
    OCIImage(image=value)


@mark.parametrize(
    "value",
    [
        param("MyRepo/Image:Tag", id="uppercase"),
        param("bad image:tag", id="space"),
        param("image:name?bad", id="illegal-char"),
        param("", id="empty"),
    ],
)
def test_oci_image_name_invalid(value: str):
    with raises(ValidationError):
        OCIImage(image=value)


@mark.parametrize(
    "value",
    [
        param("my-server", id="simple"),
        param("a", id="single-char"),
        param("server-123-test", id="with-numbers"),
        param("a" * 63, id="max-length"),
    ],
)
def test_kubernetes_object_name_valid(value: str):
    KubernetesObject(name=value)


@mark.parametrize(
    "value",
    [
        param("", id="empty"),
        param("a" * 64, id="long"),
        param("UPPER", id="uppercase"),
        param("ends-with-", id="ends-with-hyphen"),
        param("9startswithdigit", id="starts-with-digit"),
        param("Invalid_Name", id="underscore-and-uppercase"),
    ],
)
def test_kubernetes_object_name_invalid(value: str):
    with raises(ValidationError):
        KubernetesObject(name=value)
