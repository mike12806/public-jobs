#!/usr/bin/env python3
"""Staged, health-gated rolling reboot of Kubernetes nodes running as Proxmox VMs.

Replaces kured. The design goal is that a run either completes cleanly or stops
early leaving the cluster up -- never "keep going and hope".

Per node, in sequence:
  1. skip unless /var/run/reboot-required exists on the host
  2. cordon + drain: a short polite pass, then one deleting pass for pods a
     PodDisruptionBudget will never release. A drain that fails even then
     aborts the run.
  3. reboot, escalating only on failure:
       a. systemctl reboot in-guest      (graceful, clean unmount)
       b. Proxmox /status/reboot         (ACPI; shuts down AND restarts)
       c. Proxmox stop + start           (hard power cycle -- last resort)
     A rung "succeeds" only if the node goes NotReady AND returns Ready. A guest
     hung during shutdown fails that test and escalates, which is the point. A
     rung that never takes the node down at all is escalated early rather than
     sitting out its whole window.
  4. wait for a *clean* return: Ready + Longhorn engine + CSI registered
  5. uncordon, then wait for Longhorn to be fully healthy before the next node

Host access is via a short-lived privileged pod pinned to the node with its root
filesystem at /host, so no SSH is required -- SSH is exactly what is unavailable
when a node wedges. The pod tolerates all taints, since by the time we run on a
node it is already cordoned.

Everything mutating is gated on --dry-run.

For testing, FORCE_REBOOT_NODES (or --force-nodes) fakes step 1's sentinel for
named nodes, so the whole path can be exercised without waiting for a kernel
update to land. It is wired to the manual workflow input only, never the
schedule.
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
DRAIN_TIMEOUT = 180          # 3m of polite eviction; a PDB refusal never clears
DRAIN_FORCE_TIMEOUT = 300    # 5m more, deleting rather than evicting
DOWN_WAIT = 150              # 2.5m for a reboot to take the node NotReady at all
GRACEFUL_WAIT = 420          # 7m for an in-guest reboot to go down AND return
ACPI_WAIT = 420              # 7m for a hypervisor ACPI reboot to do the same
HARD_STOP_WAIT = 180         # 3m for the VM to actually reach 'stopped'
RETURN_TIMEOUT = 900         # 15m to come back fully healthy
SETTLE_TIMEOUT = 1800        # 30m for Longhorn to finish rebuilding. One node
                             # cannot be allowed an hour when there are 11.
POLL = 15
# Worst case for one node, if every wait below runs to its limit.
NODE_WORST_CASE = (DRAIN_TIMEOUT + DRAIN_FORCE_TIMEOUT + GRACEFUL_WAIT + ACPI_WAIT
                   + HARD_STOP_WAIT + RETURN_TIMEOUT + SETTLE_TIMEOUT)
# GitHub caps a job at 360 minutes and kills it mid-step, which here could mean
# a node left cordoned or half-rebooted with no failure email (a cancelled job
# skips if: failure()). So stop starting nodes before that can happen. The
# default leaves 40m of the 360m job for a node already under way to finish.
RUN_BUDGET = int(os.environ.get("REBOOT_RUN_BUDGET_MIN", "320")) * 60
# How much slower than the slowest node so far to assume the next one will be.
PACE_MARGIN = 1.5
KUBECTL_RETRIES = 3          # ride out API blips (kube-vip failover, etc.)
KUBECTL_RETRY_DELAY = 10
# Pulled on every host read; use the local proxy so Docker Hub rate limits or
# an outage cannot break the run. Override with REBOOTCTL_IMAGE.
DEBUG_IMAGE = os.environ.get(
    "REBOOTCTL_IMAGE", "harbor.mfaherty.net/dockerhub-proxy/library/busybox:1.37"
)


class Abort(Exception):
    """Stop the run. Remaining nodes are left untouched."""


def parse_forced(raw: str) -> tuple[bool, set[str]]:
    """Parse FORCE_REBOOT_NODES into (all_nodes, named_nodes).

    Nothing else in this script can be tested end to end without a node that
    genuinely has /var/run/reboot-required, which means waiting for a kernel
    update. Faking the sentinel for named nodes -- or "all" -- exercises the
    real drain/reboot/settle path on demand.

    Only the sentinel check is skipped. Every safety gate (preflight health,
    identity assertion, Longhorn settling, the deadline) still applies, and a
    forced node with no Proxmox mapping is refused rather than rebooted blind.
    """
    tokens = [t for t in raw.replace(",", " ").split() if t]
    if not tokens:
        return False, set()
    if any(t.lower() == "all" for t in tokens):
        if len(tokens) > 1:
            raise Abort("force list: 'all' cannot be combined with node names")
        return True, set()
    return False, set(tokens)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# --- kubectl ------------------------------------------------------------------
def kubectl_error(stderr: str) -> str:
    """The part of kubectl's stderr that says what went wrong.

    drain prints its "ignoring DaemonSet-managed Pods" warning first and at
    length, so a naive truncation reports that instead of the actual failure --
    which is how a PDB-blocked drain came to look like a DaemonSet problem.
    """
    lines = [ln for ln in stderr.splitlines()
             if ln.strip() and not ln.startswith("Warning:")]
    return " / ".join(lines)[:200] if lines else stderr.strip()[:200]


def kubectl(*args: str, check: bool = True, timeout: int = 120,
            retries: int = KUBECTL_RETRIES) -> str:
    """Run kubectl, riding out transient API failures.

    Rebooting a control-plane node blips the API (kube-vip failover), and a
    single hiccup should not abort an overnight run. Mutating verbs are still
    safe to retry here: cordon/uncordon/drain are idempotent.
    """
    last = ""
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                ["kubectl", *args], capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        else:
            if proc.returncode == 0:
                return proc.stdout
            last = kubectl_error(proc.stderr)
            if not check:
                return proc.stdout
        if attempt < retries:
            log(f"      kubectl {args[0]} failed ({last}); "
                f"retry {attempt}/{retries - 1}")
            time.sleep(KUBECTL_RETRY_DELAY)
    if check:
        raise Abort(f"kubectl {' '.join(args)} failed after {retries} tries: {last}")
    return ""


def kubectl_json(*args: str, **kw) -> dict:
    return json.loads(kubectl(*args, "-o", "json", **kw))


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
def longhorn_installed() -> bool:
    try:
        crds = kubectl_json("get", "crd")
    except Abort:
        return False

    for item in crds.get("items", []):
        if item.get("metadata", {}).get("name") == "volumes.longhorn.io":
            return True
    return False


def longhorn_state() -> tuple[int, int, int]:
    """(degraded, rebuilding, faulted).

    Raises Abort if Longhorn is installed but cannot be queried. Returning
    "healthy" on a failed query would fail OPEN: every gate in this script
    trusts these numbers, so an API blip during a reboot would green-light the
    next node -- the exact failure this workflow exists to prevent.

    A cluster with no Longhorn at all legitimately has nothing to gate on.
    """
    if not longhorn_installed():
        return (0, 0, 0)

    try:
        vols = kubectl_json("get", "volumes.longhorn.io", "-n", "longhorn-system")
        engines = kubectl_json("get", "engines.longhorn.io", "-n", "longhorn-system")
    except Abort as exc:
        raise Abort(
            f"Longhorn is installed but its state could not be read ({exc}). "
            "Refusing to treat unknown storage health as healthy."
        )

    degraded = faulted = 0
    for v in vols.get("items", []):
        r = v.get("status", {}).get("robustness")
        if r == "degraded":
            degraded += 1
        elif r == "faulted":
            faulted += 1

    rebuilding = sum(
        1 for e in engines.get("items", [])
        if e.get("status", {}).get("rebuildStatus")
    )
    return (degraded, rebuilding, faulted)


def wait_longhorn_settled(timeout: int, budget_left: float | None = None) -> None:
    # The settle wait is the longest single wait in a node's cycle and the only
    # safe one to cut short: the node is already back, Ready and uncordoned, so
    # stopping here leaves a serving cluster that is merely still rebuilding.
    if budget_left is not None and budget_left < timeout:
        timeout = max(int(budget_left), 60)
        log(f"      (settle capped at {timeout // 60}m by the run budget)")
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
def vm_status(vm: VmRef) -> str | None:
    """Proxmox's view of the VM: 'running', 'stopped', or None if unknown."""
    try:
        st = vm.host.call(
            f"/nodes/{vm.host.pve_node}/qemu/{vm.vmid}/status/current"
        ) or {}
        return st.get("status")
    except Exception:  # noqa: BLE001
        return None


def wait_back(node: str, vm: VmRef, seconds: int,
              down_wait: int = DOWN_WAIT) -> bool:
    """Did the node go away and come back Ready?

    This, not "did it go down", is the right success test for a reboot rung.
    An in-guest `systemctl reboot` never stops the QEMU process, so Proxmox
    reports 'running' the whole time -- waiting for 'stopped' would hang on a
    perfectly successful reboot. Conversely, NotReady alone means nothing: a
    guest hung during shutdown goes NotReady in seconds and stays there, which
    is precisely the case the next rung exists for.

    Requiring down-then-up makes a stuck node fail this check and escalate,
    which is the whole point of the ladder.

    The two failure modes deserve different patience, though. A node that went
    down and has not come back may still be booting, so it gets the full
    window. A node that never went down at all has already told us the reboot
    did not land -- a reboot that is going to work takes the node NotReady
    within a minute or so -- and waiting out the rest of the window only delays
    the rung that would have fixed it. So give "did it go down" its own short
    deadline and escalate the moment it passes.
    """
    start = time.time()
    deadline = start + seconds
    down_deadline = start + down_wait
    saw_down = False
    while time.time() < deadline:
        if not node_ready(node):
            if not saw_down:
                log("      node went NotReady (rebooting)")
            saw_down = True
        elif saw_down:
            log("      node returned Ready")
            return True
        elif time.time() >= down_deadline:
            log(f"      node never went down after {down_wait}s -- reboot did "
                f"not take; escalating without waiting out the full {seconds}s")
            return False
        time.sleep(POLL)

    if saw_down:
        log(f"      STUCK: NotReady for {seconds}s without returning "
            f"(Proxmox says: {vm_status(vm)})")
    else:
        log(f"      node never went down after {seconds}s -- reboot did not take")
    return False


def longhorn_pods_on(node: str) -> list[tuple[str, str, list[str]]]:
    """(name, phase, container readiness) for every longhorn-system pod on node.

    Built from JSON rather than a jsonpath template. The template this replaced
    embedded \t and \n, which Python turned into literal control characters
    inside the jsonpath before kubectl ever saw them -- and it ran with
    check=False, so if kubectl rejected it the result was empty output, which
    reads exactly like "no storage pods here". A gate cannot be allowed to fail
    silently into its own negative.
    """
    data = kubectl_json("get", "pods", "-n", "longhorn-system",
                        "--field-selector", f"spec.nodeName={node}", timeout=30)
    pods = []
    for item in data.get("items", []):
        pods.append((
            item.get("metadata", {}).get("name", ""),
            item.get("status", {}).get("phase", ""),
            [str(c.get("ready")) for c in
             item.get("status", {}).get("containerStatuses", [])],
        ))
    return pods


def wait_clean_return(node: str, timeout: int) -> None:
    """Ready is not enough -- CSI registers ~60s later and mounts fail in the gap."""
    deadline = time.time() + timeout
    said = False
    while time.time() < deadline:
        if node_ready(node):
            try:
                pods = longhorn_pods_on(node)
            except Abort as exc:
                # Loudly, not as a False. An unreadable cluster is not an
                # unhealthy node, and the two must not look the same here.
                log(f"    cannot read storage pods on {node}: {exc}")
                time.sleep(POLL)
                continue

            def _ready(substr: str) -> bool:
                return any(
                    substr in name and phase == "Running"
                    and states and all(s == "True" for s in states)
                    for name, phase, states in pods
                )

            has_im = _ready("instance-manager")
            has_csi = _ready("longhorn-csi-plugin")
            if has_im and has_csi:
                log(f"    {node} returned clean (Ready + Longhorn engine + CSI)")
                return
            log(f"    {node} Ready; waiting for storage (engine={has_im} csi={has_csi})")
            if not said:
                # Say once what is actually on the node. "engine=False csi=False"
                # on its own cannot distinguish a pod that is missing from one
                # that is Pending, crash-looping, or simply not ready yet.
                said = True
                if pods:
                    for name, phase, states in pods:
                        log(f"      {name}: {phase} ready={','.join(states) or '-'}")
                else:
                    log(f"      no longhorn-system pods on {node} at all -- "
                        f"nothing is being scheduled back onto it")
        time.sleep(POLL)
    raise Abort(f"{node} did not return healthy within {timeout}s")


def drain_node(node: str) -> None:
    """Evict what leaves politely, then delete what will not.

    --ignore-daemonsets covers the DaemonSet pods every node here runs: they are
    never evicted and never block. What actually stalls this drain is a
    PodDisruptionBudget -- Longhorn keeps one per instance-manager -- and that
    is a refused eviction, not a slow one, so a longer timeout buys nothing but
    dead time. --force does not help either; it covers pods with no controller,
    not pods a budget is protecting. Deleting instead of evicting is what gets
    past a PDB, so do that on the second pass.

    This is not a bypass of the health gates. Preflight still refuses to start
    on a degraded cluster, the identity assertion still runs, and Longhorn must
    be fully settled before the next node is touched.
    """
    common = ("--ignore-daemonsets", "--delete-emptydir-data")
    try:
        # retries=1: kubectl()'s default would run the whole timeout three times
        # over before reporting a stall.
        kubectl("drain", node, *common, f"--timeout={DRAIN_TIMEOUT}s",
                timeout=DRAIN_TIMEOUT + 60, retries=1)
        return
    except (Abort, subprocess.TimeoutExpired) as exc:
        log(f"      eviction unfinished after {DRAIN_TIMEOUT}s "
            f"({str(exc)[:160]})")

    log("      forcing: deleting the pods eviction will not release")
    kubectl("drain", node, *common, "--force", "--disable-eviction",
            f"--timeout={DRAIN_FORCE_TIMEOUT}s",
            timeout=DRAIN_FORCE_TIMEOUT + 60, retries=1)


def reboot_node(node: str, vm: VmRef, dry_run: bool) -> None:
    base = f"/nodes/{vm.host.pve_node}/qemu/{vm.vmid}"

    if dry_run:
        log(f"    DRY-RUN: would reboot {node} ({vm.host.name}:{vm.vmid})")
        return

    # Rung 1 -- graceful, in-guest. Clean unmount of Longhorn replicas.
    log("    rung 1/3: systemctl reboot (in-guest)")
    run_on_host(node, "nsenter -t 1 -m -u -i -n -p -- systemctl reboot", timeout=60)
    if wait_back(node, vm, GRACEFUL_WAIT):
        return

    # Rung 2 -- ACPI reboot via the hypervisor. Use /status/reboot, not
    # /status/shutdown: shutdown leaves the VM powered off and nothing would
    # start it again.
    log("    rung 2/3: Proxmox ACPI reboot")
    try:
        vm.host.call(f"{base}/status/reboot", method="POST")
    except Exception as exc:  # noqa: BLE001
        log(f"      ACPI reboot call failed ({exc}); escalating")
    else:
        if wait_back(node, vm, ACPI_WAIT):
            return

    # Rung 3 -- hard power cycle. Unclean, so only for a node that is stuck.
    log("    rung 3/3: HARD power cycle (qm stop + start)")
    assert_identity(node, vm)  # re-verify identity right before destroying state
    vm.host.call(f"{base}/status/stop", method="POST")

    deadline = time.time() + HARD_STOP_WAIT
    while time.time() < deadline:
        if vm_status(vm) == "stopped":
            break
        time.sleep(5)
    else:
        raise Abort(
            f"{node}: VM {vm.host.name}:{vm.vmid} did not reach 'stopped' within "
            f"{HARD_STOP_WAIT}s; refusing to start it in an unknown state"
        )

    log("      VM stopped; starting")
    vm.host.call(f"{base}/status/start", method="POST")


def process_node(node: str, vm: VmRef, dry_run: bool, forced: bool = False,
                 budget_left: float | None = None) -> None:
    if forced:
        log(f"  {node}: FORCED -- treating as needing a reboot (test override)")
    else:
        log(f"  {node}: reboot required ({reboot_reason(node)})")

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
        drain_node(node)
    except (Abort, subprocess.TimeoutExpired) as exc:
        kubectl("uncordon", node, check=False)
        raise Abort(f"drain of {node} failed ({exc}); uncordoned, stopping run")

    reboot_node(node, vm, dry_run)
    wait_clean_return(node, RETURN_TIMEOUT)

    log("    uncordon")
    kubectl("uncordon", node)

    log("    waiting for Longhorn to settle before next node")
    wait_longhorn_settled(SETTLE_TIMEOUT, budget_left)
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
    ap.add_argument("--force-nodes",
                    default=os.environ.get("FORCE_REBOOT_NODES", ""),
                    help="TESTING: treat these nodes as needing a reboot without "
                         "reading the host sentinel ('all' for every mapped node)")
    args = ap.parse_args()

    started = time.time()
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"
    if dry_run:
        log("DRY RUN -- no changes will be made")
    if args.deadline:
        log(f"Deadline: no new node started at or after {args.deadline} UTC "
            f"(it is now {datetime.now(timezone.utc):%H:%M} UTC)")

    try:
        force_all, force_names = parse_forced(args.force_nodes)
        hosts = load_hosts()
        plan = preflight(hosts)

        if force_all or force_names:
            log("FORCE OVERRIDE ACTIVE -- faking the host reboot sentinel for "
                + ("every mapped node" if force_all else f"{sorted(force_names)}"))
            log("  this is a test affordance; every other safety gate still applies")
            unknown = force_names - set(plan.nodes)
            if unknown:
                raise Abort(f"force list names unknown node(s): {sorted(unknown)}")
            unmapped = force_names & set(plan.unmapped)
            if unmapped:
                raise Abort(
                    f"force list names node(s) with no Proxmox mapping: "
                    f"{sorted(unmapped)} -- there would be no way to rescue them "
                    "if they wedged mid-reboot"
                )
            if not dry_run:
                log("  NOT a dry run: these nodes WILL be cordoned, drained "
                    "and rebooted for real")

        log("Checking which nodes need a reboot")
        candidates, unreachable = [], []
        for name in plan.nodes:
            if name not in plan.vms:
                continue
            if force_all or name in force_names:
                candidates.append(name)
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

        # Budgeting on NODE_WORST_CASE would be useless here: 11 nodes at the
        # ceiling do not fit in any 360m job, so a run that assumed the worst
        # would stop after three good ones. Nodes in a clean run cost a small
        # fraction of the ceiling, so once a node has actually been done, pace
        # the rest on that measurement instead.
        done: list[float] = []
        for i, name in enumerate(candidates):
            if args.deadline:
                now = datetime.now(timezone.utc).strftime("%H:%M")
                if now >= args.deadline:
                    log(f"Deadline {args.deadline} UTC reached -- stopping before "
                        f"{name}. Remaining: {candidates[i:]}")
                    break
            left = RUN_BUDGET - (time.time() - started)
            need = max(done) * PACE_MARGIN if done else NODE_WORST_CASE
            if not dry_run and left < need:
                log(f"Stopping before {name}: {left / 60:.0f}m of budget left, "
                    f"a node is costing up to {need / PACE_MARGIN / 60:.0f}m. "
                    f"Better to end clean than be killed mid-node. "
                    f"Remaining: {candidates[i:]}")
                break

            t0 = time.time()
            process_node(name, plan.vms[name], dry_run,
                         forced=force_all or name in force_names,
                         budget_left=None if dry_run else left)
            if not dry_run:
                done.append(time.time() - t0)
                log(f"  ({len(done)}/{len(candidates)} done, {done[-1] / 60:.0f}m "
                    f"for {name}, {(RUN_BUDGET - (time.time() - started)) / 60:.0f}m "
                    f"budget left)")

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
