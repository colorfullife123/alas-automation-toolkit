import asyncio
import ipaddress
import json

from module.logger import logger
from module.webui.process_manager import ProcessManager


class LocalSchedulerControl:
    PREFIX = "/__local_alas_scheduler__/v1/"

    def __init__(self, application, event, config_name="alas"):
        self.application = application
        self.event = event
        self.config_name = config_name

    @staticmethod
    def _is_loopback(scope):
        client = scope.get("client")
        if not client:
            return False
        try:
            return ipaddress.ip_address(client[0]).is_loopback
        except ValueError:
            return False

    @staticmethod
    async def _read_body(receive):
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            if (
                event["type"] == "http.request"
                and not event.get("more_body", False)
            ):
                return

    @staticmethod
    async def _send_json(send, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")

        if (
            scope.get("type") != "http"
            or not path.startswith(self.PREFIX)
        ):
            return await self.application(scope, receive, send)

        await self._read_body(receive)

        if not self._is_loopback(scope):
            return await self._send_json(
                send,
                403,
                {"ok": False, "error": "loopback access only"},
            )

        action = path[len(self.PREFIX):].strip("/")
        method = scope.get("method", "GET").upper()
        manager = ProcessManager.get_manager(self.config_name)

        try:
            if action == "status":
                if method != "GET":
                    return await self._send_json(
                        send,
                        405,
                        {"ok": False, "error": "GET required"},
                    )

                return await self._send_json(
                    send,
                    200,
                    {
                        "ok": True,
                        "config": self.config_name,
                        "running": manager.alive,
                        "state": manager.state,
                    },
                )

            if action == "start":
                if method != "POST":
                    return await self._send_json(
                        send,
                        405,
                        {"ok": False, "error": "POST required"},
                    )

                was_running = manager.alive

                if not was_running:
                    manager.start(None, self.event)
                    await asyncio.sleep(0.5)

                running = manager.alive

                return await self._send_json(
                    send,
                    200 if running else 503,
                    {
                        "ok": running,
                        "config": self.config_name,
                        "running": running,
                        "state": manager.state,
                        "action": (
                            "already_running"
                            if was_running
                            else "started"
                        ),
                    },
                )

            if action == "stop":
                if method != "POST":
                    return await self._send_json(
                        send,
                        405,
                        {"ok": False, "error": "POST required"},
                    )

                was_running = manager.alive

                if was_running:
                    manager.stop()

                    for _ in range(10):
                        if not manager.alive:
                            break
                        await asyncio.sleep(0.2)

                running = manager.alive
                stopped = not running

                return await self._send_json(
                    send,
                    200 if stopped else 503,
                    {
                        "ok": stopped,
                        "config": self.config_name,
                        "running": running,
                        "state": manager.state,
                        "action": (
                            "stopped"
                            if was_running
                            else "already_stopped"
                        ),
                    },
                )

            return await self._send_json(
                send,
                404,
                {"ok": False, "error": "unknown action"},
            )

        except Exception as exc:
            logger.exception(exc)
            return await self._send_json(
                send,
                500,
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
