# -*- coding: utf-8 -*-
"""
Tornado Web 服务。

HTTP API:
    GET  /api/state
    GET  /api/configs
    POST /api/config/select        body: {"name": "..."}
    POST /api/configs              body: {"name": "..."} + ?overwrite=true 确认覆盖
    POST /api/devices/{id}/parameters:batch
                                   body: {"baseRevision": int, "parameters": {...}}
                                         + "force": true 忽略冲突
    POST /api/devices/{id}/outputs:clear
    WS   /ws                       状态推送

错误码：
    400 参数/配置格式错误（invalid_parameter / not_found / unknown）
    409 版本冲突、故障状态、文件已存在（conflict / fault / exists）
    503 硬件命令队列满、服务未就绪（busy / unavailable）
    500 未知内部错误

写操作经 ControlHub 命令队列（executor 线程池提交，不阻塞 IOLoop）。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import tornado.ioloop
import tornado.web
import tornado.websocket
from tornado.concurrent import run_on_executor

from .commands import Command, CommandError
from .config_store import ConfigStore
from .hub import ControlHub

logger = logging.getLogger(__name__)

_STATUS_FOR_CODE = {
    "invalid_parameter": 400,
    "not_found": 400,
    "unknown": 400,
    "conflict": 409,
    "fault": 409,
    "exists": 409,
    "busy": 503,
    "unavailable": 503,
}


def error_status(e: CommandError) -> int:
    return _STATUS_FOR_CODE.get(e.code, 500)


class BaseHandler(tornado.web.RequestHandler):
    executor = ThreadPoolExecutor(max_workers=8)

    @property
    def hub(self) -> ControlHub:
        return self.settings["hub"]

    @property
    def store(self) -> ConfigStore:
        return self.settings["config_store"]

    @run_on_executor
    def _submit(self, cmd: Command) -> Dict[str, Any]:
        """在 executor 线程同步等待命令结果，避免阻塞 IOLoop。"""
        return self.hub.submit(cmd)

    def write_json(self, obj: Any, status: int = 200) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.finish(json.dumps(obj, ensure_ascii=False))

    def write_error_json(self, e: CommandError) -> None:
        self.write_json(
            {"ok": False, "error": e.message, "code": e.code},
            status=error_status(e))

    def read_json_body(self) -> Any:
        try:
            return json.loads(self.request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CommandError("请求体必须是合法 JSON", "invalid_parameter")


class StateHandler(BaseHandler):
    def get(self):
        self.write_json(self.hub.get_state_snapshot())


class ConfigsHandler(BaseHandler):
    def get(self):
        self.write_json({"configs": self.store.list()})

    async def post(self):
        """保存当前 applied state 为配置。"""
        try:
            body = self.read_json_body()
            if not isinstance(body, dict) or "name" not in body:
                raise CommandError("缺少 name", "invalid_parameter")
            # 保存的是 applied state；故障/执行中/状态不可信时拒绝保存
            snap = self.hub.get_state_snapshot()
            if snap["status"] in ("fault", "applying") or not snap["state_valid"]:
                raise CommandError(
                    "当前状态不可保存配置（status=%s）" % snap["status"], "fault")
            overwrite = bool(body.get("overwrite")) or \
                self.get_query_argument("overwrite", "false").lower() in ("1", "true")
            config = self.store.make_from_state(body["name"], snap["devices"])
            path = self.store.save(config, overwrite=overwrite)
            self.write_json({"ok": True, "name": body["name"], "path": path})
        except CommandError as e:
            self.write_error_json(e)


class ConfigSelectHandler(BaseHandler):
    async def post(self):
        try:
            body = self.read_json_body()
            if not isinstance(body, dict) or "name" not in body:
                raise CommandError("缺少 name", "invalid_parameter")
            config = self.store.load(body["name"])
            result = await self._submit(Command(op="load_config", device_id="",
                                                payload=config))
            self.write_json(result)
        except CommandError as e:
            self.write_error_json(e)


class ParametersBatchHandler(BaseHandler):
    async def post(self, device_id: str):
        try:
            body = self.read_json_body()
            if not isinstance(body, dict):
                raise CommandError("请求体必须是对象", "invalid_parameter")
            parameters = body.get("parameters")
            if not isinstance(parameters, dict):
                raise CommandError("缺少 parameters 对象", "invalid_parameter")
            base_revision = body.get("baseRevision")
            if base_revision is None:
                raise CommandError("缺少 baseRevision", "invalid_parameter")
            if isinstance(base_revision, bool) or not isinstance(base_revision, int):
                raise CommandError("baseRevision 必须是整数", "invalid_parameter")
            force = bool(body.get("force"))
            # 预检查快速失败（提示性）；权威冲突检查在 worker 执行时（expected_revision）
            if not force:
                snap = self.hub.get_state_snapshot()
                if snap["revision"] != base_revision:
                    raise CommandError(
                        "版本冲突：当前 revision=%d，编辑基准=%d"
                        % (snap["revision"], base_revision), "conflict")
            result = await self._submit(Command(
                op="apply_parameters", device_id=device_id, payload=parameters,
                expected_revision=None if force else base_revision))
            self.write_json(result)
        except CommandError as e:
            self.write_error_json(e)


class OutputsClearHandler(BaseHandler):
    async def post(self, device_id: str):
        try:
            result = await self._submit(Command(
                op="clear_outputs", device_id=device_id))
            self.write_json(result)
        except CommandError as e:
            self.write_error_json(e)


class StateWSHandler(tornado.websocket.WebSocketHandler):
    def open(self):
        self._ioloop = tornado.ioloop.IOLoop.current()
        self._hub: ControlHub = self.settings["hub"]
        self._hub.subscribe(self._on_state)
        self._send(self._hub.get_state_snapshot())

    def on_close(self):
        self._ioloop = None
        if hasattr(self, "_hub"):
            self._hub.unsubscribe(self._on_state)

    def _on_state(self, snap: Dict[str, Any]) -> None:
        """在 worker 线程被调用；转投到 IOLoop 线程发送。"""
        ioloop = self._ioloop
        if ioloop is None:
            return
        ioloop.add_callback(lambda: self._send(snap))

    def _send(self, snap: Dict[str, Any]) -> None:
        if self.ws_connection is None:
            return
        try:
            self.write_message(json.dumps(snap, ensure_ascii=False))
        except Exception:
            pass


def build_app(hub: ControlHub, config_store: ConfigStore,
              static_dir: str) -> tornado.web.Application:
    return tornado.web.Application(
        [
            (r"/api/state", StateHandler),
            (r"/api/configs", ConfigsHandler),
            (r"/api/config/select", ConfigSelectHandler),
            (r"/api/devices/([^/]+)/parameters:batch", ParametersBatchHandler),
            (r"/api/devices/([^/]+)/outputs:clear", OutputsClearHandler),
            (r"/ws", StateWSHandler),
            (r"/static/(.*)", tornado.web.StaticFileHandler,
             {"path": static_dir}),
            (r"/", tornado.web.RedirectHandler, {"url": "/static/index.html"}),
        ],
        hub=hub,
        config_store=config_store,
        debug=False,
    )
