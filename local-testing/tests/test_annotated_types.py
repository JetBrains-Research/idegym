"""Test validation of annotated types like OCI image names and Kubernetes names."""

import pytest
from pydantic import ValidationError

from .idegym_utils import create_http_client


@pytest.mark.asyncio
async def test_invalid_oci_image_name_raises_validation_error():
    async with create_http_client(name="types-check") as client:
        invalid_images = [
            "MyRepo/Image:Tag",  # uppercase letters
            "bad image:tag",  # space
            "image:name?bad",  # illegal '?'
            "",  # empty
        ]

        for bad in invalid_images:
            with pytest.raises(ValidationError):
                async with client.with_server(image_tag=bad, server_name="okname"):
                    pass


@pytest.mark.asyncio
async def test_invalid_k8s_name_raises_validation_error():
    async with create_http_client(name="types-check2") as client:
        # Use a syntactically valid OCI image name so that only server_name validation fails
        valid_image_like = "alpine:3.19"

        invalid_names = [
            "Invalid_Name",  # underscore + uppercase
            "9startswithdigit",  # must start with letter
            "ends-with-",  # cannot end with '-'
            "UPPER",  # uppercase not allowed
            "a" * 64,  # too long (max 63)
            "",  # empty
        ]

        for bad in invalid_names:
            with pytest.raises(ValidationError):
                async with client.with_server(image_tag=valid_image_like, server_name=bad):
                    pass
