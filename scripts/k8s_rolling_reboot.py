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
       b. Proxmox stop + start           (power cycle; always completes)
     A rung "succeeds" only if the node goes NotReady AND returns Ready. A guest
     hung during shutdown fails that test and escalates, which is the point. A
     rung that never takes the node down at all is escalated early rather than
     sitting out its whole window.
     There is deliberately no ACPI rung. /status/reboot asks the guest to shut
     down and waits for it to agree, so the one situation an escalation exists
     for -- a guest too wedged to answer -- is the situation where it can sit
     there indefinitely. /status/stop kills the QEMU process outright, so it
     always completes; rung (a) is what covers the clean case.
  4. wait for Longhorn's own pods to come back: CSI registered before the
     uncordon, instance-manager after it (Longhorn will not place one while
     cordoned). Bounded, and advisory rather than fatal. Control-plane nodes
     skip it entirely -- Longhorn is kept off them -- as does any other node
     Longhorn has no record of running on, and a node whose pods are late only
     stops the run if the volumes actually need them. Which pods are up on the node just rebooted is not the
     property this job exists to protect; see step 5 for the one that is.
  5. move on once every volume would still hold REPLICA_FLOOR healthy copies
     with the *next* node down -- not once the cluster falls silent. A rebuild
     on the node just finished endangers nothing about a node that holds none
     of that volume's copies. The run settles fully once, at the end -- and
     even there, volumes still rebuilding above the floor finish the run
     rather than fail it.

That wait ends on whether Longhorn is making progress, not on a fixed clock.
Engines publish the percentage the UI shows for a rebuild in flight, so the
wait watches those percentages climb. A rebuild still gaining ground keeps the
wait alive -- past SETTLE_TIMEOUT, up to whatever the run budget can spare --
because a volume five minutes from safe is not a reason to fail a run, and the
old fixed hour failed exactly those. A rebuild that has gained nothing for
REBUILD_STALL ends it instead, since nothing is being waited for.

The run budget is what actually bounds the wait now. That limit is real: the
job is killed at 360m and a node cordoned at that moment stays cordoned, so
the wait extends only into budget the reboot it precedes will not need.

Why nothing is moving is reported, never acted on. A volume Longhorn cannot
place reads as unschedulable, but this job cordons a node at a time and
Longhorn will not schedule onto a cordoned node by default, so that condition
is one the run itself produces and clears. It belongs in the message that
explains a stall, not in a rule that causes one -- "still short after 3600s"
named a symptom, and the point of the percentages is to name a cause.

A volume reading FAULTED mid-run is re-checked for ten minutes before it stops
anything: a node coming back through a reboot can fault a volume transiently
while its engine re-attaches or its last replicas rebuild, and that clears on
its own. Only a fault still standing at the end of the window aborts. The
preflight gate is deliberately not given that grace -- nothing has been
rebooted yet, so a fault there is not one this run caused.

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
HARD_STOP_WAIT = 180         # 3m for the VM to actually reach 'stopped'
RETURN_TIMEOUT = 900         # 15m to come back Ready on a new boot
STORAGE_TIMEOUT = 300        # 5m for Longhorn's per-node pods, once the node
                             # itself is back. Measured, not guessed: in a
                             # clean run csi-plugin and instance-manager are
                             # ready within half a minute of the node
                             # returning, every time. This used to borrow
                             # RETURN_TIMEOUT, so a node where they were never
                             # coming back cost a quarter of an hour before
                             # saying so -- and then ended the run. It no
                             # longer ends the run on its own; see
                             # storage_back().
SETTLE_TIMEOUT = 3600        # 60m for Longhorn to finish rebuilding. Half an
                             # hour was not enough: a large volume rebuilding
                             # across a busy cluster routinely runs past it, and
                             # aborting there leaves the run unfinished for a
                             # rebuild that was progressing fine. This is a
                             # ceiling, not a cost -- the wait returns the
                             # moment the volumes are safe, and the run budget
                             # below caps it when the job is short on time, so
                             # one slow node spends the tail of the run rather
                             # than the whole job.
REPLICA_FLOOR = 2            # healthy copies that must survive the next node
                             # going down. Two, not three: it keeps every volume
                             # one-failure-tolerant at all times, which is the
                             # property that matters. Requiring three means
                             # waiting for a rebuild that endangers nothing.
                             # One would mean deliberately power-cycling a node
                             # while a volume had a single copy left.
FAULT_GRACE = 600            # 10m of re-checks before a FAULTED volume is
                             # believed. Longhorn calls a volume faulted the
                             # moment it has no usable replica, which a node
                             # returning from a reboot produces transiently.
                             # A genuine fault outlives ten minutes easily.
FAULT_POLL = 30              # re-check cadence inside that window
REBUILD_STALL = 1200         # 20m of no rebuild gaining any ground before a
                             # wait stops waiting. SETTLE_TIMEOUT alone cannot
                             # tell "slow" from "stuck": it spends the same
                             # hour either way, which is both too long for a
                             # rebuild that is never going to finish and too
                             # short for a large one that would have. Longhorn
                             # publishes the percentage the UI shows, so ask
                             # the question directly -- is anything copying.
                             # This is also how much runway a wait is given
                             # each time something does move, which is what
                             # lets a slow rebuild outlast SETTLE_TIMEOUT.
                             # Twenty minutes rather than ten because Longhorn
                             # legitimately does nothing for a while after a
                             # node goes away: replica-replenishment-wait-
                             # interval (default 600s) is how long it holds off
                             # replacing replicas from a node that might just
                             # be rebooting -- which is every node this script
                             # touches. A window under that would call the
                             # cluster stalled while it was only being patient.
                             # Raise this if that setting is raised.
POLL = 15
HEARTBEAT = 60               # never go longer than this without saying
                             # something. A wait that polls silently for
                             # minutes is indistinguishable from a hung job,
                             # and "is it stuck?" is not a question an
                             # unattended run should leave anyone asking.
# Worst case for one node, if every wait below runs to its limit.
# FAULT_GRACE is in here because a fault seen during a node's redundancy wait
# extends that wait by the window it spends re-checking. STORAGE_TIMEOUT counts
# twice: once before the uncordon and once after.
NODE_WORST_CASE = (DRAIN_TIMEOUT + DRAIN_FORCE_TIMEOUT + GRACEFUL_WAIT
                   + HARD_STOP_WAIT + RETURN_TIMEOUT + 2 * STORAGE_TIMEOUT
                   + SETTLE_TIMEOUT + FAULT_GRACE)
# What a node still needs after its redundancy wait returns: everything in the
# worst case except the wait itself. The wait is allowed to run long while
# rebuilds are progressing, so it has to leave this much of the run budget
# behind -- waiting until the budget is gone and then cordoning a node is how
# you get killed at GitHub's 360m limit with a node half rebooted.
REBOOT_RESERVE = NODE_WORST_CASE - SETTLE_TIMEOUT
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
        if attempt < retries:
            log(f"      kubectl {args[0]} failed ({last}); "
                f"retry {attempt}/{retries - 1}")
            time.sleep(KUBECTL_RETRY_DELAY)
    # Reached only when every attempt failed. check=False used to return here on
    # the FIRST failure, before any retry -- so the callers most exposed to an
    # API blip were the only ones this wrapper never protected, and a
    # control-plane reboot is exactly when kube-vip makes the API blip.
    if check:
        raise Abort(f"kubectl {' '.join(args)} failed after {retries} tries: {last}")
    return ""


def kubectl_json(*args: str, **kw) -> dict:
    return json.loads(kubectl(*args, "-o", "json", **kw))


def node_ready(name: str) -> bool | None:
    """True, False, or None when the cluster could not be asked.

    None is not False. It ran with check=False, which returns empty stdout on
    failure, so an unreachable API read as "" -- which is not "True", which is
    NotReady. That is how an API blip becomes a phantom reboot: wait_back sees a
    down-then-up transition that never happened, calls the rung a success, and
    the node is marked done still carrying the kernel update it was supposed to
    take. Unknown has to stay distinguishable from down.
    """
    try:
        out = kubectl(
            "get", "node", name, "-o",
            'jsonpath={range .status.conditions[?(@.type=="Ready")]}{.status}{end}',
            timeout=30,
        )
    except (Abort, subprocess.TimeoutExpired):
        return None
    return out.strip() == "True"


def node_boot_id(name: str) -> str | None:
    """The node's kernel boot_id, or None when it could not be read.

    A different one means the machine has actually booted, which is the only
    unambiguous evidence that a reboot happened. Ready/NotReady is not. The node
    controller needs about forty seconds of missed leases before it calls a node
    NotReady, so for the first half minute after a power cycle the API still
    reports the node Ready -- carrying the status it had when the power went.
    That is how the run this came from walked straight out of "start issued"
    into a storage wait against a node that was still in its BIOS, and then
    spent fifteen minutes blaming Longhorn for it.
    """
    try:
        out = kubectl(
            "get", "node", name, "-o", "jsonpath={.status.nodeInfo.bootID}",
            timeout=30,
        )
    except (Abort, subprocess.TimeoutExpired):
        return None
    return out.strip() or None


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
    """Whether Longhorn's CRDs are present. Raises if the cluster cannot be asked.

    Swallowing the error here returned False, which made longhorn_state() report
    (0, 0, 0) -- a perfectly healthy cluster -- from an API blip. Every gate
    below trusts these numbers, and they now decide whether it is safe to take
    another node down, so "could not ask" must not read as "nothing to check".
    """
    crds = kubectl_json("get", "crd")
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
    try:
        if not longhorn_installed():
            return (0, 0, 0)
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


def rebuild_progress() -> dict[str, int]:
    """{volume/replica: percent complete} for every rebuild now in flight.

    This is the number the Longhorn UI puts on a rebuilding volume, and it is
    the only thing here that separates "slow" from "stuck". The degraded and
    rebuilding counts say a rebuild exists; they say nothing about whether it
    is moving, which is why a run can sit on them for an hour and learn
    nothing. Engines carry the percentage in status.rebuildStatus, keyed by
    the replica being rebuilt.

    Only an attached volume with a running engine reports anything, and
    Longhorn rebuilds a limited number of replicas at a time, so an empty map
    means "nothing copying right now" rather than "nothing to do" -- normal
    between rebuilds, and why callers judge the cluster as a whole rather than
    any single volume.
    """
    engines = kubectl_json("get", "engines.longhorn.io", "-n", "longhorn-system")
    out: dict[str, int] = {}
    for e in engines.get("items", []):
        vol = e.get("spec", {}).get("volumeName", "?")
        for replica, status in (e.get("status", {}).get("rebuildStatus") or {}).items():
            out[f"{vol}/{replica}"] = int(status.get("progress") or 0)
    return out


def rebuilds_advanced(before: dict[str, int], now: dict[str, int]) -> bool:
    """Whether any rebuild gained ground between two readings.

    Ground gained means one thing: a percentage that went up. Deliberately not
    "a rebuild appeared" -- Longhorn replaces a failed replica with a brand new
    one under a brand new name, so a rebuild that dies and is retried forever
    is an endless stream of new keys, and counting those as progress would make
    a crash loop look like the healthiest volume in the cluster. It is the one
    failure worth catching that the old timeout could not see at all.

    A rebuild that vanishes counts neither way; if it finished, the volume
    leaves the caller's short list and that is what says so. A percentage that
    drops is a rebuild starting over, which is not progress either.

    The cost of being this strict is a rebuild that reports 0% for its first
    few minutes, which reads as no movement until the first percent lands.
    REBUILD_STALL is sized so that is comfortably survivable.
    """
    return any(key in before and pct > before[key] for key, pct in now.items())


def rebuild_detail(progress: dict[str, int]) -> str:
    """The in-flight rebuilds as the heartbeat and abort messages show them."""
    if not progress:
        return "; no rebuild in flight"
    shown = sorted(progress.items())
    at = ", ".join(f"{key} at {pct}%" for key, pct in shown[:3])
    more = f" (+{len(shown) - 3} more)" if len(shown) > 3 else ""
    return f"; rebuilding {at}{more}"


def unschedulable_volumes() -> dict[str, str]:
    """{volume: why} for volumes Longhorn cannot place a replica for.

    A volume whose Scheduled condition is False is not rebuilding slowly, it is
    not rebuilding at all: Longhorn has looked for somewhere to put the copy
    and found nowhere. Disk pressure, a tag or anti-affinity rule no remaining
    node satisfies, or replicas stranded on a node that is gone all land here.
    None of them resolve by waiting, so a wait that treats this like a slow
    rebuild spends its whole window to reach a conclusion available in the
    first minute.

    Unknown is not False: Longhorn uses it while it is still working the
    placement out, and only False is a decision.
    """
    vols = kubectl_json("get", "volumes.longhorn.io", "-n", "longhorn-system")
    out: dict[str, str] = {}
    for v in vols.get("items", []):
        if v.get("status", {}).get("robustness") not in ("healthy", "degraded"):
            continue
        for cond in v.get("status", {}).get("conditions", []) or []:
            if cond.get("type") == "Scheduled" and cond.get("status") == "False":
                name = v.get("metadata", {}).get("name", "?")
                out[name] = cond.get("reason") or cond.get("message") or "unschedulable"
    return out


def confirm_faulted(faulted: int, where: str) -> int:
    """Re-check a FAULTED reading for FAULT_GRACE before acting on it.

    Longhorn marks a volume faulted the moment it has no usable replica, and a
    node going through a reboot produces exactly that transiently: the engine
    has not re-attached yet, or the copies left are still rebuilding. It clears
    on its own within a minute or two. Aborting on the first reading ends the
    run over a condition that fixes itself, and a run that ends early leaves
    every remaining node unbooted.

    Returns 0 if the cluster recovered inside the window, otherwise the faulted
    count still standing at the end of it -- which the caller treats exactly as
    it used to treat the first reading. A volume whose last replica is really
    gone stays faulted far longer than ten minutes, so the trade is ten minutes
    of run budget against aborting on a blip.
    """
    log(f"    {faulted} Longhorn volume(s) FAULTED {where} -- re-checking for "
        f"{FAULT_GRACE // 60}m before stopping; a reboot can fault a volume "
        f"transiently")
    deadline = time.time() + FAULT_GRACE
    while time.time() < deadline:
        time.sleep(FAULT_POLL)
        degraded, rebuilding, faulted = longhorn_state()
        if not faulted:
            log(f"    FAULTED cleared (degraded={degraded} "
                f"rebuilding={rebuilding}) -- continuing")
            return 0
        log(f"      still faulted={faulted} (degraded={degraded} "
            f"rebuilding={rebuilding}), "
            f"{max(0.0, deadline - time.time()) / 60:.0f}m of grace left")
    log(f"    {faulted} volume(s) still FAULTED after {FAULT_GRACE // 60}m")
    return faulted


def grant_grace(deadline: float, given: float, started: float) -> tuple[float, float]:
    """Hand a wait back the time a fault re-check cost it, up to one window.

    The re-check was not spent on what the wait is actually waiting for, so
    charging it to that wait would time a cluster out the moment it recovers.
    The cap is what keeps that honest: a volume flapping in and out of faulted
    would otherwise push the deadline back a window at a time and the wait
    would never end. Past the cap the wait times out normally.
    """
    give = min(time.time() - started, max(0.0, FAULT_GRACE - given))
    return deadline + give, given + give


def replica_map() -> dict[str, dict[str, bool]]:
    """volume name -> {node holding a replica: is that replica healthy}.

    Healthy means Longhorn has recorded the replica as healthy (spec.healthyAt)
    and has not since failed it (spec.failedAt). A replica being rebuilt has no
    healthyAt yet, which is exactly the state this gate needs to see.

    currentState is deliberately not consulted: a detached volume's replicas are
    stopped but perfectly good, and treating stopped as unhealthy would block
    the run on volumes nothing is using.
    """
    data = kubectl_json("get", "replicas.longhorn.io", "-n", "longhorn-system")
    out: dict[str, dict[str, bool]] = {}
    for r in data.get("items", []):
        spec = r.get("spec", {})
        vol, node = spec.get("volumeName"), spec.get("nodeID")
        if not vol or not node:
            # Refuse to guess. Reporting fewer replicas than exist would be
            # conservative; placing a replica on the wrong node would not.
            raise Abort(
                f"replica {r.get('metadata', {}).get('name')} has no volumeName "
                "or nodeID -- cannot tell which node holds which copy"
            )
        healthy = bool(spec.get("healthyAt")) and not spec.get("failedAt")
        # Two replicas of one volume on a single node is an anti-affinity
        # violation, but if it happens that node counts as holding a healthy
        # copy when any of them is healthy -- and as one node either way, which
        # is what matters when it goes down.
        out.setdefault(vol, {})[node] = out.get(vol, {}).get(node, False) or healthy
    return out


def shortfalls(vols: dict[str, str], limit: int) -> list[str]:
    """The first `limit` short volumes, rendered for a log line or an abort."""
    return [f"{name} ({why})" for name, why in sorted(vols.items())[:limit]]


def unsafe_volumes(node: str) -> dict[str, str]:
    """Volumes that would fall below REPLICA_FLOOR if `node` went down now.

    The old gate waited for the whole cluster to be clean, which asked the wrong
    question: a volume rebuilding a replica on the node just finished endangers
    nothing about the next node, unless the next node holds one of its remaining
    healthy copies. This asks the question that matters -- once this node drops,
    how many healthy copies does each volume still have.

    The floor is lowered for volumes that cannot reach it: a two-replica volume
    with a copy here can only ever keep one, and a single-replica volume keeps
    none. Those get the redundancy they would have had in a fully healthy
    cluster rather than an impossible target that would hang the run.

    Keyed by volume name so a caller can line the answer up against the other
    things Longhorn says about the same volume -- whether it can be scheduled,
    whether anything is copying into it -- rather than re-deriving the name
    from a sentence.
    """
    vols = kubectl_json("get", "volumes.longhorn.io", "-n", "longhorn-system")
    reps = replica_map()
    bad: dict[str, str] = {}
    for v in vols.get("items", []):
        name = v.get("metadata", {}).get("name", "?")
        robustness = v.get("status", {}).get("robustness")
        if robustness not in ("healthy", "degraded"):
            # unknown/detached: the settle gate never counted these either, and
            # faulted is caught separately as an abort rather than a wait.
            continue
        holders = reps.get(name)
        if not holders:
            raise Abort(
                f"volume {name} is {robustness} but no replicas report holding "
                "it -- refusing to reason about redundancy from that"
            )
        healthy_elsewhere = sum(1 for n, ok in holders.items() if ok and n != node)
        floor = min(REPLICA_FLOOR, len(holders) - (1 if node in holders else 0))
        if healthy_elsewhere < floor:
            bad[name] = f"{healthy_elsewhere} healthy off {node}, needs {floor}"
    return bad


def wait_safe_to_reboot(node: str, timeout: int,
                        budget_left: float | None = None) -> None:
    """Block until taking `node` down leaves every volume above the floor.

    Two clocks, and which one stops the wait is the whole point.

    `timeout` is patience with a cluster that is not visibly doing anything.
    It no longer ends a wait on its own: a rebuild that is still copying gets
    the deadline pushed out, because the question this wait exists to answer
    is "will the volumes be safe", and a rebuild gaining ground is the cluster
    answering yes slowly. Cutting it off at a fixed hour failed runs that were
    minutes from clearing, which is what the old fixed deadline did.

    The run budget is the clock that does stop it. That one is real -- GitHub
    kills the job at 360m and a node cordoned when that happens stays cordoned
    -- so the wait may extend only while REBOOT_RESERVE of budget remains for
    the reboot it is waiting to start. When neither clock is available (a dry
    run passes no budget), `timeout` stands in as the ceiling.

    Nothing gaining ground for REBUILD_STALL ends it too, and that is the only
    genuinely new way to stop: no rebuild anywhere moved, so waiting longer is
    not waiting for anything.
    """
    if not longhorn_installed():
        return
    started = time.time()
    # `base` is what this wait always got: `timeout`, cut down when the run is
    # short on time. `ceiling` is how far progress may push it -- only into
    # budget the reboot itself will not need. Never below `base`, because the
    # caller admits a node on its observed pace while REBOOT_RESERVE is a
    # worst case, so the subtraction goes negative on a cluster rebooting
    # briskly. Extending must never shorten.
    base = timeout if budget_left is None else max(min(timeout, budget_left), 60)
    ceiling = started + (base if budget_left is None
                         else max(base, budget_left - REBOOT_RESERVE))
    if base < timeout:
        log(f"    (redundancy wait capped at {int(base) // 60}m "
            f"by the run budget)")
    deadline = started + base
    grace_given = 0.0
    bad: dict[str, str] = {}
    said = False
    beat = time.time()
    seen = rebuild_progress()   # rebuilds in flight as of the last reading
    short = -1                  # how many volumes were short at that reading
    moved = time.time()         # when either of those last improved
    while time.time() < deadline:
        degraded, rebuilding, faulted = longhorn_state()
        if faulted:
            grace_start = time.time()
            faulted = confirm_faulted(faulted, f"while waiting on {node}")
            was = deadline
            deadline, grace_given = grant_grace(deadline, grace_given, grace_start)
            # Time spent re-checking a fault is not time this wait spent
            # waiting, by the same argument grant_grace makes -- so it is not
            # time the cluster spent failing to rebuild either. Charging it to
            # the stall clock would let a single fault window plus one quiet
            # stretch look like a rebuild that died. Progress actually made
            # during the window is still seen: the next reading is compared
            # against the one from before it.
            # Credited from the grace granted, before the budget claws any of
            # it back below: whether the deadline had room for that window has
            # no bearing on whether the cluster was rebuilding during it.
            moved += deadline - was
            deadline = min(deadline, ceiling)   # the budget outranks the grace
            if faulted:
                raise Abort(f"{faulted} Longhorn volume(s) still FAULTED after "
                            f"{FAULT_GRACE // 60}m -- stopping")
        bad = unsafe_volumes(node)
        if not bad:
            return

        # Why nothing is rebuilding, when nothing is. Reported, never acted on:
        # this job cordons a node at a time, and Longhorn stops scheduling onto
        # a cordoned node by default, so a volume can read unschedulable purely
        # because of what this run is doing and clear on the uncordon. Treating
        # that as terminal would fail runs over a condition the run created.
        # As a line in the stall message it is exactly what is wanted -- the
        # reason the percentages were not moving.
        cannot = {v: why for v, why in unschedulable_volumes().items() if v in bad}

        now = rebuild_progress()
        if short < 0 or len(bad) < short or rebuilds_advanced(seen, now):
            moved = time.time()
            # Ground gained buys more waiting, up to what the run can spare.
            deadline = min(max(deadline, time.time() + REBUILD_STALL), ceiling)
        seen, short = now, len(bad)
        idle = time.time() - moved
        if idle >= REBUILD_STALL:
            why = (f"; Longhorn cannot place a replica for "
                   f"{shortfalls(cannot, 3)}" if cannot else "")
            raise Abort(
                f"no Longhorn rebuild has gained ground in "
                f"{REBUILD_STALL // 60}m and {len(bad)} volume(s) are still "
                f"short of {REPLICA_FLOOR} copies; not taking {node} down: "
                f"{shortfalls(bad, 3)}{rebuild_detail(now)}{why}"
            )

        if not said:
            said = True
            beat = time.time()
            log(f"    waiting for redundancy before {node}: {len(bad)} volume(s) "
                f"would drop below {REPLICA_FLOOR} copies")
            for line in shortfalls(bad, 5):
                log(f"      {line}")
            if len(bad) > 5:
                log(f"      ... and {len(bad) - 5} more")
        elif time.time() - beat >= HEARTBEAT:
            # This is the longest wait in the run and it used to say the above
            # once and then poll in silence until the rebuilds finished --
            # a quarter of an hour of nothing on a busy cluster. The counts
            # show there is work outstanding; the percentages show it is
            # actually being done, and the idle clock shows how close this is
            # to deciding it is not.
            beat = time.time()
            stalling = (f"; nothing moved for {int(idle)}s of {REBUILD_STALL}s"
                        if idle >= HEARTBEAT else "")
            unplaceable = (f"; unschedulable: {shortfalls(cannot, 2)}"
                           if cannot else "")
            log(f"      still {len(bad)} volume(s) short before {node} "
                f"(degraded={degraded} rebuilding={rebuilding}"
                f"{rebuild_detail(now)}{stalling}{unplaceable}); "
                f"{int(deadline - time.time())}s left, "
                f"{int(ceiling - time.time())}s of budget")
        time.sleep(30)
    # Reached only by running out of budget, or out of `timeout` without ever
    # seeing progress buy more. Either way the volumes never got there.
    waited = int(time.time() - started)
    raise Abort(
        f"volumes still short of {REPLICA_FLOOR} copies after {waited}s "
        f"({'run budget spent' if time.time() >= ceiling else 'no progress'}); "
        f"not taking {node} down: {shortfalls(bad, 3)}"
    )


def wait_longhorn_settled(timeout: int, budget_left: float | None = None) -> None:
    # The settle wait is the longest single wait in a node's cycle and the only
    # safe one to cut short: the node is already back, Ready and uncordoned, so
    # stopping here leaves a serving cluster that is merely still rebuilding.
    if budget_left is not None and budget_left < timeout:
        timeout = max(int(budget_left), 60)
        log(f"      (settle capped at {timeout // 60}m by the run budget)")
    started = time.time()
    # Same two clocks as the redundancy wait, and the same reason: a rebuild
    # still copying is not a reason to stop waiting. Nothing follows this wait
    # inside the run, so the budget itself is the ceiling -- no reserve.
    ceiling = (started + max(budget_left, 60.0)
               if budget_left is not None else started + timeout)
    deadline = min(started + timeout, ceiling)
    grace_given = 0.0
    seen = rebuild_progress()
    moved = time.time()
    while time.time() < deadline:
        degraded, rebuilding, faulted = longhorn_state()
        if faulted:
            grace_start = time.time()
            faulted = confirm_faulted(faulted, "while settling")
            was = deadline
            deadline, grace_given = grant_grace(deadline, grace_given, grace_start)
            moved += deadline - was   # not time spent failing to rebuild
            if faulted:
                raise Abort(f"{faulted} Longhorn volume(s) still FAULTED after "
                            f"{FAULT_GRACE // 60}m -- stopping")
            # The counts above predate the window; re-read rather than settle
            # on them.
            continue
        if degraded == 0 and rebuilding == 0:
            return
        now = rebuild_progress()
        if rebuilds_advanced(seen, now):
            moved = time.time()
            deadline = min(max(deadline, time.time() + REBUILD_STALL), ceiling)
        seen = now
        if time.time() - moved >= REBUILD_STALL:
            # Stop waiting, but do not decide anything here: this is the one
            # wait whose timeout is not a failure, and a stall is only a reason
            # to reach that same verdict sooner. The check below is what says
            # whether the cluster is actually unsafe or merely untidy.
            log(f"    Longhorn has gained no ground in {REBUILD_STALL // 60}m "
                f"(degraded={degraded} rebuilding={rebuilding}"
                f"{rebuild_detail(now)}) -- not waiting out the rest")
            break
        log(f"    Longhorn settling: degraded={degraded} "
            f"rebuilding={rebuilding}{rebuild_detail(now)}")
        time.sleep(30)

    # Out of time, but "not settled" and "not safe" are different claims, and
    # only the second is worth failing a run over. Every node is back and
    # uncordoned by now; a volume that holds REPLICA_FLOOR healthy copies can
    # finish rebuilding the rest on its own time. "" is no node: it asks what
    # each volume has right now, rather than what it would have with some node
    # taken away.
    waited = int(time.time() - started)
    short = unsafe_volumes("")
    if short:
        raise Abort(
            f"Longhorn did not settle within {waited}s and {len(short)} "
            f"volume(s) are below {REPLICA_FLOOR} healthy copies: "
            f"{shortfalls(short, 3)}"
        )
    log(f"    Longhorn still rebuilding after {waited}s, but every volume holds "
        f"{REPLICA_FLOOR} healthy copies -- finishing")


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
              down_wait: int = DOWN_WAIT, boot_id: str | None = None,
              already_down: bool = False) -> bool:
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

    Given `boot_id` -- the node's boot id from before the reboot was issued --
    the transition stops being the test and becomes a fallback. Ready on a new
    boot id is proof the node rebooted, whether or not anyone was watching when
    it went down; Ready on the *same* boot id is not, however many NotReady
    polls preceded it, because a kubelet that merely blipped produces exactly
    that. `already_down` is for the rung that took the power away itself: there
    is no going-down left to observe, only a coming-back.
    """
    start = time.time()
    deadline = start + seconds
    down_deadline = start + down_wait
    saw_down = already_down
    beat = start
    while time.time() < deadline:
        ready = node_ready(node)
        if ready is None:
            # Not evidence of anything. Poll again rather than bank a
            # transition the cluster never actually reported.
            log("      cluster unreadable; not counting that as the node going down")
            beat = time.time()
            time.sleep(POLL)
            continue
        if not ready:
            if not saw_down:
                log("      node went NotReady (rebooting)")
                beat = time.time()
            saw_down = True
        else:
            now_boot = node_boot_id(node) if boot_id else None
            if now_boot and now_boot != boot_id:
                log("      node returned Ready (rebooted)")
                return True
            if saw_down and not now_boot:
                # Nothing to compare against -- fall back to the transition,
                # which is all this check ever had.
                log("      node returned Ready")
                return True
            if not saw_down and time.time() >= down_deadline:
                log(f"      node never went down after {down_wait}s -- reboot "
                    f"did not take; escalating without waiting out the full "
                    f"{seconds}s")
                return False
        # Say something on the way. The old loop announced NotReady once and
        # then polled in silence until the node returned or the window ran
        # out -- minutes of nothing, which reads exactly like a hung job.
        if time.time() - beat >= HEARTBEAT:
            beat = time.time()
            # What the cluster says now, not what this loop has decided. With a
            # boot id in play those differ, and the difference is the whole
            # point: a node reported Ready on the boot id it had before the
            # power cycle has not come back, it has not left yet.
            if ready:
                seen = "still Ready on the pre-reboot boot" if boot_id else "still Ready"
            else:
                seen = "still NotReady"
            log(f"      {seen}; {int(deadline - time.time())}s left in this rung "
                f"(Proxmox says: {vm_status(vm)})")
        time.sleep(POLL)

    if saw_down:
        log(f"      STUCK: never came back Ready on a new boot within {seconds}s "
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


def longhorn_manages(node: str) -> bool:
    """Does Longhorn run on this node at all?

    longhorn-manager creates a nodes.longhorn.io object for every node it lands
    on. A node Longhorn is kept off -- a control-plane node whose taint its
    DaemonSets do not tolerate, or one outside a node selector -- never gets
    one, never gets a csi-plugin pod, and holds no replicas for anything to
    depend on. Waiting for storage to come back there is waiting for something
    that was never there, which is exactly how the run this came from ended:
    fifteen minutes of "waiting for storage (longhorn-csi-plugin=False)" on a
    control-plane node, then an abort with two nodes still to do.

    Unreadable counts as managed. The wait it gates is bounded and no longer
    fatal on its own, so the cautious answer costs a few minutes at worst.
    """
    try:
        data = kubectl_json("get", "nodes.longhorn.io", "-n", "longhorn-system",
                            timeout=30)
    except (Abort, subprocess.TimeoutExpired):
        return True
    return any(i.get("metadata", {}).get("name") == node
               for i in data.get("items", []))


def wait_for_storage(node: str, require: tuple[str, ...], timeout: int) -> bool:
    """Wait until every named longhorn-system component is Running and ready here.

    Reports whether they came back; deciding what that is worth belongs to
    storage_back(), which knows what the volumes need.

    Split from a single "clean return" check because the two things worth
    waiting for do not live under the same constraint. longhorn-csi-plugin is a
    DaemonSet, so it tolerates the unschedulable taint and returns to a cordoned
    node by itself. instance-manager is not: Longhorn will not place one on a
    cordoned node, so waiting for it before the uncordon is a wait that can
    never end. They have to be waited on either side of it.
    """
    deadline = time.time() + timeout
    said = False
    beat = time.time()
    last = ""
    want = "+".join(require)
    while time.time() < deadline:
        ready = node_ready(node)
        if ready is not True and time.time() - beat >= HEARTBEAT:
            # Until the node is Ready this loop had nothing to say at all, so a
            # node still booting looked identical to a wedged script for up to
            # the whole timeout.
            beat = time.time()
            log(f"    waiting for {node} to come back "
                f"({'NotReady' if ready is False else 'cluster unreadable'}; "
                f"{int(deadline - time.time())}s left)")
        if ready is True:
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

            state = {name: _ready(name) for name in require}
            if all(state.values()):
                log(f"    {node}: {want} ready")
                return True
            shown = " ".join(f"{k}={v}" for k, v in state.items())
            if shown != last or time.time() - beat >= HEARTBEAT:
                # Once per change, then at the heartbeat. Polling every 15s and
                # printing every poll filled the run log that prompted this
                # with sixty copies of one line.
                beat = time.time()
                last = shown
                log(f"    {node} Ready; waiting for storage ({shown}; "
                    f"{int(deadline - time.time())}s left)")
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
    log(f"    {node}: {want} did not become ready within {timeout}s")
    return False


def storage_back(node: str, require: tuple[str, ...],
                 control_plane: bool = False) -> bool:
    """Wait for Longhorn's pods on `node`, and decide what their absence means.

    They are worth waiting for: a node whose csi-plugin has not registered
    cannot mount a Longhorn volume. They are the wrong thing to end a run over,
    and until now they were the only thing that could end one here -- a pod
    readiness check on the node just rebooted, given the authority to abort a
    cluster-wide maintenance run.

    What this job has to protect is redundancy: REPLICA_FLOOR healthy copies of
    every volume on nodes other than this one. That is a question about the
    volumes, it is already asked directly before each node goes down, and it is
    the question to ask here too. Three outcomes:

      * Longhorn does not run on this node. Nothing to wait for, nothing
        depends on it, move on. Control-plane nodes are that case by design
        here and are taken at their label: Longhorn is kept off them, so there
        is no point asking Longhorn about them. Asking is not free either --
        longhorn-manager's nodes.longhorn.io object outlives the pods, so a
        node Longhorn was taken off still has one, and the lookup would put a
        control-plane node back in the queue for a wait that cannot end.
      * Pods late, volumes fine. Say so loudly and carry on: losing this node
        outright would still leave every volume above the floor, so its pods
        being late endangers nothing.
      * Pods late and some volume is counting on this node. Now it matters, and
        now it stops the run -- which is what the old check was reaching for
        and had no way to express.
    """
    if not longhorn_installed():
        return True
    if control_plane:
        log(f"    {node}: control-plane -- Longhorn does not run here, "
            f"no storage to wait for")
        return True
    if not longhorn_manages(node):
        log(f"    {node}: Longhorn does not run here -- no storage to wait for")
        return True
    if wait_for_storage(node, require, STORAGE_TIMEOUT):
        return True

    want = "+".join(require)
    degraded, rebuilding, faulted = longhorn_state()
    if faulted:
        faulted = confirm_faulted(faulted, f"with {want} still down on {node}")
        if faulted:
            raise Abort(f"{faulted} Longhorn volume(s) still FAULTED and {want} "
                        f"has not come back on {node} -- stopping")
        # The counts above predate the window; re-read rather than report them.
        degraded, rebuilding, _ = longhorn_state()
    bad = unsafe_volumes(node)
    if bad:
        raise Abort(
            f"{node}: {want} did not come back within {STORAGE_TIMEOUT}s and "
            f"{len(bad)} volume(s) need this node to stay above "
            f"{REPLICA_FLOOR} copies: {shortfalls(bad, 3)}"
        )
    log(f"    {node}: {want} still down after {STORAGE_TIMEOUT}s, but every "
        f"volume holds {REPLICA_FLOOR} healthy copies without it "
        f"(degraded={degraded} rebuilding={rebuilding}) -- continuing")
    return False


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

    # Read before anything is issued: this is what "did it actually reboot"
    # compares against, on both rungs.
    boot = node_boot_id(node)

    # Rung 1 -- graceful, in-guest. Clean unmount of Longhorn replicas.
    log("    rung 1/2: systemctl reboot (in-guest)")
    ok, out = run_on_host(node, "nsenter -t 1 -m -u -i -n -p -- systemctl reboot",
                          timeout=60)
    if not ok:
        # Not fatal, and deliberately not read as "the reboot did not land":
        # the pod is racing the shutdown it just asked for and can lose that
        # race on a perfectly successful reboot. But when the node then never
        # goes down, this line is the difference between a guest that ignored
        # the reboot and a reboot nobody ever managed to ask for.
        log(f"      (could not confirm the reboot command ran: "
            f"{out.strip()[:120] or 'no output from the pod'})")
    if wait_back(node, vm, GRACEFUL_WAIT, boot_id=boot):
        return

    # Rung 2 -- power cycle via the hypervisor. Deliberately NOT /status/reboot:
    # ACPI asks the guest to shut down and then waits for it to do so, which
    # means the exact case this rung exists for -- a guest too wedged to answer
    # -- is the case where it can sit there until something gives up. Stop kills
    # the QEMU process, so it lands whatever the guest thinks.
    #
    # Unclean by construction, which is why rung 1 goes first: by the time we
    # are here the guest has already failed to reboot itself, so there is no
    # clean unmount left to preserve.
    log("    rung 2/2: Proxmox power cycle (stop + start)")
    assert_identity(node, vm)  # re-verify identity right before destroying state
    try:
        vm.host.call(f"{base}/status/stop", method="POST")
    except Exception as exc:  # noqa: BLE001
        raise Abort(
            f"{node}: Proxmox refused to stop VM {vm.host.name}:{vm.vmid} "
            f"({exc}). The node is drained and cordoned; it needs a hand."
        )

    deadline = time.time() + HARD_STOP_WAIT
    beat = time.time()
    while time.time() < deadline:
        st = vm_status(vm)
        if st == "stopped":
            break
        if time.time() - beat >= HEARTBEAT:
            beat = time.time()
            log(f"      waiting for VM to stop (Proxmox says: {st}; "
                f"{int(deadline - time.time())}s left)")
        time.sleep(5)
    else:
        raise Abort(
            f"{node}: VM {vm.host.name}:{vm.vmid} did not reach 'stopped' within "
            f"{HARD_STOP_WAIT}s; refusing to start it in an unknown state"
        )

    log("      VM stopped; starting")
    vm.host.call(f"{base}/status/start", method="POST")
    log("      start issued; waiting for the node to come back")

    # This rung used to end here, on the word of the Proxmox API that a start
    # had been issued, and everything after it ran against whatever the cluster
    # happened to say about a node that was still powering on -- which, for the
    # first half minute, is the Ready it was carrying when the power went. So
    # wait for the node itself, and take a new boot id as the proof: there is no
    # going-down left to watch for, we took it down ourselves.
    if not wait_back(node, vm, RETURN_TIMEOUT, down_wait=RETURN_TIMEOUT,
                     boot_id=boot, already_down=True):
        raise Abort(
            f"{node}: did not come back Ready within {RETURN_TIMEOUT // 60}m of "
            f"the power cycle (Proxmox says: {vm_status(vm)}). It is drained and "
            f"cordoned; it needs a hand."
        )


def process_node(node: str, vm: VmRef, dry_run: bool, forced: bool = False,
                 budget_left: float | None = None,
                 control_plane: bool = False) -> bool:
    if forced:
        log(f"  {node}: FORCED -- treating as needing a reboot (test override)")
    else:
        log(f"  {node}: reboot required ({reboot_reason(node)})")

    # Not "is the cluster spotless" but "does removing this node still leave
    # every volume with copies to spare". A rebuild elsewhere is none of this
    # node's business.
    wait_safe_to_reboot(node, SETTLE_TIMEOUT, budget_left)

    assert_identity(node, vm)

    if dry_run:
        log(f"    DRY-RUN: would cordon, drain, reboot, uncordon {node}")
        return True

    log("    cordon + drain")
    kubectl("cordon", node)
    try:
        drain_node(node)
    except (Abort, subprocess.TimeoutExpired) as exc:
        kubectl("uncordon", node, check=False)
        raise Abort(f"drain of {node} failed ({exc}); uncordoned, stopping run")

    reboot_node(node, vm, dry_run)

    # Before uncordoning: CSI should be registered, or the first pod scheduled
    # here fails its mount. This is the gap the original check existed for, and
    # a DaemonSet returns to a cordoned node on its own, so it can be waited on
    # while the node is still fenced off.
    storage = storage_back(node, ("longhorn-csi-plugin",), control_plane)

    log("    uncordon")
    kubectl("uncordon", node)

    # Only now can this be asked for. Longhorn does not place an
    # instance-manager on a cordoned node, and the drain deleted the one that
    # was here, so asking for it any earlier waits out the timeout for nothing.
    storage = storage_back(node, ("instance-manager",), control_plane) and storage

    if storage:
        log(f"  {node}: DONE (rebuilds may still be running; the next node waits "
            f"on redundancy, not on silence)")
    else:
        # Uncordoned anyway. Leaving it fenced off would keep Longhorn from
        # placing the very pods it is waiting for, and the volumes have already
        # said they do not need this node.
        log(f"  {node}: DONE, but Longhorn has not come back on it. No volume "
            f"needs it -- that was just checked -- so the run continues; the "
            f"node carries no replicas until Longhorn is back.")
    return storage


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
        # long as possible. The same set tells process_node which nodes to skip
        # the Longhorn waits on -- storage is kept off the control plane here.
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
        no_storage: list[str] = []
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
            if not process_node(name, plan.vms[name], dry_run,
                                forced=force_all or name in force_names,
                                budget_left=None if dry_run else left,
                                control_plane=name in cp):
                no_storage.append(name)
            if not dry_run:
                done.append(time.time() - t0)
                log(f"  ({len(done)}/{len(candidates)} done, {done[-1] / 60:.0f}m "
                    f"for {name}, {(RUN_BUDGET - (time.time() - started)) / 60:.0f}m "
                    f"budget left)")

        # Per-node gating lets the run move on while rebuilds finish, so the
        # last one can still be in flight. Settle once here, because "Cluster
        # healthy" has to be true when it is printed.
        if not dry_run:
            log("All nodes done; waiting for Longhorn to finish rebuilding")
            # Capped like every other wait: at an hour, this one can outlast the
            # job's own 360m limit, and being killed here means no failure mail
            # (a cancelled job skips if: failure()) even though every node is
            # already back and uncordoned.
            wait_longhorn_settled(SETTLE_TIMEOUT,
                                  budget_left=RUN_BUDGET - (time.time() - started))
        if no_storage:
            # Worth one line at the top level. The run was right to continue --
            # every volume kept its copies elsewhere, which is the only reason
            # it did -- but a node Longhorn never came back on is carrying no
            # replicas, and nothing else is going to mention it.
            log(f"NOTE: Longhorn did not come back on {no_storage}. Every volume "
                f"held {REPLICA_FLOOR} healthy copies without those nodes, so the "
                f"run continued; they are worth a look.")
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
