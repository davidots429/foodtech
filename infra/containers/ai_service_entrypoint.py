"""Materialize ECS-injected TLS material as private files, then start one service."""

import os
import tempfile


def service_settings():
    legacy_inputs = (
        "FOODTECH_TLS_SERVER_CERT_PEM",
        "FOODTECH_TLS_SERVER_KEY_PEM",
        "FOODTECH_TLS_BUSINESS_CA_PEM",
    )
    return {
        "credential-registration": (
            "foodtech_credential_registration.serve",
            dict(zip(legacy_inputs, (
                "FOODTECH_REGISTRATION_SERVER_CERT",
                "FOODTECH_REGISTRATION_SERVER_KEY",
                "FOODTECH_REGISTRATION_BUSINESS_CA",
            ), strict=True)),
        ),
        "ai-gateway": (
            "foodtech_ai_gateway.serve",
            dict(zip(legacy_inputs, (
                "FOODTECH_GATEWAY_SERVER_CERT",
                "FOODTECH_GATEWAY_SERVER_KEY",
                "FOODTECH_GATEWAY_BUSINESS_CA",
            ), strict=True)),
        ),
        "tonkatsu-gateway": (
            "foodtech_ai_gateway.serve_tonkatsu",
            tonkatsu_tls_paths("SERVER_CERT", "SERVER_KEY", "BROKER_CA", "BROKER_CERT"),
        ),
        "tonkatsu-business-gateway": (
            "foodtech_ai_gateway.serve_tonkatsu_business",
            tonkatsu_tls_paths(
                "BUSINESS_GATEWAY_SERVER_CERT", "BUSINESS_GATEWAY_SERVER_KEY",
                "BUSINESS_GATEWAY_CLIENT_CA", "BUSINESS_GATEWAY_CLIENT_CERT",
            ),
        ),
        "tonkatsu-broker": (
            "foodtech_business_api.serve_tonkatsu_broker",
            tonkatsu_tls_paths(
                "BROKER_SERVER_CERT", "BROKER_SERVER_KEY", "BFF_CA", "BFF_CERT",
                "GATEWAY_SERVER_CERT", "BROKER_CLIENT_CERT", "BROKER_CLIENT_KEY",
            ),
        ),
    }


def tonkatsu_tls_paths(*names: str) -> dict[str, str]:
    return {f"FOODTECH_TONKATSU_{name}_PEM": f"FOODTECH_TONKATSU_{name}" for name in names}


def main() -> None:
    service = os.environ["FOODTECH_SERVICE"]
    module, paths = service_settings()[service]
    materials = {}
    for secret_name, path_name in paths.items():
        material = os.environ.pop(secret_name).encode("utf-8")
        if not material.startswith(b"-----BEGIN "):
            raise ValueError(f"Invalid TLS material: {secret_name}")
        materials[path_name] = material

    directory = tempfile.mkdtemp(prefix="foodtech-tls-")
    os.chmod(directory, 0o700)
    for path_name, material in materials.items():
        path = os.path.join(directory, path_name.lower())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(material)
        os.environ[path_name] = path
    os.execvpe("python", ["python", "-m", module], os.environ)


if __name__ == "__main__":
    main()
