"""Profile/snapshot/lifecycle contracts; deterministic transport, genuine MAF tools."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from tests.test_demo_agent import Config, FixtureHub, FixtureLink, ROOT, create_app
from supervisor import AzureML
from tests.pdf_samples import text_pdf


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.app = create_app(Config(data=Path(self.folder.name), aml_dir=ROOT / "aml",
            session_secret="test", open_demo=True, workspaces={"uksouth":"workspace"}), start_supervisor=False)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.source = self.app.state.workspaces.create()
        self.headers = {"X-AX-Workspace":self.source.token}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.folder.cleanup()

    async def create(self, **extra):
        raw = {"messages":[{"role":"user","content":"2^64-1 계산"}], "tools":True,
               "thinking":False, "max_tokens":512, **extra}
        return await self.client.post("/api/compare", headers=self.headers, json=raw)

    def record(self, run):
        return self.source.comparisons[run["id"]]

    async def test_profiles_isolate_state_gate_workspace_and_share_auth_archive(self):
        nv = self.app.state.profiles["nvfp4"]
        self.app.state.store.data["desired"] = "on"
        self.app.state.store.data["active"] = "existing-fp8"
        self.app.state.store.save()
        self.assertEqual(nv.state.store.data["desired"], "off")
        self.assertIs(nv.state.auth, self.app.state.auth)
        self.assertIs(nv.state.supervisor.archives, self.app.state.supervisor.archives)
        self.assertIsNot(nv.state.gate, self.app.state.gate)
        self.assertIsNot(nv.state.workspaces, self.app.state.workspaces)
        again = create_app(self.app.state.config, start_supervisor=False)
        self.assertEqual(again.state.store.data["active"], "existing-fp8")
        self.assertEqual(again.state.profiles["nvfp4"].state.store.data["desired"], "off")
        response = await self.client.get("/api/models")
        self.assertEqual({m["id"] for m in response.json()["models"]}, {"fp8","nvfp4"})
        self.assertEqual((await self.client.post("/models/unknown/api/agent", json={})).status_code, 404)

    async def test_snapshot_same_actual_maf_input_and_tools_then_single_use(self):
        self.source.put("project.txt", b"public sample", original=True)
        run = (await self.create(attachments=["project.txt"],
            generation={"temperature":0.3,"top_p":0.8})).json()
        record = self.record(run)
        links = {}
        for name, actor in record["actors"].items():
            links[name] = FixtureLink()
            actor["profile"].state.hub = FixtureHub(links[name])
            actor["profile"].state.is_ready = lambda: True
        responses = await asyncio.gather(*(self.client.post(actor["stream"], headers=self.headers, json={})
                                          for actor in run["actors"]))
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertIn('"state": "success"', response.text)
            self.assertIn("18446744073709551615", response.text)
            self.assertIn('"input_tokens": null', response.text)
            self.assertIn("event: metrics", response.text)
        self.assertEqual(links["fp8"].requests[0], links["nvfp4"].requests[0])
        self.assertEqual(links["fp8"].requests[0]["temperature"], 0.3)
        self.assertNotEqual(run["actors"][0]["workspace"], run["actors"][1]["workspace"])
        replay = await self.client.post(run["actors"][0]["stream"], headers=self.headers, json={})
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.source.files["project.txt"], b"public sample")

    async def test_each_history_and_file_continuation_is_separate_independent_is_fresh(self):
        self.source.put("project.txt", b"original", original=True)
        first = (await self.create()).json()
        for name, actor in self.record(first)["actors"].items():
            actor["space"].put("project.txt", name.encode())
            actor["space"].history = [{"role":"assistant", "content":name + " actual prior result"}]
        second = (await self.create(mode="continue", previous=first["id"])).json()
        self.assertTrue(second["input_difference"])
        for name, actor in self.record(second)["actors"].items():
            self.assertEqual(actor["space"].get("project.txt"), name.encode())
            self.assertEqual(actor["space"].history[0]["content"], name + " actual prior result")
            self.assertEqual(actor["space"].original["project.txt"], b"original")
        independent = (await self.create(mode="independent", previous=second["id"])).json()
        for actor in self.record(independent)["actors"].values():
            self.assertEqual(actor["space"].history, [])
            self.assertEqual(actor["space"].get("project.txt"), b"original")

    async def test_cancel_before_actor_starts_and_close_revokes_both_model_tokens(self):
        self.source.put("sample.txt", b"private test", original=True)
        run = (await self.create()).json()
        self.assertEqual((await self.client.post("/api/workspace/cancel", headers=self.headers)).status_code, 200)
        self.assertEqual((await self.client.post(run["actors"][0]["stream"], headers=self.headers, json={})).status_code, 409)
        foreign = self.app.state.workspaces.create()
        response = await self.client.post(run["actors"][0]["stream"],
            headers={"X-AX-Workspace":foreign.token}, json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.client.post("/api/workspace/close", headers=self.headers)).status_code, 200)
        for actor in run["actors"]:
            response = await self.client.get(actor["api_prefix"] + "/api/workspace/download?path=sample.txt",
                headers={"X-AX-Workspace":actor["workspace"]})
            self.assertEqual(response.status_code, 410)

    async def test_one_side_not_ready_never_falls_back_and_capacity_rolls_back(self):
        run = (await self.create()).json()
        result = await self.client.post(run["actors"][1]["stream"], headers=self.headers, json={})
        self.assertEqual(result.status_code, 503)
        self.assertIn("대체하지", result.json()["message"])
        self.assertFalse(self.record(run)["actors"]["fp8"]["started"])
        nv = self.app.state.profiles["nvfp4"]
        while len(nv.state.workspaces.items) < 16:
            nv.state.workspaces.create()
        before = len(self.app.state.workspaces.items)
        result = await self.create()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(len(self.app.state.workspaces.items), before)
        self.assertFalse(self.source.busy)

    async def test_pdf_actual_preflight_has_identical_first_model_input(self):
        self.source.upload("sample.pdf", text_pdf())
        run = (await self.create(attachments=["sample.pdf"])).json()
        links = {}
        for name, actor in self.record(run)["actors"].items():
            links[name] = FixtureLink()
            actor["profile"].state.hub = FixtureHub(links[name])
            actor["profile"].state.is_ready = lambda: True
        responses = await asyncio.gather(*(self.client.post(actor["stream"], headers=self.headers, json={})
                                          for actor in run["actors"]))
        for response in responses:
            self.assertIn('"name": "read_pdf"', response.text)
            self.assertIn('"state": "success"', response.text)
            self.assertIn("sample.pdf [page 1]", response.text)
        self.assertEqual(links["fp8"].requests[0], links["nvfp4"].requests[0])

    async def test_common_context_locked_actors_and_reuploaded_file_version(self):
        self.source.upload("sample.pdf", text_pdf())
        first = (await self.create(mode="common", context="Same trusted context")).json()
        for actor in self.record(first)["actors"].values():
            self.assertEqual(actor["space"].history, [{"role":"user", "content":"Same trusted context"}])
            actor["space"].pdf_checked.add("sample.pdf")
            locked = await self.client.post(
                ("" if actor["profile"] is self.app else "/models/nvfp4") + "/api/workspace/upload?name=x.txt",
                headers={"X-AX-Workspace":actor["space"].token}, content=b"alter snapshot")
            self.assertEqual(locked.status_code, 409)
        await self.client.post("/api/workspace/remove", headers=self.headers, json={"files":["sample.pdf"]})
        replacement = text_pdf(["Replacement public content"])
        self.source.upload("sample.pdf", replacement)
        second = (await self.create(mode="continue", previous=first["id"])).json()
        for actor in self.record(second)["actors"].values():
            self.assertEqual(actor["space"].get("sample.pdf"), replacement)
            self.assertNotIn("sample.pdf", actor["space"].pdf_checked)

    async def test_expiry_clears_retained_actor_references_and_snapshot(self):
        self.source.upload("private.txt", b"private local fixture")
        run = (await self.create()).json()
        actors = list(self.record(run)["actors"].values())
        for actor in actors:
            actor["space"].history = [{"role":"user", "content":"private local fixture"}]
            actor["space"].touched = time.monotonic() - 1900
        self.source.touched = time.monotonic() - 1900
        self.app.state.workspaces.prune()
        self.assertNotIn(self.source.token, self.app.state.workspaces.items)
        self.assertFalse(self.source.comparisons)
        for actor in actors:
            self.assertTrue(actor["space"].closed)
            self.assertFalse(actor["space"].files)
            self.assertFalse(actor["space"].history)
            self.assertIsNone(actor["space"].comparison_snapshot)
            self.assertNotIn(actor["space"].token, actor["profile"].state.workspaces.items)

    async def test_invalid_generation_modes_and_previous_ids(self):
        for extra in [{"max_tokens":True}, {"mode":[]}, {"previous":[]},
                      {"generation":{"temperature":True}}, {"generation":{"top_p":0}}]:
            with self.subTest(extra=extra):
                self.assertEqual((await self.create(**extra)).status_code, 400)

    def test_shared_compute_node_accounting_filters_actual_run_ids(self):
        nodes = [SimpleNamespace(runId="fp8-job", nodeState="running"),
                 SimpleNamespace(runId="fp8-job", nodeState="running"),
                 SimpleNamespace(runId="nv-job", nodeState="running"),
                 SimpleNamespace(runId="other-job", nodeState="preempted")]
        azure = AzureML(self.app.state.config)
        azure.client = lambda region: SimpleNamespace(compute=SimpleNamespace(list_nodes=lambda name:nodes))
        self.assertEqual(azure.nodes_for("uksouth", ["fp8-job"]), (2,2))
        self.assertEqual(azure.nodes_for("uksouth", ["nv-job"]), (1,1))
        self.assertEqual(azure.nodes_for("uksouth", []), (0,0))
