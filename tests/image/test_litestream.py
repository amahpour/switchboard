"""The shipped Litestream sidecar restores the broker from MinIO (#689, DESIGN.md §57.2).

Opt-in with the existing image marker. CI provisions the pinned images; local runs never pull.
The restore probe waits for the expected rows in S3 instead of assuming a sync delay has elapsed.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from pathlib import Path

import pytest
import yaml
from test_image import docker, docker_missing, wait_for
from test_image import image as image  # noqa: F401 (shared image fixture)

ROOT = Path(__file__).resolve().parents[2]
# Built in CI from pinned upstream server/client commits; no dependency on withdrawn registry images.
MINIO = "switchboard:litestream-minio"
MC = MINIO
pytestmark = pytest.mark.image
DB_PATH = "/data/switchboard/switchboard.db"


def test_replication_survives_migration_checkpoints_and_volume_loss(image: str, tmp_path: Path) -> None:
    """Replicate the release fixture while the broker migrates it, write rooms, lose the volume,
    restore and open the new schema. Also checks existing-DB and empty-replica restore guards.
    A missing WAL transaction, wrong path, uid, config, image or credential breaks this drill.
    """
    if reason := docker_missing():
        pytest.skip(reason)
    values = yaml.safe_load((ROOT / "deploy/helm/switchboard/values.yaml").read_text())
    litestream = values["backup"]["litestream"]["image"]
    for ref in (litestream, MINIO, MC):
        if docker("image", "inspect", ref, check=False).returncode:
            if os.environ.get("CI"):
                pytest.fail("CI must provision Litestream and build the test-only MinIO image")
            pytest.skip("Litestream or test-only MinIO image not cached; this test never pulls images")

    # Consume the actual shipped config and container definitions, not a test-only lookalike.
    example = (
        (ROOT / "deploy/kubernetes/switchboard.yaml")
        .read_text()
        .split("# BEGIN LITESTREAM EXAMPLE\n", 1)[1]
        .split("# END LITESTREAM EXAMPLE", 1)[0]
    )
    docs = {
        d["kind"]: d
        for d in yaml.safe_load_all("\n".join(line.removeprefix("# ") for line in example.splitlines()))
        if d
    }
    pod = docs["StatefulSet"]["spec"]["template"]["spec"]
    (sidecar,) = pod["containers"]
    (init,) = pod["initContainers"]
    config = yaml.safe_load(docs["ConfigMap"]["data"]["litestream.yml"])
    config["dbs"][0]["replica"]["endpoint"] = "http://minio:9000"
    config_path = tmp_path / "litestream.yml"
    config_path.write_text(yaml.safe_dump(config))
    config_path.chmod(0o644)
    tag = "sbimg-litestream-" + uuid.uuid4().hex[:8]
    net, volume, recovered = tag + "-net", tag + "-data", tag + "-restored"
    broker, replica, minio = tag + "-broker", tag + "-replica", tag + "-minio"
    task = tag + "-task"
    started: list[str] = [task]
    credentials = [
        "-e",
        "AWS_ACCESS_KEY_ID=switchboard-test",
        "-e",
        "AWS_SECRET_ACCESS_KEY=switchboard-test-secret",
    ]

    def once(*args: str, check: bool = True, stdin: str | None = None):
        return docker(
            "run",
            "--rm",
            "--pull=never",
            "--name",
            task,
            "--label",
            "switchboard-selftest",
            *args,
            check=check,
            stdin=stdin,
        )

    def ls_args(vol: str, network: str = net) -> list[str]:
        return [
            "--network",
            network,
            "--user",
            "10001:10001",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{vol}:/data",
            "-v",
            f"{config_path}:/etc/litestream.yml:ro",
            *credentials,
        ]

    def cli(*args: str) -> str:
        return docker("exec", "--user", "10001:10001", broker, "switchboard", *args).stdout

    state_script = (
        "import json; from switchboard import db; "
        f"c=db.connect({DB_PATH!r},readonly=True); "
        "print(json.dumps({'schema':db.schema_version(c),'counts':db.row_counts(c,db.tables_of(db.SCHEMA_VERSION)),"
        "'integrity':db.integrity_ok(c)})); c.close()"
    )

    def remote_scalar(query: str) -> str:
        result = docker(
            "exec",
            replica,
            "/bin/sh",
            "-ec",
            "rm -f /tmp/probe.db; litestream restore -config /etc/litestream.yml "
            "-o /tmp/probe.db " + DB_PATH + "; sqlite3 /tmp/probe.db " + shlex.quote(query),
            check=False,
        )
        lines = result.stdout.strip().splitlines()
        return lines[-1] if result.returncode == 0 and lines else ""

    try:
        docker("network", "create", net)
        for vol in (volume, recovered):
            docker("volume", "create", vol)
            once(
                "-v",
                f"{vol}:/data",
                "--entrypoint",
                "sh",
                image,
                "-ec",
                "chown 0:10001 /data; chmod 2770 /data",
            )
        started.append(minio)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--label",
            "switchboard-selftest",
            "--name",
            minio,
            "--network",
            net,
            "--network-alias",
            "minio",
            "-e",
            "MINIO_ROOT_USER=switchboard-test",
            "-e",
            "MINIO_ROOT_PASSWORD=switchboard-test-secret",
            MINIO,
            "server",
            "/data",
        )
        wait_for(
            "MinIO bucket creation",
            lambda: (
                once(
                    "--network",
                    net,
                    "--entrypoint",
                    "/bin/sh",
                    MC,
                    "-ec",
                    "mc alias set replica http://minio:9000 "
                    "switchboard-test switchboard-test-secret >/dev/null; "
                    "mc mb --ignore-existing replica/switchboard-backups",
                    check=False,
                ).returncode
                == 0
            ),
        )
        # The shipped init step on a genuinely empty replica permits first install, creating a private home.
        once(
            *ls_args(recovered),
            "--entrypoint",
            init["command"][0],
            litestream,
            *init["command"][1:],
            *init["args"],
        )
        fixture = (ROOT / "tests/fixtures/db/v0_37_2.sql").read_text()
        seed = (
            "import os, pathlib, sqlite3\n"
            "h=pathlib.Path('/data/switchboard'); h.mkdir(mode=0o700)\n"
            f"c=sqlite3.connect({DB_PATH!r}); c.executescript({fixture!r}); c.close()\n"
            "os.chown(h,10001,10001)\n"
            "for p in h.iterdir(): os.chown(p,10001,10001); p.chmod(0o600)\n"
        )
        once(
            "-i",
            "-v",
            f"{volume}:/data",
            "--entrypoint",
            "/opt/switchboard/bin/python",
            image,
            "-",
            stdin=seed,
        )
        started.append(replica)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--label",
            "switchboard-selftest",
            "--name",
            replica,
            *ls_args(volume),
            "--entrypoint",
            sidecar["command"][0],
            litestream,
            *sidecar["command"][1:],
            *sidecar["args"],
        )
        wait_for(
            "Litestream watching the old schema", lambda: "initialized db" in docker("logs", replica).stdout
        )
        wait_for(
            "old schema in the S3 replica",
            lambda: remote_scalar("SELECT value FROM meta WHERE key='schema_version'") == "28",
            timeout=90,
        )
        started.append(broker)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--label",
            "switchboard-selftest",
            "--name",
            broker,
            "--network",
            net,
            "--user",
            "10001:10001",
            "-v",
            f"{volume}:/data",
            "-e",
            "SWITCHBOARD_PUBLIC_URL=https://sb.example.com",
            image,
        )
        wait_for("broker after migration", lambda: "broker up" in docker("logs", broker).stdout)
        assert "schema migration" in docker("logs", broker).stdout
        for name in ("#replicated-one", "#replicated-two"):
            cli("create", name)
            cli("say", name, "a committed message for the recovery drill")
        # Exercise PASSIVE checkpointing and shutdown TRUNCATE with replication active.
        docker(
            "exec",
            broker,
            "/opt/switchboard/bin/python",
            "-c",
            f"from switchboard import db; c=db.connect({DB_PATH!r}); db.checkpoint(c); c.close()",
        )
        docker("stop", "-t", "40", broker)
        assert docker("inspect", "-f", "{{.State.ExitCode}}", broker).stdout.strip() == "0"
        # Read the stopped source so background housekeeping cannot race the expected counts.
        expected = json.loads(
            once(
                "--user",
                "10001:10001",
                "-v",
                f"{volume}:/data",
                "--entrypoint",
                "/opt/switchboard/bin/python",
                image,
                "-c",
                state_script,
            ).stdout
        )
        assert expected["integrity"] is None

        # Restore probes are bounded event polling: wait until the remote replica contains all rows.
        wait_for(
            "both new rooms in the S3 replica",
            lambda: (
                remote_scalar(
                    "SELECT COUNT(*) FROM rooms WHERE name IN ('#replicated-one','#replicated-two')"
                )
                == "2"
            ),
            timeout=90,
        )
        docker("stop", "-t", "40", replica)
        assert docker("inspect", "-f", "{{.State.ExitCode}}", replica).stdout.strip() == "0"
        # Remove the original volume: recovery now has only S3 and the already-empty target volume.
        docker("rm", broker, replica)
        docker("volume", "rm", volume)
        once(
            *ls_args(recovered),
            "--entrypoint",
            init["command"][0],
            litestream,
            *init["command"][1:],
            *init["args"],
        )
        # With a DB present the init step must succeed even when the remote store is unreachable.
        once(
            *ls_args(recovered, network="none"),
            "--entrypoint",
            init["command"][0],
            litestream,
            *init["command"][1:],
            *init["args"],
        )
        # Inspect before startup's transient-state cleanup; opening via db.open_db must need no migration.
        docker(
            "run",
            "-d",
            "--pull=never",
            "--label",
            "switchboard-selftest",
            "--name",
            broker,
            "--user",
            "10001:10001",
            "-v",
            f"{recovered}:/data",
            "--entrypoint",
            "sleep",
            image,
            "infinity",
        )
        actual = json.loads(docker("exec", broker, "/opt/switchboard/bin/python", "-c", state_script).stdout)
        assert actual == expected
        docker(
            "exec",
            broker,
            "/opt/switchboard/bin/python",
            "-c",
            "from switchboard import db; c=db.open_db('" + DB_PATH + "'); c.close()",
        )
        docker("rm", "-f", broker)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--label",
            "switchboard-selftest",
            "--name",
            broker,
            "--user",
            "10001:10001",
            "-v",
            f"{recovered}:/data",
            "-e",
            "SWITCHBOARD_PUBLIC_URL=https://sb.example.com",
            image,
        )
        wait_for("recovered broker", lambda: "broker up" in docker("logs", broker).stdout)
        assert "#replicated-one" in cli("rooms") and "#replicated-two" in cli("rooms")
        assert "schema migration" not in docker("logs", broker).stdout
        print("LITESTREAM: empty replica, migration, checkpoints, volume loss, restore and row counts passed")
    finally:
        for name in reversed(started):
            docker("rm", "-f", name, check=False)
        for vol in (volume, recovered):
            docker("volume", "rm", vol, check=False)
        docker("network", "rm", net, check=False)
