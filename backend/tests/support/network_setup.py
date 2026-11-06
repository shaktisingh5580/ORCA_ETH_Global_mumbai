"""Purpose: provision synthetic local-only sources and separately keyed A2A test processes.
Behavior: confine fake wallets/captures to disposable tests without chain or cloud writes.
Refs: H04 protocol test gate; Phase: H04; test support only.
"""

import csv
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.contracts.network import AgentBinding
from orca.infrastructure.federation.runtime import (
    ArkivGateway,
    NetworkConfiguration,
    NetworkRuntime,
)
from orca.infrastructure.federation.signing import b64


def identity(name, capability, port, wallet):
    private = Ed25519PrivateKey.generate()
    return AgentBinding(
        id=uuid4(),
        binding_version=uuid4(),
        name=name,
        capability=capability,
        endpoint=f"http://127.0.0.1:{port}/a2a",
        public_key=b64(
            private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
        key_id=name,
        wallet=wallet,
    ), b64(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )


def unused_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def configuration(folder):
    coordinator, secret = identity("test-coordinator", "sst_reproduction", 8000, "0x" + "11" * 20)
    ocean, ocean_secret = identity(
        "test-ocean", "sst_reproduction", unused_port(), "0x" + "22" * 20
    )
    reviewer, reviewer_secret = identity(
        "test-reviewer", "sst_review", unused_port(), "0x" + "33" * 20
    )
    config = NetworkConfiguration(coordinator=coordinator, agents=(ocean, reviewer))
    configfile = folder / "bindings.json"
    configfile.write_text(config.model_dump_json(), encoding="utf-8")
    runtime = NetworkRuntime(
        config,
        secret,
        ArkivGateway(folder / "no-wallet.env", folder / "coordinator", coordinator.wallet),
    )
    return runtime, configfile, ((ocean, ocean_secret), (reviewer, reviewer_secret))


def source_recipe(folder):
    source = folder / "synthetic-test-sources"
    source.mkdir()
    rows = [
        ["variable", name, "", "double", "time, latitude, longitude"]
        for name in ("analysed_sst", "analysis_error", "mask")
    ]
    attrs = {
        ("analysed_sst", "standard_name"): "sea_surface_foundation_temperature",
        ("analysed_sst", "units"): "degree_C",
        ("analysis_error", "units"): "degree_C",
        ("analysis_error", "long_name"): "Estimated Error Standard Deviation",
        ("mask", "flag_masks"): "1, 2, 4, 8, 16",
        (
            "mask",
            "flag_meanings",
        ): "open_sea land open_lake open_sea_with_ice_in_the_grid open_lake_with_ice_in_the_grid",
        ("NC_GLOBAL", "Conventions"): "CF-1.6",
        ("NC_GLOBAL", "license"): "Synthetic test-only data may be used and redistributed for free",
        ("NC_GLOBAL", "geospatial_lat_resolution"): "0.01",
        ("NC_GLOBAL", "geospatial_lon_resolution"): "0.01",
        ("analysed_sst", "valid_min"): "-2",
        ("analysed_sst", "valid_max"): "40",
        ("analysed_sst", "_FillValue"): "-999",
        ("analysis_error", "valid_min"): "0",
        ("analysis_error", "valid_max"): "10",
        ("analysis_error", "_FillValue"): "-999",
        ("latitude", "units"): "degrees_north",
        ("longitude", "units"): "degrees_east",
    }
    rows.extend(
        ["attribute", name, attribute, "String", value]
        for (name, attribute), value in attrs.items()
    )
    raw = json.dumps(
        {
            "table": {
                "columnNames": [
                    "Row Type",
                    "Variable Name",
                    "Attribute Name",
                    "Data Type",
                    "Value",
                ],
                "rows": rows,
            }
        }
    ).encode()
    (source / "metadata.json").write_bytes(raw)
    metadata_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    members = []
    for year in (2025, *range(2003, 2023)):
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["time", "latitude", "longitude", "analysed_sst", "analysis_error", "mask"])
        writer.writerow(["UTC", "degrees_north", "degrees_east", "degree_C", "degree_C", ""])
        writer.writerows(
            [
                f"{year}-01-{day:02d}T09:00:00Z",
                round(20.45 + y * 0.01, 2),
                round(68.45 + x * 0.01, 2),
                27 if year == 2025 else 26,
                0.5,
                1,
            ]
            for day in range(1, 32)
            for y in range(11)
            for x in range(11)
        )
        data = output.getvalue().encode()
        (source / f"{year}-01.csv").write_bytes(data)
        members.append(
            {
                "year": year,
                "id": str(uuid4()),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )
    result = {
        "method": "orca.native-seasonal-sst/hackathon-v1",
        "bundle_digest": "sha256:" + "b" * 64,
        "metadata_digest": metadata_digest,
        "input_snapshots": members,
        "bbox": [68.45, 20.45, 68.55, 20.55],
        "current_mean": 27.0,
        "baseline_mean": 26.0,
        "anomaly": 1.0,
        "daily": [
            {
                "date": f"2025-01-{day:02d}",
                "current_mean": 27.0,
                "baseline_mean": 26.0,
                "anomaly": 1.0,
            }
            for day in range(1, 32)
        ],
        "source_mode": "real",
        "redistribution_permitted": True,
    }
    return source, result


def start_provider(folder, configfile, binding, private, source):
    from orca.agent_provider import ProviderSettings

    bindingfile = folder / f"{binding.key_id}.json"
    bindingfile.write_text(binding.model_dump_json(), encoding="utf-8")
    settings = ProviderSettings(
        binding_file=str(bindingfile),
        coordinator_file=str(configfile),
        private_key=private,
        wallet_env_file=str(folder / "unused-wallet.env"),
        capture_folder=str(source),
        state_dir=str(folder / binding.key_id),
    )
    envfile = folder / f"{binding.key_id}.env"
    envfile.write_text(
        "\n".join(
            "ORCA_PROVIDER_"
            + key.upper()
            + "="
            + json.dumps(value.get_secret_value() if hasattr(value, "get_secret_value") else value)
            for key, value in settings.model_dump().items()
        ),
        encoding="utf-8",
    )
    log = (folder / f"{binding.key_id}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "orca.agent_provider", "--env-file", str(envfile)],
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"),
    )
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            for _ in range(100):
                try:
                    response = client.get(
                        binding.endpoint.removesuffix("/a2a") + "/.well-known/agent-card.json"
                    )
                    if response.status_code == 200:
                        return process, log, settings
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise AssertionError((folder / f"{binding.key_id}.log").read_text())
                time.sleep(0.1)
        raise AssertionError("provider did not become ready")
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        raise
