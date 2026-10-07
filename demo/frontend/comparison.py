"""RAM-only, source-workspace-owned comparison snapshots and isolated model actors."""
import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone

from fastapi import Request


def install(app, workspace_for, read_json, validate, problem, limit):
    @app.post("/api/compare")
    async def create(request: Request):
        source = workspace_for(request)
        raw = await read_json(request, limit)
        source = workspace_for(request)
        body, attachments = validate(raw, source)
        from agent import turn_policy
        policy = turn_policy(body["messages"][-1]["content"], raw.get("tools", False))
        if policy["external"]:
            await app.state.web_iq.prepare()
            source = workspace_for(request)
            body, attachments = validate(raw, source)
        mode = raw.get("mode", "independent")
        context = raw.get("context", "")
        if not isinstance(mode, str) or mode not in {"independent", "common", "continue"}:
            raise problem(400, "comparison_mode", "지원하지 않는 비교 문맥 모드입니다.")
        if not isinstance(context, str) or len(context.encode()) > 64 * 1024:
            raise problem(400, "comparison_context", "공통 문맥은 64 KiB 이하여야 합니다.")
        records = getattr(source, "comparisons", {})
        if any(actor["space"].busy for row in records.values() for actor in row["actors"].values()):
            raise problem(409, "comparison_busy", "이 비교 대화의 이전 턴이 아직 실행 중입니다.")
        if len(records) >= 8:
            raise problem(413, "comparison_limit", "비교는 대화당 8회까지 보존됩니다. 새 대화를 시작해 주세요.")
        prior_id = raw.get("previous")
        if prior_id is not None and (not isinstance(prior_id, str) or len(prior_id) != 32):
            raise problem(400, "comparison_previous", "이전 비교 식별자가 올바르지 않습니다.")
        previous = records.get(prior_id)
        if mode == "continue" and raw.get("previous") and previous is None:
            raise problem(410, "comparison_previous", "이 대화에 속한 이전 비교가 없습니다.")
        identifier = secrets.token_hex(16)
        snapshot = {"id":identifier, "utc":datetime.now(timezone.utc).isoformat(), "mode":mode,
                    "context":context if mode == "common" else "", "attachments":attachments,
                    "tools":raw.get("tools", False),
                    "web_iq":copy.deepcopy(app.state.web_iq.status()),
                    "body":dict(body, messages=[body["messages"][-1]]),
                    "files":[{"path":name, "bytes":len(data), "sha256":hashlib.sha256(data).hexdigest()}
                             for name, data in sorted(source.files.items())],
                    "upload_versions":dict(source.upload_versions),
                    "pdf_call_ids":{p:hashlib.sha256((identifier + p).encode()).hexdigest()[:24]
                                    for p in attachments if p.lower().endswith(".pdf")}}
        snapshot["input_sha256"] = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        actors = {}
        source.busy = True
        try:
            for name, profile in app.state.profiles.items():
                try:
                    target = profile.state.workspaces.create()
                except ValueError as exc:
                    raise problem(503, "workspace_capacity", str(exc)) from exc
                actors[name] = {"profile":profile, "space":target, "started":False}
                if mode == "continue" and previous:
                    before = previous["actors"][name]["space"]
                    if before.closed or before.token not in profile.state.workspaces.items:
                        raise problem(410, "comparison_previous", "이전 모델 대화가 만료되었습니다.")
                    target.files, target.original = dict(before.files), dict(before.original)
                    target.history = copy.deepcopy(before.history)
                    target.tool_events = copy.deepcopy(before.tool_events)
                    target.pdf_checked, target.pdf_outcomes = set(before.pdf_checked), copy.deepcopy(before.pdf_outcomes)
                elif mode == "common" and context:
                    target.history = [{"role":"user", "content":context}]
                for path, data in source.files.items():
                    prior_version = previous["snapshot"].get("upload_versions", {}).get(path) if previous else None
                    new_upload = (mode == "continue" and previous
                                  and source.upload_versions.get(path) != prior_version)
                    if path not in target.files or new_upload:
                        try:
                            target.put(path, data, original=True)
                        except ValueError as exc:
                            raise problem(413, "comparison_files", str(exc)) from exc
                        if new_upload:
                            target.original[path] = source.original.get(path, data)
                            target.pdf_checked.discard(path)
                            target.pdf_outcomes.pop(path, None)
                target.comparison_snapshot = copy.deepcopy(snapshot)
            source.comparisons = records
            records[identifier] = {"snapshot":snapshot, "actors":actors}
        except BaseException:
            for actor in actors.values():
                actor["space"].closed = True
                actor["profile"].state.workspaces.items.pop(actor["space"].token, None)
            raise
        finally:
            source.busy = False
        return {"id":identifier, "snapshot":snapshot, "input_difference":mode == "continue" and bool(previous),
                "actors":[{"model":name, "workspace":actor["space"].token,
                           "api_prefix":"" if name == "fp8" else "/models/nvfp4",
                           "stream":f"/api/compare/{identifier}/{name}"}
                          for name, actor in actors.items()]}

    @app.post("/api/compare/{identifier}/{model}")
    async def stream(identifier: str, model: str, request: Request):
        from agent import agent_response
        source = workspace_for(request, allow_busy=True)
        record = getattr(source, "comparisons", {}).get(identifier)
        actor = record["actors"].get(model) if record else None
        if actor is None:
            raise problem(404, "comparison", "이 대화에 속한 비교 모델 실행이 없습니다.")
        if record.get("cancelled"):
            raise problem(409, "comparison_cancelled", "이 비교는 중단되었습니다. 새 요청으로 시작해 주세요.")
        if actor["started"]:
            raise problem(409, "comparison_used", "이미 실행한 비교입니다. 반복 비교는 새 요청으로 시작해 주세요.")
        actor["started"] = True
        profile, space, snapshot = actor["profile"], actor["space"], record["snapshot"]
        if space.closed or space.token not in profile.state.workspaces.items:
            raise problem(410, "comparison", "비교 모델 작업공간이 만료되었습니다.")
        if getattr(space, "cancel_requested", False):
            raise problem(409, "comparison_cancelled", "이 모델 실행은 중단되었습니다.")
        if not profile.state.is_ready():
            raise problem(503, "not_ready", "이 모델은 아직 준비되지 않았습니다. 다른 모델로 대체하지 않습니다.")
        space.busy = True
        return agent_response(profile.state.hub, profile.state.gate, copy.deepcopy(snapshot["body"]),
                              space, snapshot["tools"], attachment_paths=snapshot["attachments"],
                              web_iq=profile.state.web_iq, snapshot=snapshot, tool_gate=profile.state.tool_gate)
