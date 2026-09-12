#!/usr/bin/env python3
"""Staged, health-gated rolling reboot of Kubernetes nodes running as Proxmox VMs.

Replaces kured. The design goal is that a run either completes cleanly or stops
early leaving the cluster up -- never "keep going and hope".

Per node, in sequence:
  1. skip unless /var/run/reboot-required exists on the host
  2. cordon + drain (a failed drain aborts the run; it is never forced past)
  3. reboot, escalating only on failure:
       a. systemctl reboot in-guest   (graceful, clean unmount)
       b. qm shutdown  via Proxmox    (ACPI)
       c. qm stop + qm start          (hard power cycle -- last resort)
  4. wait for a *clean* return: Ready + Longhorn engine + CSI registered
  5. uncordon, then wait for Longhorn to be fully healthy before the next node

Host access is via a short-lived privileged pod pinned to the node with its root
filesystem at /host, so no SSH is required -- SSH is exactly what is unavailable
when a node wedges. The pod tolerates all taints, since by the time we run on a
node it is already cordoned.

Everything mutating is gated on --dry-run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- tunables -----------------------------------------------------------------
DRAIN_TIMEOUT = 900          # 15m; generous, Longhorn detach can be slow
GRACEFUL_DOWN_WAIT = 300     # 5m for in-guest reboot to take the node down
ACPI_DOWN_WAIT = 300         # 5m more after qm shutdown
HARD_DOWN_WAIT = 120         # after qm stop
RETURN_TIMEOUT = 900         # 15m to come back fully healthy
SETTLE_TIMEOUT = 3600        # 60m for Longhorn to finish rebuilding
POLL = 15
DEBUG_IMAGE = "busybox:1.37"


class Abort(Exception):
    """Stop the run. Remaining nodes are left untouched."""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# --- kubectl ------------------------------------------------------------------
def kubectl(*args: str, check: bool = True, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise Abort(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def kubectl_json(*args: str) -> dict:
    return json.loads(kubectl(*args, "-o", "json"))


def node_ready(name: str) -> bool:
    try:
        out = kubectl(
            "get", "node", name, "-o",
            'jsonpath={range .status.conditions[?(@.type=="Ready")]}{.status}{end}',
            check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return out.strip() == "True"


def _host_pod_spec(node: str, name: str, script: str) -> dict:
    """Privileged pod pinned to one node with its root filesystem at /host.

    Built explicitly rather than via `kubectl debug` so the pod name is
    deterministic (cleanup) and the tolerations are right -- the node is
    cordoned by the time we run on it, and may be tainted unreachable.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {
            "nodeName": node,
            "hostPID": True,          # nsenter into PID 1 for systemctl
            "hostNetwork": True,
            "restartPolicy": "Never",
            "tolerations": [{"operator": "Exists"}],  # cordoned/tainted nodes
            "containers": [{
                "name": "rebootctl",
                "image": DEBUG_IMAGE,
                "command": ["sh", "-c", script],
                "securityContext": {"privileged": True},
                "volumeMounts": [{"name": "host", "mountPath": "/host"}],
            }],
            "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
        },
    }


def run_on_host(node: str, script: str, timeout: int = 120) -> tuple[bool, str]:
    """Run a shell snippet against the node's host filesystem.

    Returns (ok, stdout). Never raises -- an unreachable node is a normal
    outcome the caller decides how to treat.
    """
    name = f"rebootctl-{node}-{int(time.time())}"
    spec = json.dumps(_host_pod_spec(node, name, script))
    try:
        create = subprocess.run(
            ["kubectl", "apply", "-n", "default", "-f", "-"],
            input=spec, capture_output=True, text=True, timeout=60,
        )
        if create.returncode != 0:
            return False, create.stderr

        deadline = time.time() + timeout
        while time.time() < deadline:
            phase = subprocess.run(
                ["kubectl", "get", "pod", "-n", "default", name,
                 "-o", "jsonpath={.status.phase}"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if phase in ("Succeeded", "Failed"):
                logs = subprocess.run(
                    ["kubectl", "logs", "-n", "default", name],
                    capture_output=True, text=True, timeout=30,
                ).stdout
                return phase == "Succeeded", logs
            time.sleep(2)
        return False, ""
    except subprocess.TimeoutExpired:
        return False, ""
    finally:
        subprocess.run(["kubectl", "delete", "pod", "-n", "default", name,
                        "--wait=false", "--ignore-not-found"],
                       capture_output=True, timeout=30)


def reboot_required(node: str) -> bool | None:
    """True/False, or None if the host could not be read."""
    ok, out = run_on_host(
        node, "test -f /host/var/run/reboot-required && echo YES || echo NO"
    )
    if not ok and "YES" not in out and "NO" not in out:
        return None
    return "YES" in out


def reboot_reason(node: str) -> str:
    _, out = run_on_host(node, "cat /host/var/run/reboot-required.pkgs 2>/dev/null")
    pkgs = [p for p in out.split() if p]
    return ", ".join(pkgs[:8]) + ("..." if len(pkgs) > 8 else "") if pkgs else "unknown"


# --- Longhorn -----------------------------------------------------------------
def longhorn_state() -> tuple[int, int, int]:
    """(degraded, rebuilding, faulted). Longhorn absent -> all zero."""
    try:
        vols = kubectl_json("get", "volumes.longhorn.io", "-n", "longhorn-system")
    except Abort:
        return (0, 0, 0)
    degraded = faulted = 0
    for v in vols.get("items", []):
        r = v.get("status", {}).get("robustness")
        if r == "degraded":
            degraded += 1
        elif r == "faulted":
            faulted += 1
    rebuilding = 0
    try:
        engines = kubectl_json("get", "engines.longhorn.io", "-n", "longhorn-system")
        for e in engines.get("items", []):
            if e.get("status", {}).get("rebuildStatus"):
                rebuilding += 1
    except Abort:
        pass
    return (degraded, rebuilding, faulted)


def wait_longhorn_settled(timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        degraded, rebuilding, faulted = longhorn_state()
        if faulted:
            raise Abort(f"{faulted} Longhorn volume(s) FAULTED -- stopping")
        if degraded == 0 and rebuilding == 0:
            return
        log(f"    Longhorn settling: degraded={degraded} rebuilding={rebuilding}")
        time.sleep(30)
    raise Abort(f"Longhorn did not settle within {timeout}s")


# --- Proxmox ------------------------------------------------------------------
@dataclass
class PveHost:
    name: str
    endpoint: str
    token_id: str
    token_secret: str
    verify_tls: bool = False
    pve_node: str = ""
    reachable: bool = True

    def call(self, path: str, method: str = "GET", data: dict | None = None) -> dict:
        url = f"{self.endpoint.rstrip('/')}/api2/json{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization",
                       f"PVEAPIToken={self.token_id}={self.token_secret}")
        ctx = None
        if url.startswith("https") and not self.verify_tls:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return json.loads(resp.read()).get("data")


@dataclass
class VmRef:
    host: PveHost
    vmid: int
    name: str
    smbios_uuid: str = ""


@dataclass
class Plan:
    nodes: list[str] = field(default_factory=list)
    vms: dict[str, VmRef] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)


def load_hosts() -> list[PveHost]:
    """Build the host list from per-host env vars.

    PVE_TOKEN_ID / PVE_SECRET, PVE2_TOKEN_ID / PVE2_SECRET, ... through
    PVE{PVE_HOST_COUNT}. Endpoints default to https://<prefix>.<PVE_DOMAIN>
    (pve.example.net, pve2.example.net, ...) and can be overridden per host
    with PVE{n}_ENDPOINT.

    A host with no credentials is skipped rather than failing the run -- the
    k8s nodes on it simply become unmapped, and unmapped nodes are never
    touched.
    """
    count = int(os.environ.get("PVE_HOST_COUNT", "7"))
    domain = os.environ.get("PVE_DOMAIN", "mfaherty.net").strip().lstrip(".")
    verify_tls = os.environ.get("PVE_VERIFY_TLS", "").lower() == "true"

    hosts, skipped = [], []
    for i in range(1, count + 1):
        prefix = "PVE" if i == 1 else f"PVE{i}"
        token_id = os.environ.get(f"{prefix}_TOKEN_ID", "").strip()
        secret = os.environ.get(f"{prefix}_SECRET", "").strip()
        if not token_id or not secret:
            skipped.append(prefix)
            continue
        endpoint = os.environ.get(
            f"{prefix}_ENDPOINT", f"https://{prefix.lower()}.{domain}"
        ).strip()
        hosts.append(PveHost(
            name=prefix,
            endpoint=endpoint,
            token_id=token_id,
            token_secret=secret,
            verify_tls=verify_tls,
        ))

    if skipped:
        log(f"  note: no credentials for {skipped} -- those hosts are skipped")
    if not hosts:
        raise Abort(
            "no Proxmox credentials found; expected PVE_TOKEN_ID/PVE_SECRET "
            f"through PVE{count}_TOKEN_ID/PVE{count}_SECRET"
        )
    return hosts


def discover(hosts: list[PveHost], node_names: list[str]) -> Plan:
    """Map k8s node names to Proxmox VMs across standalone hosts."""
    plan = Plan(nodes=node_names)
    by_name: dict[str, list[VmRef]] = {}
    for h in hosts:
        try:
            # Resolve the PVE node name explicitly. Inferring it from a VM record
            # silently fails on a host with no matching VMs.
            nodes = h.call("/nodes") or []
            if not nodes:
                raise RuntimeError("/nodes returned nothing")
            h.pve_node = nodes[0]["node"]

            for res in h.call("/cluster/resources?type=vm") or []:
                vm_name = res.get("name", "")
                if vm_name in node_names:
                    by_name.setdefault(vm_name, []).append(
                        VmRef(host=h, vmid=int(res["vmid"]), name=vm_name)
                    )
        except Exception as exc:  # noqa: BLE001 - any failure means "unusable host"
            h.reachable = False
            log(f"  WARNING: Proxmox host {h.name} unreachable: {exc}")

    for name in node_names:
        found = by_name.get(name, [])
        if len(found) == 1:
            plan.vms[name] = found[0]
        else:
            plan.unmapped.append(name)
            if len(found) > 1:
                where = ", ".join(f"{v.host.name}:{v.vmid}" for v in found)
                log(f"  WARNING: {name} matches MULTIPLE VMs ({where}) -- refusing")
    return plan


def assert_identity(node: str, vm: VmRef) -> None:
    """Confirm the VM really is this node before doing anything destructive.

    Names match today, so this should never fire -- which is what makes it a
    useful canary rather than dead weight.
    """
    cfg = vm.host.call(f"/nodes/{vm.host.pve_node}/qemu/{vm.vmid}/config") or {}
    smbios = cfg.get("smbios1", "")
    uuid = ""
    for part in smbios.split(","):
        if part.startswith("uuid="):
            uuid = part[5:].strip().lower()
    expected = kubectl(
        "get", "node", node, "-o", "jsonpath={.status.nodeInfo.systemUUID}"
    ).strip().lower()
    if uuid and expected and uuid != expected:
        raise Abort(
            f"IDENTITY MISMATCH for {node}: Proxmox {vm.host.name}:{vm.vmid} "
            f"smbios uuid={uuid} but node systemUUID={expected}. Refusing to act."
        )
    if not uuid:
        log(f"    note: {vm.host.name}:{vm.vmid} has no smbios uuid; name match only")


# --- per-node reboot ----------------------------------------------------------
def wait_down(node: str, vm: VmRef, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not node_ready(node):
            try:
                st = vm.host.call(
                    f"/nodes/{vm.host.pve_node}/qemu/{vm.vmid}/status/current"
                ) or {}
                if st.get("status") == "stopped":
                    return True
            except Exception:  # noqa: BLE001
                pass
            return True  # NotReady is enough to consider it going down
        time.sleep(POLL)
    return False


def wait_clean_return(node: str, timeout: int) -> None:
    """Ready is not enough -- CSI registers ~60s later and mounts fail in the gap."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if node_ready(node):
            pods = kubectl(
                "get", "pods", "-n", "longhorn-system", "--field-selector",
                f"spec.nodeName={node}", "--no-headers", check=False, timeout=30,
            )
            has_im = any("instance-manager" in l and "Running" in l
                         for l in pods.splitlines())
            has_csi = any("csi-plugin" in l and "Running" in l and "2/2" in l
                          for l in pods.splitlines())
            if has_im and has_csi:
                log(f"    {node} returned clean (Ready + Longhorn engine + CSI)")
                return
            log(f"    {node} Ready; waiting for storage (engine={has_im} csi={has_csi})")
        time.sleep(POLL)
    raise Abort(f"{node} did not return healthy within {timeout}s")


def reboot_node(node: str, vm: VmRef, dry_run: bool) -> None:
    base = f"/nodes/{vm.host.pve_node}/qemu/{vm.vmid}"

    if dry_run:
        log(f"    DRY-RUN: would reboot {node} ({vm.host.name}:{vm.vmid})")
        return

    # Rung 1 -- graceful, in-guest. Clean unmount of Longhorn replicas.
    log("    rung 1/3: systemctl reboot (in-guest)")
    run_on_host(node, "nsenter -t 1 -m -u -i -n -p -- systemctl reboot", timeout=60)
    if wait_down(node, vm, GRACEFUL_DOWN_WAIT):
        return

    # Rung 2 -- ACPI via the hypervisor. Guest still chooses how to stop.
    log("    rung 2/3: qm shutdown (ACPI)")
    vm.host.call(f"{base}/status/shutdown", method="POST")
    if wait_down(node, vm, ACPI_DOWN_WAIT):
        return

    # Rung 3 -- hard power cycle. Unclean; only because the node is stuck.
    log("    rung 3/3: qm stop + start (HARD power cycle)")
    assert_identity(node, vm)
    vm.host.call(f"{base}/status/stop", method="POST")
    time.sleep(HARD_DOWN_WAIT)
    vm.host.call(f"{base}/status/start", method="POST")


def process_node(node: str, vm: VmRef, dry_run: bool) -> None:
    reason = reboot_reason(node)
    log(f"  {node}: reboot required ({reason})")

    degraded, rebuilding, faulted = longhorn_state()
    if faulted or degraded or rebuilding:
        raise Abort(
            f"cluster not healthy before {node}: degraded={degraded} "
            f"rebuilding={rebuilding} faulted={faulted}"
        )

    assert_identity(node, vm)

    if dry_run:
        log(f"    DRY-RUN: would cordon, drain, reboot, uncordon {node}")
        return

    log("    cordon + drain")
    kubectl("cordon", node)
    try:
        kubectl("drain", node, "--ignore-daemonsets", "--delete-emptydir-data",
                f"--timeout={DRAIN_TIMEOUT}s", timeout=DRAIN_TIMEOUT + 60)
    except (Abort, subprocess.TimeoutExpired) as exc:
        kubectl("uncordon", node, check=False)
        raise Abort(f"drain of {node} failed ({exc}); uncordoned, stopping run")

    reboot_node(node, vm, dry_run)
    wait_clean_return(node, RETURN_TIMEOUT)

    log("    uncordon")
    kubectl("uncordon", node)

    log("    waiting for Longhorn to settle before next node")
    wait_longhorn_settled(SETTLE_TIMEOUT)
    log(f"  {node}: DONE")


# --- main ---------------------------------------------------------------------
def preflight(hosts: list[PveHost]) -> Plan:
    log("Preflight")
    nodes = kubectl_json("get", "nodes")["items"]
    names, not_ready, cordoned = [], [], []
    for n in nodes:
        name = n["metadata"]["name"]
        names.append(name)
        ready = any(c["type"] == "Ready" and c["status"] == "True"
                    for c in n["status"]["conditions"])
        if not ready:
            not_ready.append(name)
        if n["spec"].get("unschedulable"):
            cordoned.append(name)

    if not_ready:
        raise Abort(f"nodes not Ready: {not_ready} -- fix before rebooting anything")
    if cordoned:
        raise Abort(f"nodes already cordoned: {cordoned} -- resolve first")

    degraded, rebuilding, faulted = longhorn_state()
    if degraded or rebuilding or faulted:
        raise Abort(
            f"Longhorn not healthy: degraded={degraded} rebuilding={rebuilding} "
            f"faulted={faulted} -- refusing to start"
        )
    log(f"  {len(names)} nodes Ready, Longhorn healthy")

    plan = discover(hosts, names)
    log(f"  mapped {len(plan.vms)}/{len(names)} nodes to Proxmox VMs")
    for name, vm in sorted(plan.vms.items()):
        log(f"    {name} -> {vm.host.name} vmid={vm.vmid}")
    if plan.unmapped:
        # Not fatal on its own: those nodes are simply skipped, because without a
        # hypervisor there is no rescue if they wedge mid-reboot.
        log(f"  WARNING: no usable Proxmox mapping for {plan.unmapped} -- skipping")
    return plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; touch nothing")
    ap.add_argument("--deadline", default=os.environ.get("REBOOT_DEADLINE_UTC", ""),
                    help="HH:MM UTC after which no new node is started")
    args = ap.parse_args()

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"
    if dry_run:
        log("DRY RUN -- no changes will be made")

    try:
        hosts = load_hosts()
        plan = preflight(hosts)

        log("Checking which nodes need a reboot")
        candidates, unreachable = [], []
        for name in plan.nodes:
            if name not in plan.vms:
                continue
            needs = reboot_required(name)
            if needs is None:
                unreachable.append(name)
            elif needs:
                candidates.append(name)

        if unreachable:
            raise Abort(
                f"could not read reboot sentinel on {unreachable} -- these need "
                "recovery, not patching; stopping rather than guessing"
            )
        if not candidates:
            log("No nodes require a reboot. Nothing to do.")
            return 0

        # Control-plane last: keep the API (and this script's kubectl) alive as
        # long as possible.
        cp = set()
        for n in kubectl_json("get", "nodes")["items"]:
            labels = n["metadata"].get("labels", {})
            if "node-role.kubernetes.io/control-plane" in labels:
                cp.add(n["metadata"]["name"])
        candidates.sort(key=lambda n: (n in cp, n))
        log(f"Nodes to reboot ({len(candidates)}): {candidates}")

        for name in candidates:
            if args.deadline:
                now = datetime.now(timezone.utc).strftime("%H:%M")
                if now >= args.deadline:
                    log(f"Deadline {args.deadline} UTC reached -- stopping before "
                        f"{name}. Remaining: {candidates[candidates.index(name):]}")
                    break
            process_node(name, plan.vms[name], dry_run)

        log("Run complete. Cluster healthy.")
        return 0

    except Abort as exc:
        log(f"ABORTED: {exc}")
        log("Remaining nodes were left untouched.")
        return 1
    except Exception as exc:  # noqa: BLE001 - unattended: fail loudly, not silently
        log(f"UNEXPECTED FAILURE: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
