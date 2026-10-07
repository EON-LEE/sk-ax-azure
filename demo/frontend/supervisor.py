"""Keeps one GPU job running while the demo is switched on; a port of aml/first_capacity_wins.sh.

Every tick (45 s, or at once when poked) it reads the status of each submitted job and the node count of
every cluster that has a job or still had nodes. It then applies these rules:
- OFF: cancel every job. Clusters are never deleted. They scale to zero after their idle time and keep their
  identity and storage role assignments.
- ON with an active job: keep it while it lives.
  - A job in a terminal state is dropped. Its region is retried alone first, because a crashed server leaves
    warm nodes behind. After the retry window every region races.
  - The active job is also dropped if it has had no link for stale_limit seconds and its cluster holds fewer
    usable nodes than the job needs. Every region then races.
- ON without an active job:
  - The first job that qualifies wins: its link said hello, its cluster holds all the nodes, or it is running.
    Every other job is cancelled.
  - A cluster that holds only some of the nodes for longer than partial_limit is released, and its region
    cools down.
  - Each region without a job gets one; while retrying, only the last region does. A region is skipped
    during its cooldown after a failed submission.
Node-seconds per region are integrated from the polled counts to estimate the cost. SDK calls run in worker
threads; the state changes only on the event loop.
"""
import asyncio
import base64
import importlib.util
import os
import re
import secrets
import tempfile
import time

from store import token_digest
from sources import SourceArchives

RUNNING = {"Preparing", "Running", "Finalizing"}
TERMINAL = {"Failed", "Canceled", "CancelRequested", "Completed", "NotResponding"}
GONE = {"leaving", "preempted", "unusable"}


def short(exc):
    return f"{type(exc).__name__}: {exc}"[:300]


def node_state(node):
    """azure-ai-ml 1.35 copies the REST fields onto AmlComputeNodeInfo under their wire names (nodeState), so
    its node_state attribute stays None; read either."""
    fields = vars(node)
    return str(fields.get("node_state") or fields.get("nodeState") or "").lower()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AzureML:
    """The few azure-ai-ml calls the supervisor makes, one MLClient per region, as the app's managed identity."""

    def __init__(self, config):
        self.config, self.clients, self.credential = config, {}, None

    def client(self, region):
        if region not in self.clients:
            from azure.ai.ml import MLClient
            from azure.identity import ManagedIdentityCredential

            self.credential = self.credential or ManagedIdentityCredential()
            self.clients[region] = MLClient(self.credential, self.config.subscription, self.config.resource_group,
                                            self.config.workspaces[region])
        return self.clients[region]

    def status(self, region, name):
        return self.client(region).jobs.get(name).status

    def facts(self, region, name):
        job = self.client(region).jobs.get(name)
        values = job.environment_variables or {}
        revision = re.findall(r"hf:([0-9a-f]{40})", job.command or "")
        return {"checkpoint":values.get("FULL_REPO") or "skt/A.X-K2",
                "revision":revision[-1] if revision else None,
                "tp":int(values["TP"]), "pp":int(values["PP"]),
                "nodes":job.resources.instance_count,
                "source_sha":values.get("AXK2_SRC_SHA256"),
                "evidence":"actual AML job command/environment/resource configuration"}

    def nodes(self, region):
        nodes = list(self.client(region).compute.list_nodes(self.config.compute))
        usable = [n for n in nodes if node_state(n) not in GONE]
        return len(nodes), len(usable)

    def nodes_for(self, region, jobs):
        names = set(jobs)
        nodes = [n for n in self.client(region).compute.list_nodes(self.config.compute)
                 if (vars(n).get("run_id") or vars(n).get("runId")) in names]
        return len(nodes), sum(node_state(n) not in GONE for n in nodes)

    def submit(self, region, text):
        from azure.ai.ml import load_job

        handle, path = tempfile.mkstemp(suffix=".yml")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(text)
            job = load_job(source=path)
        finally:
            os.unlink(path)
        return self.client(region).jobs.create_or_update(job).name

    def cancel(self, region, name):
        self.client(region).jobs.begin_cancel(name)


class Supervisor:
    def __init__(self, config, store, hub, azure=None, clock=time.time, archives=None):
        self.config, self.store, self.hub = config, store, hub
        self.azure, self.clock = azure or AzureML(config), clock
        self.wake, self.renderer, self.source = asyncio.Event(), None, b""
        self.archives = archives or SourceArchives(config.data / "sources")

    def poke(self):
        self.wake.set()

    def usable(self, region):
        return (self.store.data["nodes"].get(region) or {}).get("usable", 0)

    def prepare(self):
        if self.renderer is None:
            module = load_module("axk2_render_job", self.config.aml_dir / "render_job.py")
            template = (self.config.aml_dir / "jobs" / self.config.template).read_text(encoding="utf-8")
            encoded = module.payload()
            self.source = base64.b64decode(encoded)  # served at /api/link/src; the job checks its digest
            self.archives.put(self.source)
            self.renderer = lambda values: module.render(template, encoded, values)

    def job_text(self, token):
        self.prepare()
        return self.renderer({"AXK2_LINK_URL": self.config.link_url, "AXK2_LINK_TOKEN": token})

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except Exception as exc:
                self.store.event("error", f"supervisor tick failed: {short(exc)}", save=False)
            try:
                self.store.save()
            except OSError:
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval())
            except asyncio.TimeoutError:
                pass

    def interval(self):
        data = self.store.data
        busy = (data["desired"] == "on" or data["jobs"]
                or any((n or {}).get("total") for n in data["nodes"].values()))
        return self.config.interval if busy else self.config.idle_interval

    # ------------------------------------------------------------------------------------------- one pass
    async def tick(self):
        data = self.store.data
        await self.refresh()
        now = self.clock()
        if data["desired"] != "on":
            for digest in list(data["jobs"]):
                await self.release(digest, "the demo was switched off")
            data["restart"] = False
            return
        if data["restart"]:
            data["restart"] = False
            if data["active"]:
                await self.release(data["active"], "restart requested")
        active = data["active"] if data["active"] in data["jobs"] else None
        data["active"] = active
        if active:
            job = data["jobs"][active]
            if active in self.hub.links:
                job["link_seen"] = now
            elif (now - (job.get("link_seen") or data["active_since"] or now) > self.config.stale_limit
                  and self.usable(job["region"]) < self.config.nodes):
                await self.release(active, f"no link for over {self.config.stale_limit} s and too few nodes",
                                   race=True)
                active = None
        if active:
            for digest in [d for d in data["jobs"] if d != active]:
                await self.release(digest, "another job is active")
            return
        winner = self.winner()
        if winner:
            self.promote(*winner)
            for digest in [d for d in data["jobs"] if d != winner[0]]:
                await self.release(digest, "another region won")
            return
        await self.drop_partials()
        await self.submit_missing()

    async def refresh(self):
        data = self.store.data
        for digest, job in list(data["jobs"].items()):
            if not job.get("name"):
                continue
            try:
                status = await asyncio.to_thread(self.azure.status, job["region"], job["name"])
            except Exception as exc:
                self.store.event("error", f"{job['region']}: reading {job['name']} failed: {short(exc)}", save=False)
                continue
            if data["jobs"].get(digest) is not job:
                continue  # released while we waited
            if status != job.get("status"):
                job["status"] = status
                self.store.event("job", f"{job['region']} {job['name']}: {status}", save=False)
            if not job.get("facts") and hasattr(self.azure, "facts"):
                try:
                    job["facts"] = await asyncio.to_thread(self.azure.facts, job["region"], job["name"])
                except (ValueError, KeyError, AttributeError) as exc:
                    self.store.event("error", f"{job['region']}: job provenance unavailable: {short(exc)}", save=False)
            if status in TERMINAL:
                await self.release(digest, f"the job ended ({status})", cancel=False)
        regions = {job["region"] for job in data["jobs"].values()}
        regions |= {region for region, nodes in data["nodes"].items() if (nodes or {}).get("total")}
        for region in sorted(regions):
            if region not in self.config.workspaces:
                data["nodes"].pop(region, None)
                continue
            try:
                names = [j["name"] for j in data["jobs"].values() if j["region"] == region and j.get("name")]
                total, usable = await asyncio.to_thread(self.azure.nodes_for, region, names)
            except Exception as exc:
                self.store.event("error", f"{region}: listing nodes failed: {short(exc)}", save=False)
                continue
            now = self.clock()
            previous = data["nodes"].get(region) or {}
            if previous.get("t"):
                seconds = data["cost"]["node_seconds"]
                seconds[region] = round(seconds.get(region, 0) + previous.get("total", 0) * max(0, now - previous["t"]),
                                        1)
            if total != previous.get("total"):
                self.store.event("nodes", f"{region}: {total} node(s), {usable} usable", save=False)
            data["nodes"][region] = {"total": total, "usable": usable, "t": round(now, 1)}

    def winner(self):
        data, best = self.store.data, None
        for digest, job in data["jobs"].items():
            if not job.get("name"):
                continue
            if digest in self.hub.links:
                return digest, "its link connected"
            if best is None and self.usable(job["region"]) >= self.config.nodes:
                best = digest, f"its cluster holds {self.config.nodes} nodes"
            elif best is None and job.get("status") in RUNNING:
                best = digest, f"the job is {job['status']}"
        return best

    def promote(self, digest, why):
        data = self.store.data
        job = data["jobs"].get(digest)
        if job is None or data["active"] == digest:
            return False
        if data["active"] in data["jobs"]:
            return False  # one active job at a time; the supervisor releases the others
        data["active"], data["active_since"], data["last_region"] = digest, round(self.clock(), 1), job["region"]
        self.store.event("active", f"{job['region']} {job['name']}: active ({why})", save=False)
        return True

    async def release(self, digest, why, cancel=True, race=False):
        data = self.store.data
        job = data["jobs"].pop(digest, None)
        if job is None:
            return
        if data["active"] == digest:
            data["active"] = data["active_since"] = None
            data["last_region"] = job["region"]
            data["mode"], data["mode_since"] = ("race" if race else "retry"), round(self.clock(), 1)
        self.hub.drop(digest)
        self.store.event("release", f"{job['region']} {job.get('name') or '(submitting)'}: {why}", save=False)
        if cancel and job.get("name") and job.get("status") not in TERMINAL:
            try:
                await asyncio.to_thread(self.azure.cancel, job["region"], job["name"])
            except Exception as exc:
                self.store.event("error", f"{job['region']}: cancelling {job['name']} failed: {short(exc)}",
                                 save=False)

    async def drop_partials(self):
        data = self.store.data
        for digest, job in list(data["jobs"].items()):
            usable, now = self.usable(job["region"]), self.clock()
            if 0 < usable < self.config.nodes:
                since = job.setdefault("partial_since", round(now, 1))
                if now - since > self.config.partial_limit:
                    data["cooldown"][job["region"]] = round(now + self.config.cooldown, 1)
                    await self.release(digest, f"only {usable} of {self.config.nodes} nodes for over "
                                               f"{self.config.partial_limit} s")
            else:
                job.pop("partial_since", None)

    async def submit_missing(self):
        data, now = self.store.data, self.clock()
        cooled = {region for region, until in data["cooldown"].items() if until > now}
        last = data["last_region"]
        if (data["mode"] == "retry" and last in self.config.workspaces and last not in cooled
                and now - (data["mode_since"] or now) < self.config.retry_window):
            targets = [last]
        else:
            if data["mode"] != "race":
                data["mode"], data["mode_since"] = "race", round(now, 1)
                self.store.event("race", "submitting to every region; the first with capacity wins", save=False)
            targets = list(self.config.workspaces)
        for region in targets:
            busy = {job["region"] for job in data["jobs"].values()}
            if data["desired"] != "on" or data["active"]:
                return
            if region not in busy and region not in cooled:
                await self.submit(region)

    async def submit(self, region):
        data = self.store.data
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        data["jobs"][digest] = {"region": region, "name": None, "submitted": round(self.clock(), 1),
                                "status": "Submitting"}
        self.store.save()  # node 0 may dial in before create_or_update returns
        try:
            text = self.job_text(token)
            if self.source:
                data["jobs"][digest]["source_sha"] = self.archives.put(self.source)
                self.store.save()
            name = await asyncio.to_thread(self.azure.submit, region, text)
        except Exception as exc:
            data["jobs"].pop(digest, None)
            data["cooldown"][region] = round(self.clock() + self.config.cooldown, 1)
            self.store.event("error", f"{region}: submission failed: {short(exc)}", save=False)
            return
        job = data["jobs"].get(digest)
        if job is None:  # switched off while submitting
            try:
                await asyncio.to_thread(self.azure.cancel, region, name)
            except Exception as exc:
                self.store.event("error", f"{region}: cancelling {name} failed: {short(exc)}", save=False)
            return
        job.update(name=name, status="Queued")
        self.store.event("submit", f"{region}: submitted {name}", save=False)
