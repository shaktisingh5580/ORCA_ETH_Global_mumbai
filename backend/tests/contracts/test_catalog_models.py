"""Purpose: validate P03.1 catalog semantics and fail-closed rights independently.
Behavior: use test-only metadata to probe tenant visibility and immutable versions.
Refs: main plan P03.1; API 4.2; research-only guide 5; Phase: P03.1.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from orca.contracts.catalog import (
    CanonicalVariableMapping,
    LicenseProfile,
    ProductVersion,
    ScientificProduct,
    visible_metadata,
)


def version_data():
    return dict(
        id=uuid4(),
        tenant_id=uuid4(),
        product_id=uuid4(),
        source_version="test-version",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="test-only bulk SST",
        native_units="K",
        calendar="gregorian",
        retrieved_at="2025-02-02T00:00:00Z",
        license=LicenseProfile(),
    )


def test_catalog_semantic_types():
    data = version_data()
    value = ProductVersion(**data)
    assert value.available_at is None and value.depth_m is None
    native = ProductVersion(
        **(data | {"native_units": "Degree C", "dimensions": ("time", "latitude", "longitude")})
    )
    mapping = CanonicalVariableMapping.from_version(native)
    assert mapping.native_units == "Degree C"
    assert mapping.dimensions == ("time", "latitude", "longitude")
    assert mapping.version_id == native.id and mapping.depth_m is None
    for field, invalid in (
        ("native_units", "metres"),
        ("standard_name", "sea_water_temperature"),
        ("depth_m", -1),
        ("retrieved_at", "2025-02-02"),
    ):
        with pytest.raises(ValidationError):
            ProductVersion(**(data | {field: invalid}))


def test_catalog_license_scope():
    with pytest.raises(ValidationError):
        LicenseProfile(can_preserve=True)
    data = version_data()
    version = ProductVersion(
        **(
            data
            | {
                "license": LicenseProfile(
                    license_ref="test-license",
                    evidence_digest="sha256:" + "b" * 64,
                    can_preserve=True,
                )
            }
        )
    )
    assert version.can_preserve_for(version.tenant_id)
    assert not version.can_preserve_for(uuid4())
    assert not version.license.can_redistribute


def test_product_version_stability():
    version = ProductVersion(**version_data())
    with pytest.raises(ValidationError):
        version.source_version = "replacement"
    with pytest.raises(ValidationError):
        version.license.can_preserve = True
    assert ProductVersion.model_validate_json(version.model_dump_json()) == version


def test_hidden_restricted_metadata():
    owner, outsider = uuid4(), uuid4()
    product = ScientificProduct(
        id=uuid4(),
        tenant_id=owner,
        provider_id="test-provider",
        source_product_id="test-product",
        kind="observation",
    )
    assert visible_metadata(product, owner)
    assert not visible_metadata(product, outsider)
    public = product.model_copy(update={"access_class": "public"})
    assert not visible_metadata(public, outsider)
    assert visible_metadata(
        public.model_copy(update={"metadata_publication_allowed": True}), outsider
    )
