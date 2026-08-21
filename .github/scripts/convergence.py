#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

from lib.config import ConfigError, compact_json, load_json, object_value, repository_root, schema_v1
from lib.github import append_outputs, append_summary
from lib.r2 import R2Client, R2Credentials, R2Error, R2PreconditionFailed, self_check as r2_self_check


PROTOCOL = "nexu-workload-result-v1"
CONTROL_SUITE = "convergence-control"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
PRODUCT_TYPES = {"job", "url"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def require_identity(value: Any, label: str) -> str:
    value = require_string(value, label)
    if not IDENTITY_RE.fullmatch(value):
        raise ConfigError(f"{label} has invalid identity {value!r}")
    return value


class Workload:
    def __init__(self, workflow: str, identity: str, raw: Any):
        value = object_value(raw, f"convergence.workflows.{workflow}.workloads.{identity}")
        expected = {"inputs", "runnerClass", "products", "reusable"}
        if set(value) != expected:
            raise ConfigError(
                f"convergence.workflows.{workflow}.workloads.{identity} keys must be {sorted(expected)}"
            )
        self.identity = require_identity(identity, f"convergence workload {workflow}")
        self.inputs = ConvergenceContract.tokens(value["inputs"], f"workload {workflow}/{identity}.inputs")
        self.runner_class = require_identity(value["runnerClass"], f"workload {workflow}/{identity}.runnerClass")
        if value["products"] not in {"none", "manifest"}:
            raise ConfigError(f"workload {workflow}/{identity}.products must be none or manifest")
        self.products = value["products"]
        if not isinstance(value["reusable"], bool):
            raise ConfigError(f"workload {workflow}/{identity}.reusable must be boolean")
        self.reusable = value["reusable"]


class WorkflowContract:
    def __init__(self, name: str, raw: Any):
        value = object_value(raw, f"convergence.workflows.{name}")
        if set(value) != {"policy", "workloads"}:
            raise ConfigError(f"convergence.workflows.{name} keys must be policy and workloads")
        self.name = require_identity(name, "convergence workflow")
        self.policy = require_identity(value["policy"], f"convergence.workflows.{name}.policy")
        workloads = object_value(value["workloads"], f"convergence.workflows.{name}.workloads")
        if not workloads:
            raise ConfigError(f"convergence.workflows.{name}.workloads must not be empty")
        self.workloads = {identity: Workload(name, identity, raw_workload) for identity, raw_workload in workloads.items()}


class ConvergenceContract:
    def __init__(self, path: Path):
        value = object_value(load_json(path), "convergence")
        if set(value) != {"schema", "suites", "workflows"}:
            raise ConfigError("convergence keys must be schema, suites, and workflows")
        schema_v1(value, "convergence")
        suites = object_value(value["suites"], "convergence.suites")
        self.suites = {
            require_identity(name, "convergence suite"): self.tokens(tokens, f"convergence.suites.{name}")
            for name, tokens in suites.items()
        }
        if CONTROL_SUITE not in self.suites:
            raise ConfigError(f"convergence.suites must define {CONTROL_SUITE}")
        workflows = object_value(value["workflows"], "convergence.workflows")
        self.workflows = {name: WorkflowContract(name, raw) for name, raw in workflows.items()}
        if not self.workflows:
            raise ConfigError("convergence.workflows must not be empty")
        self.validate_graph()

    @staticmethod
    def tokens(value: Any, label: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{label} must be a non-empty array")
        if any(not isinstance(token, str) or not token for token in value):
            raise ConfigError(f"{label} contains an invalid token")
        return value

    @staticmethod
    def validate_path(token: str, label: str) -> None:
        if token == "*":
            return
        if token.startswith(("/", "~")) or "\\" in token or "\n" in token:
            raise ConfigError(f"{label} has unsafe path token {token!r}")
        if ".." in PurePosixPath(token).parts:
            raise ConfigError(f"{label} escapes the repository: {token!r}")
        if "://" in token:
            raise ConfigError(f"{label} has unsupported token scheme: {token}")

    def workflow(self, name: str) -> WorkflowContract:
        if name not in self.workflows:
            raise ConfigError(f"unknown convergence workflow: {name}")
        return self.workflows[name]

    def validate_graph(self) -> None:
        nodes: dict[str, list[str]] = {f"suite://{name}": tokens for name, tokens in self.suites.items()}
        for workflow in self.workflows.values():
            for workload in workflow.workloads.values():
                nodes[f"workload://{workflow.name}/{workload.identity}"] = workload.inputs
        for node, tokens in nodes.items():
            for token in tokens:
                if token.startswith("suite://"):
                    if token not in nodes:
                        raise ConfigError(f"{node} references unknown {token}")
                else:
                    self.validate_path(token, node)
        visiting: list[str] = []
        complete: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ConfigError(f"convergence dependency cycle: {' -> '.join((*visiting, node))}")
            if node in complete:
                return
            visiting.append(node)
            for token in nodes[node]:
                if token.startswith("suite://"):
                    visit(token)
            visiting.pop()
            complete.add(node)

        for node in nodes:
            visit(node)

    def suite_paths(self, name: str) -> list[str]:
        if name not in self.suites:
            raise ConfigError(f"unknown convergence suite: {name}")
        paths: set[str] = set()

        def collect(suite: str) -> None:
            for token in self.suites[suite]:
                if token.startswith("suite://"):
                    collect(token.removeprefix("suite://"))
                else:
                    paths.add(token)

        collect(name)
        return sorted(paths)


class GitFingerprinter:
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, list[tuple[str, str, str, str]]] = {}

    def records(self, token: str) -> list[tuple[str, str, str, str]]:
        if token in self.cache:
            return self.cache[token]
        if token == "*":
            pathspec: list[str] = []
        elif any(character in token for character in "*?["):
            pathspec = [f":(glob){token}"]
        else:
            pathspec = [token]
        command = ["git", "ls-files", "-s", "-z"]
        if pathspec:
            command += ["--", *pathspec]
        result = subprocess.run(command, cwd=self.root, check=True, stdout=subprocess.PIPE)
        records = []
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            metadata, path = raw.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            records.append((path.decode("utf-8", "surrogateescape"), mode, oid, stage))
        candidate = self.root / token
        if not records and not any(character in token for character in "*?[") and candidate.is_file():
            oid = subprocess.run(
                ["git", "hash-object", "--", token],
                cwd=self.root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            mode = "100755" if candidate.stat().st_mode & 0o111 else "100644"
            records.append((token, mode, oid, "0"))
        records.sort()
        if not records:
            raise ConfigError(f"convergence path token matched no tracked files: {token}")
        self.cache[token] = records
        return records


def digest_tokens(
    contract: ConvergenceContract,
    fingerprinter: GitFingerprinter,
    node: str,
    tokens: list[str],
    resolved: dict[str, str],
) -> str:
    if node in resolved:
        return resolved[node]
    digest = hashlib.sha256()
    digest.update(f"{PROTOCOL}\0{node}\0".encode())
    for token in tokens:
        digest.update(f"token\0{token}\0".encode())
        if token.startswith("suite://"):
            name = token.removeprefix("suite://")
            child = digest_tokens(contract, fingerprinter, token, contract.suites[name], resolved)
            digest.update(f"digest\0{child}\0".encode())
        else:
            for path, mode, oid, stage in fingerprinter.records(token):
                digest.update(f"file\0{path}\0{mode}\0{oid}\0{stage}\0".encode("utf-8", "surrogateescape"))
    resolved[node] = digest.hexdigest()
    return resolved[node]


def calculate(
    contract: ConvergenceContract,
    root: Path,
    workflow_name: str,
    runner_plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    workflow = contract.workflow(workflow_name)
    resolved: dict[str, str] = {}
    fingerprinter = GitFingerprinter(root)
    control_digest = digest_tokens(
        contract,
        fingerprinter,
        f"suite://{CONTROL_SUITE}",
        contract.suites[CONTROL_SUITE],
        resolved,
    )
    results: dict[str, dict[str, Any]] = {}
    for identity, workload in workflow.workloads.items():
        if workload.runner_class not in runner_plan:
            raise ConfigError(f"runner plan lacks class {workload.runner_class} for {workflow_name}/{identity}")
        labels = runner_plan[workload.runner_class]
        if not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label for label in labels):
            raise ConfigError(f"runner plan class {workload.runner_class} must be a non-empty string array")
        input_digest = digest_tokens(
            contract,
            fingerprinter,
            f"workload-inputs://{workflow_name}/{identity}",
            workload.inputs,
            resolved,
        )
        execution_class = canonical_json({"runnerClass": workload.runner_class, "labels": labels})
        digest = hashlib.sha256()
        digest.update(f"{PROTOCOL}\0workload-result\0".encode())
        for value in (workflow_name, workflow.policy, identity, input_digest, control_digest, execution_class, workload.products):
            digest.update(value.encode())
            digest.update(b"\0")
        results[identity] = {
            "digest": digest.hexdigest(),
            "executionClass": json.loads(execution_class),
            "products": workload.products,
            "reusable": workload.reusable,
        }
    return results


def result_key(repository_id: int, workflow: str, policy: str, identity: str, digest: str) -> str:
    return (
        f"workload-results/v1/repos/{repository_id}/workflows/{workflow}/policies/{policy}"
        f"/workloads/{identity}/digests/{digest}.json"
    )


def product_key(
    repository_id: int,
    workflow: str,
    policy: str,
    identity: str,
    digest: str,
    product: str,
) -> str:
    return (
        f"workload-products/v1/repos/{repository_id}/workflows/{workflow}/policies/{policy}"
        f"/workloads/{identity}/digests/{digest}/products/{product}.zip"
    )


def validate_products(value: Any, label: str, require_urls: bool) -> dict[str, Any]:
    products = object_value(value, label)
    normalized: dict[str, Any] = {}
    for name, raw in sorted(products.items()):
        require_identity(name, f"{label} product")
        entry = object_value(raw, f"{label}.{name}")
        if not {"type", "source"}.issubset(entry) or set(entry) - {"type", "source", "data"}:
            raise ConfigError(f"{label}.{name} keys must be type, source, and optional data")
        product_type = require_string(entry["type"], f"{label}.{name}.type")
        if product_type not in PRODUCT_TYPES:
            raise ConfigError(f"{label}.{name}.type must be job or url")
        if require_urls and product_type != "url":
            raise ConfigError(f"{label}.{name} must be promoted to url before publication")
        source = require_string(entry["source"], f"{label}.{name}.source")
        if product_type == "url":
            parsed = urllib.parse.urlparse(source)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ConfigError(f"{label}.{name}.source must be an HTTPS URL without credentials")
        elif not IDENTITY_RE.fullmatch(source):
            raise ConfigError(f"{label}.{name}.source must name a current-run job source")
        normalized_entry: dict[str, Any] = {"type": product_type, "source": source}
        if "data" in entry:
            data = object_value(entry["data"], f"{label}.{name}.data")
            canonical_json(data)
            if "sha256" in data and (
                not isinstance(data["sha256"], str) or not DIGEST_RE.fullmatch(data["sha256"])
            ):
                raise ConfigError(f"{label}.{name}.data.sha256 must be a lowercase SHA-256 digest")
            normalized_entry["data"] = data
        normalized[name] = normalized_entry
    return normalized


def validate_result(
    value: Any,
    *,
    repository_id: int,
    workflow: WorkflowContract,
    identity: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    result = object_value(value, "workload result")
    required = {
        "schemaVersion",
        "protocol",
        "repositoryId",
        "workflow",
        "policy",
        "workload",
        "digest",
        "executionClass",
        "products",
        "validated",
    }
    if set(result) != required:
        raise ConfigError("workload result fields differ")
    checks = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "repositoryId": repository_id,
        "workflow": workflow.name,
        "policy": workflow.policy,
        "workload": identity,
        "digest": expected["digest"],
        "executionClass": expected["executionClass"],
    }
    for key, expected_value in checks.items():
        if result.get(key) != expected_value:
            raise ConfigError(f"workload result {key} mismatch")
    products = validate_products(result["products"], "workload result.products", require_urls=True)
    if expected["products"] == "none" and products:
        raise ConfigError("products:none workload result must not contain products")
    if expected["products"] == "manifest" and not products:
        raise ConfigError("products:manifest workload result must contain products")
    validated = object_value(result["validated"], "workload result.validated")
    if not isinstance(validated.get("runId"), int) or validated["runId"] <= 0:
        raise ConfigError("workload result.validated.runId must be positive")
    return {**result, "products": products}


def fetch_result(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"unexpected HTTP status {response.status}")
        if response.headers.get_content_type() not in {"application/json", "text/plain"}:
            raise OSError("unexpected workload result content type")
        body = response.read(262145)
        if len(body) > 262144:
            raise OSError("workload result exceeds 256 KiB")
        return json.loads(body)


def probe_product(url: str, timeout: float) -> None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "*/*", "Cache-Control": "no-cache", "Range": "bytes=0-0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status not in {200, 206}:
            raise OSError(f"unexpected product HTTP status {response.status}")
        response.read(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_url(url: str, timeout: float) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"Accept": "*/*", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"unexpected product HTTP status {response.status}")
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_product_archive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as input_archive:
        entries = sorted(input_archive.infolist(), key=lambda entry: entry.filename)
        if len(entries) > 10000:
            raise ConfigError("product artifact contains too many entries")
        if sum(entry.file_size for entry in entries) > 2 * 1024 * 1024 * 1024:
            raise ConfigError("product artifact expands beyond 2 GiB")
        seen: set[str] = set()
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output_archive:
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name.rstrip("/"))
                if (
                    not name
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or ".." in path.parts
                    or name in seen
                ):
                    raise ConfigError(f"product artifact has an unsafe or duplicate entry: {name!r}")
                seen.add(name)
                directory = name.endswith("/")
                normalized = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                normalized.compress_type = zipfile.ZIP_DEFLATED
                normalized.external_attr = (0o40755 if directory else 0o100644) << 16
                if directory:
                    output_archive.writestr(normalized, b"")
                    continue
                with input_archive.open(entry, "r") as input_file, output_archive.open(normalized, "w") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def resolve_results(
    base_url: str | None,
    repository_id: int,
    workflow: WorkflowContract,
    calculated: dict[str, dict[str, Any]],
    timeout: float,
) -> tuple[dict[str, bool], dict[str, str], dict[str, dict[str, Any]]]:
    hits: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}
    invalid_base_url = False
    if base_url:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            base_url = None
            invalid_base_url = True
    for identity, expected in calculated.items():
        if not expected["reusable"]:
            hits[identity] = False
            reasons[identity] = "reuse-disabled"
            continue
        if not base_url:
            hits[identity] = False
            reasons[identity] = "base-url-invalid" if invalid_base_url else "base-url-missing"
            continue
        key = result_key(repository_id, workflow.name, workflow.policy, identity, expected["digest"])
        url = f"{base_url.rstrip('/')}/{key}"
        try:
            value = fetch_result(url, timeout)
            result = validate_result(
                value,
                repository_id=repository_id,
                workflow=workflow,
                identity=identity,
                expected=expected,
            )
            for product in result["products"].values():
                declared_digest = product.get("data", {}).get("sha256")
                if declared_digest is None:
                    probe_product(product["source"], timeout)
                elif sha256_url(product["source"], timeout) != declared_digest:
                    raise ConfigError("workload result product digest mismatch")
            results[identity] = result
            hits[identity] = True
            reasons[identity] = "result-hit"
        except urllib.error.HTTPError as error:
            hits[identity] = False
            reasons[identity] = "result-missing" if error.code == 404 else f"read-http-{error.code}"
        except (ConfigError, json.JSONDecodeError, OSError, urllib.error.URLError, TimeoutError) as error:
            hits[identity] = False
            reasons[identity] = f"read-unavailable:{type(error).__name__}"
    return hits, reasons, results


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def plan_command(args: argparse.Namespace, contract: ConvergenceContract, root: Path) -> int:
    workflow = contract.workflow(args.workflow)
    scope_plan = load_json(args.scope_plan)
    enabled = object_value(scope_plan.get("enabled"), "scope plan.enabled")
    if set(enabled) != set(workflow.workloads):
        raise ConfigError(
            f"scope/convergence identity mismatch (scope={sorted(enabled)}, convergence={sorted(workflow.workloads)})"
        )
    if any(not isinstance(value, bool) for value in enabled.values()):
        raise ConfigError("scope plan.enabled values must be booleans")
    runner_plan = object_value(json.loads(args.runner_plan_json), "runner plan")
    calculated = calculate(contract, root, args.workflow, runner_plan)
    hits, read_reasons, results = resolve_results(
        args.base_url or None,
        args.repository_id,
        workflow,
        calculated,
        args.timeout,
    )
    would_run = {identity: bool(enabled[identity]) and not hits[identity] for identity in calculated}
    run = dict(would_run) if args.mode == "enforce" else {identity: bool(enabled[identity]) for identity in calculated}
    reasons = {
        identity: "scope-disabled"
        if not enabled[identity]
        else "shadow-result-hit"
        if args.mode == "shadow" and hits[identity]
        else "result-hit"
        if hits[identity]
        else read_reasons[identity]
        for identity in calculated
    }
    pending = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "repositoryId": args.repository_id,
        "repository": args.repository,
        "workflow": workflow.name,
        "policy": workflow.policy,
        "mode": args.mode,
        "workloads": {
            identity: {
                **calculated[identity],
                "scopeEnabled": bool(enabled[identity]),
                "resultHit": hits[identity],
                "run": run[identity],
                "wouldRun": would_run[identity],
                "result": results.get(identity),
            }
            for identity in calculated
        },
    }
    write_json_atomic(args.pending, pending)
    append_outputs(
        {
            "run": compact_json(run),
            "hit": compact_json(hits),
            "would_run": compact_json(would_run),
        }
    )
    lines = [
        "### Workload convergence",
        "",
        f"Mode: `{args.mode}`",
        "",
        "| Workload | Scope | Reusable | Result | Run | Reason |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for identity, value in calculated.items():
        lines.append(
            f"| {identity} | {str(bool(enabled[identity])).lower()} | {str(value['reusable']).lower()} "
            f"| {str(hits[identity]).lower()} | {str(run[identity]).lower()} | {reasons[identity]} |"
        )
    append_summary("\n".join(lines))
    print(json.dumps({"run": run, "hit": hits, "wouldRun": would_run, "reasons": reasons}, indent=2, sort_keys=True))
    return 0


def validated_provenance(value: Any) -> dict[str, Any]:
    provenance = object_value(value, "provenance")
    required = {"event", "runId", "runAttempt", "headSha", "baseSha", "treeSha", "validatedAt"}
    if set(provenance) != required:
        raise ConfigError("provenance fields differ")
    if provenance["event"] not in {"pull_request", "merge_group"}:
        raise ConfigError("provenance.event is not admissible")
    for name in ("runId", "runAttempt"):
        if not isinstance(provenance[name], int) or provenance[name] <= 0:
            raise ConfigError(f"provenance.{name} must be positive")
    for name in ("headSha", "baseSha", "treeSha"):
        if not isinstance(provenance[name], str) or not re.fullmatch(r"[0-9a-f]{40}", provenance[name]):
            raise ConfigError(f"provenance.{name} must be a lowercase SHA")
    require_string(provenance["validatedAt"], "provenance.validatedAt")
    return provenance


def finalize_command(args: argparse.Namespace, contract: ConvergenceContract) -> int:
    pending = object_value(load_json(args.pending), "pending convergence")
    workflow = contract.workflow(require_string(pending.get("workflow"), "pending workflow"))
    if pending.get("schemaVersion") != 1 or pending.get("protocol") != PROTOCOL or pending.get("policy") != workflow.policy:
        raise ConfigError("pending convergence contract differs")
    provenance = validated_provenance(load_json(args.provenance))
    workloads = object_value(pending.get("workloads"), "pending workloads")
    receipts = []
    for identity, raw in workloads.items():
        if identity not in workflow.workloads:
            raise ConfigError(f"pending convergence has unknown workload {identity}")
        value = object_value(raw, f"pending workloads.{identity}")
        if (
            not value.get("reusable")
            or not value.get("scopeEnabled")
            or not value.get("run")
            or value.get("resultHit")
        ):
            continue
        products_mode = workflow.workloads[identity].products
        if products_mode == "manifest":
            manifest_path = args.products_root / identity / "product-manifest.json"
            if not manifest_path.is_file():
                raise ConfigError(f"executed reusable product workload lacks manifest: {identity}")
            manifest = object_value(load_json(manifest_path), f"product manifest {identity}")
            products = validate_products(
                manifest.get("products"),
                f"product manifest {identity}.products",
                require_urls=False,
            )
            if manifest.get("workload") != identity or manifest.get("digest") != value.get("digest"):
                raise ConfigError(f"product manifest identity or digest mismatch: {identity}")
            if manifest.get("executionClass") != value.get("executionClass"):
                raise ConfigError(f"product manifest execution class mismatch: {identity}")
        else:
            products = {}
        receipt = {
            "schemaVersion": 1,
            "protocol": PROTOCOL,
            "repositoryId": pending["repositoryId"],
            "workflow": workflow.name,
            "policy": workflow.policy,
            "workload": identity,
            "digest": value["digest"],
            "executionClass": value["executionClass"],
            "products": products,
            "validated": provenance,
        }
        receipts.append(
            {
                "key": result_key(pending["repositoryId"], workflow.name, workflow.policy, identity, value["digest"]),
                "receipt": receipt,
            }
        )
    candidate = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "repositoryId": pending["repositoryId"],
        "repository": pending["repository"],
        "workflow": workflow.name,
        "policy": workflow.policy,
        "provenance": provenance,
        "results": receipts,
    }
    write_json_atomic(args.output, candidate)
    print(json.dumps(candidate, indent=2, sort_keys=True))
    return 0


def prepare_publication(
    candidate_path: Path,
    output_dir: Path,
    *,
    require_urls: bool = True,
) -> list[dict[str, str]]:
    candidate = object_value(load_json(candidate_path), "convergence candidate")
    if candidate.get("schemaVersion") != 1 or candidate.get("protocol") != PROTOCOL:
        raise ConfigError("convergence candidate contract differs")
    repository_id = candidate.get("repositoryId")
    if not isinstance(repository_id, int) or repository_id <= 0:
        raise ConfigError("convergence candidate repositoryId must be positive")
    workflow = require_identity(candidate.get("workflow"), "convergence candidate workflow")
    policy = require_identity(candidate.get("policy"), "convergence candidate policy")
    provenance = validated_provenance(candidate.get("provenance"))
    results = candidate.get("results")
    if not isinstance(results, list):
        raise ConfigError("convergence candidate results must be an array")
    manifest = []
    seen: set[str] = set()
    for index, raw in enumerate(results):
        item = object_value(raw, f"convergence candidate results[{index}]")
        if set(item) != {"key", "receipt"}:
            raise ConfigError("convergence candidate result keys differ")
        receipt = object_value(item["receipt"], "convergence candidate receipt")
        expected_fields = {
            "schemaVersion",
            "protocol",
            "repositoryId",
            "workflow",
            "policy",
            "workload",
            "digest",
            "executionClass",
            "products",
            "validated",
        }
        if set(receipt) != expected_fields:
            raise ConfigError("convergence candidate receipt fields differ")
        identity = require_identity(receipt.get("workload"), "receipt workload")
        digest = require_string(receipt.get("digest"), "receipt digest")
        if not DIGEST_RE.fullmatch(digest):
            raise ConfigError("receipt digest must be sha256")
        expected_key = result_key(repository_id, workflow, policy, identity, digest)
        if item["key"] != expected_key or expected_key in seen:
            raise ConfigError("convergence candidate result key mismatch or duplicate")
        if receipt.get("schemaVersion") != 1 or receipt.get("protocol") != PROTOCOL:
            raise ConfigError("receipt protocol differs")
        if receipt.get("repositoryId") != repository_id or receipt.get("workflow") != workflow or receipt.get("policy") != policy:
            raise ConfigError("receipt identity differs from candidate")
        validate_products(receipt.get("products"), "receipt.products", require_urls=require_urls)
        if validated_provenance(receipt.get("validated")) != provenance:
            raise ConfigError("receipt provenance differs from candidate")
        seen.add(expected_key)
        path = output_dir / f"{identity}-{digest}.json"
        write_json_atomic(path, receipt)
        manifest.append({"key": expected_key, "file": str(path)})
    return manifest


def prepare_publication_command(args: argparse.Namespace) -> int:
    manifest = prepare_publication(args.candidate, args.output_dir)
    print(json.dumps({"results": manifest}, indent=2, sort_keys=True))
    return 0


def product_sources_command(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as temporary:
        manifest = prepare_publication(args.candidate, Path(temporary), require_urls=False)
        sources: set[str] = set()
        for item in manifest:
            receipt = object_value(load_json(Path(item["file"])), "convergence candidate receipt")
            products = validate_products(receipt.get("products"), "receipt.products", require_urls=False)
            sources.update(entry["source"] for entry in products.values() if entry["type"] == "job")
    print(json.dumps(sorted(sources)))
    return 0


def public_origin(value: str) -> str:
    parsed = urllib.parse.urlparse(value.rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ConfigError("public origin must be HTTPS without credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("public origin must not contain query or fragment")
    return value.rstrip("/")


def existing_receipt(url: str, timeout: float) -> Any | None:
    try:
        return fetch_result(url, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def same_reusable_result(existing: Any, expected: Any) -> bool:
    if not isinstance(existing, dict) or not isinstance(expected, dict):
        return False
    try:
        validated_provenance(existing.get("validated"))
    except ConfigError:
        return False
    return canonical_json({key: value for key, value in existing.items() if key != "validated"}) == canonical_json(
        {key: value for key, value in expected.items() if key != "validated"}
    )


def self_check() -> None:
    workflow = WorkflowContract.__new__(WorkflowContract)
    workflow.name = "ci"
    workflow.policy = "self-check-v1"
    expected = {
        "digest": "d" * 64,
        "executionClass": {"runnerClass": "worker", "labels": ["test-runner"]},
        "products": "none",
        "reusable": True,
    }
    provenance = {
        "event": "pull_request",
        "runId": 1,
        "runAttempt": 1,
        "headSha": "a" * 40,
        "baseSha": "b" * 40,
        "treeSha": "c" * 40,
        "validatedAt": "2026-08-21T00:00:00Z",
    }
    receipt = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "repositoryId": 42,
        "workflow": "ci",
        "policy": "self-check-v1",
        "workload": "unit",
        "digest": expected["digest"],
        "executionClass": expected["executionClass"],
        "products": {},
        "validated": provenance,
    }
    module = sys.modules[__name__]
    with patch.object(module, "fetch_result", return_value=receipt):
        hits, _, _ = resolve_results("https://results.example", 42, workflow, {"unit": expected}, 0.1)
        if hits != {"unit": True}:
            raise ConfigError("convergence self-check did not accept a valid result")
    with patch.object(module, "fetch_result", return_value={}):
        hits, _, _ = resolve_results("https://results.example", 42, workflow, {"unit": expected}, 0.1)
        if hits != {"unit": False}:
            raise ConfigError("convergence self-check accepted a malformed result")
    with patch.object(module, "fetch_result", side_effect=TimeoutError()):
        hits, _, _ = resolve_results("https://results.example", 42, workflow, {"unit": expected}, 0.1)
        if hits != {"unit": False}:
            raise ConfigError("convergence self-check did not fail open on timeout")
    product_expected = {**expected, "products": "manifest"}
    product_receipt = json.loads(canonical_json(receipt))
    product_receipt["products"] = {
        "bundle": {"type": "url", "source": "https://results.example/bundle.zip"}
    }
    with (
        patch.object(module, "fetch_result", return_value=product_receipt),
        patch.object(module, "probe_product", side_effect=TimeoutError()),
    ):
        hits, _, _ = resolve_results(
            "https://results.example",
            42,
            workflow,
            {"unit": product_expected},
            0.1,
        )
        if hits != {"unit": False}:
            raise ConfigError("convergence self-check accepted an unavailable product set")
    hits, reasons, _ = resolve_results("not-a-url", 42, workflow, {"unit": expected}, 0.1)
    if hits != {"unit": False} or reasons != {"unit": "base-url-invalid"}:
        raise ConfigError("convergence self-check did not fail open on an invalid base URL")
    repeated = json.loads(canonical_json(receipt))
    repeated["validated"]["runId"] = 2
    if not same_reusable_result(receipt, repeated):
        raise ConfigError("convergence self-check rejected an idempotent repeated result")
    repeated["executionClass"]["labels"] = ["different-runner"]
    if same_reusable_result(receipt, repeated):
        raise ConfigError("convergence self-check accepted a nondeterministic repeated result")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = root / "first.zip"
        second = root / "second.zip"
        with zipfile.ZipFile(first, "w") as archive:
            archive.writestr("b.txt", "b")
            archive.writestr("a.txt", "a")
        with zipfile.ZipFile(second, "w") as archive:
            archive.writestr("a.txt", "a")
            archive.writestr("b.txt", "b")
        first_normalized = root / "first-normalized.zip"
        second_normalized = root / "second-normalized.zip"
        normalize_product_archive(first, first_normalized)
        normalize_product_archive(second, second_normalized)
        if sha256_file(first_normalized) != sha256_file(second_normalized):
            raise ConfigError("convergence self-check produced nondeterministic product archives")


def publish_command(args: argparse.Namespace) -> int:
    origin = public_origin(args.public_origin)
    access_key_id = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
    secret_access_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")
    session_token = os.environ.get("CLOUDFLARE_R2_SESSION_TOKEN")
    client = R2Client(
        endpoint=args.endpoint,
        bucket=args.bucket,
        credentials=R2Credentials(access_key_id, secret_access_key, session_token),
        timeout=args.timeout,
    )
    with tempfile.TemporaryDirectory() as temporary:
        prepare_publication(args.candidate, Path(temporary), require_urls=False)
    candidate = object_value(load_json(args.candidate), "convergence candidate")
    repository_id = candidate["repositoryId"]
    workflow = candidate["workflow"]
    policy = candidate["policy"]
    promoted_products = 0
    for item in candidate["results"]:
        receipt = item["receipt"]
        identity = receipt["workload"]
        digest = receipt["digest"]
        products = validate_products(receipt["products"], "receipt.products", require_urls=False)
        for name, product in products.items():
            if product["type"] != "job":
                continue
            source = product["source"]
            source_archive = args.products_root / f"{source}.zip"
            if not source_archive.is_file():
                raise ConfigError(f"current-run product artifact is missing: {source}")
            archive = args.output_dir / "products" / f"{identity}-{name}.zip"
            normalize_product_archive(source_archive, archive)
            key = product_key(repository_id, workflow, policy, identity, digest, name)
            content_digest = sha256_file(archive)
            data = dict(product.get("data", {}))
            declared_digest = data.get("sha256")
            if declared_digest is not None and declared_digest != content_digest:
                raise ConfigError(f"declared product digest differs from artifact: {identity}/{name}")
            data["sha256"] = content_digest
            try:
                client.put_file(
                    key=key,
                    file=archive,
                    content_type="application/zip",
                )
            except R2PreconditionFailed:
                if sha256_url(f"{origin}/{key}", args.timeout) != content_digest:
                    raise ConfigError(f"immutable workload product collision: {key}")
            promoted = {"type": "url", "source": f"{origin}/{key}"}
            promoted["data"] = data
            receipt["products"][name] = promoted
            promoted_products += 1
    promoted_candidate = args.output_dir / "promoted-candidate.json"
    write_json_atomic(promoted_candidate, candidate)
    manifest = prepare_publication(promoted_candidate, args.output_dir)
    published = 0
    unchanged = 0
    for item in manifest:
        key = item["key"]
        file = Path(item["file"])
        receipt = load_json(file)
        url = f"{origin}/{key}"
        existing = existing_receipt(url, args.timeout)
        if existing is not None:
            if not same_reusable_result(existing, receipt):
                raise ConfigError(f"immutable workload result collision: {key}")
            unchanged += 1
            continue
        try:
            client.put_file(key=key, file=file)
            published += 1
        except R2PreconditionFailed:
            raced = existing_receipt(url, args.timeout)
            if raced is None or not same_reusable_result(raced, receipt):
                raise ConfigError(f"immutable workload result publication race differs: {key}")
            unchanged += 1
    print(
        json.dumps(
            {"promotedProducts": promoted_products, "published": published, "unchanged": unchanged},
            sort_keys=True,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan and publish reusable workload results.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("control-paths")
    plan = sub.add_parser("github-output")
    plan.add_argument("--workflow", required=True)
    plan.add_argument("--scope-plan", type=Path, required=True)
    plan.add_argument("--runner-plan-json", required=True)
    plan.add_argument("--repository-id", type=int, required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--base-url", default="")
    plan.add_argument("--timeout", type=float, default=2.0)
    plan.add_argument("--mode", choices=["shadow", "enforce"], default="shadow")
    plan.add_argument("--pending", type=Path, required=True)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--pending", type=Path, required=True)
    finalize.add_argument("--provenance", type=Path, required=True)
    finalize.add_argument("--products-root", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    publication = sub.add_parser("prepare-publication")
    publication.add_argument("--candidate", type=Path, required=True)
    publication.add_argument("--output-dir", type=Path, required=True)
    sources = sub.add_parser("product-sources")
    sources.add_argument("--candidate", type=Path, required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--candidate", type=Path, required=True)
    publish.add_argument("--output-dir", type=Path, required=True)
    publish.add_argument("--endpoint", required=True)
    publish.add_argument("--bucket", required=True)
    publish.add_argument("--public-origin", required=True)
    publish.add_argument("--products-root", type=Path, required=True)
    publish.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve() if args.root else repository_root(__file__)
    if args.command == "prepare-publication":
        return prepare_publication_command(args)
    if args.command == "product-sources":
        return product_sources_command(args)
    if args.command == "publish":
        return publish_command(args)
    contract = ConvergenceContract(args.config or root / ".github/config/convergence.json")
    if args.command == "control-paths":
        print("\n".join(contract.suite_paths(CONTROL_SUITE)))
        return 0
    if args.command == "validate":
        r2_self_check()
        self_check()
        runner_classes = {
            workload.runner_class
            for workflow in contract.workflows.values()
            for workload in workflow.workloads.values()
        }
        runner_plan = {runner_class: [f"validation-{runner_class}"] for runner_class in runner_classes}
        for workflow in contract.workflows:
            calculate(contract, root, workflow, runner_plan)
        print("convergence configuration is valid")
        return 0
    if args.command == "github-output":
        return plan_command(args, contract, root)
    return finalize_command(args, contract)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, R2Error, json.JSONDecodeError, subprocess.SubprocessError, OSError) as error:
        print(f"convergence error: {error}", file=sys.stderr)
        raise SystemExit(2)
