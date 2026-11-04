"""后端 WebSocket 服务的测试：不需要显卡、不调用翻译 API。

  python tests/test_server.py

用一个假的流水线启动服务，模拟 Electron 界面连接，检查：令牌校验、hello、事件转发、各个命令。
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config  # noqa: E402
import app.server as server_mod  # noqa: E402
from app.config import load_config  # noqa: E402
from app.server import Server, settings_patch  # noqa: E402


class FakeLogbook:
    def __init__(self, d: Path) -> None:
        self.directory = d
        self.path = d / "2026-01-01_0000.md"
        self.exists = False


class FakePipeline:
    def __init__(self, d: Path) -> None:
        self.paused = False
        self.device_hint = ""
        self.logbook = FakeLogbook(d)
        self.stopped = False

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        return self.paused

    def set_device(self, name: str) -> None:
        self.device_hint = name

    @staticmethod
    def list_devices() -> list[str]:
        return ["耳机 (Test)", "扬声器 (Test)"]

    def stop(self) -> None:
        self.stopped = True


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp())
    # 所有设置写到临时目录，不碰真实的 user_settings.json / .env
    config.USER_SETTINGS = tmp / "user_settings.json"
    config.ENV_FILE = tmp / ".env"
    server_mod.save_user_settings = lambda patch: config.save_user_settings(patch, tmp / "user_settings.json")
    server_mod.save_env_var = lambda k, v: config.save_env_var(k, v, tmp / ".env")
    server_mod.Pipeline = FakePipeline  # list_devices 是静态方法

    cfg = load_config(user_settings=tmp / "none.json")
    events: queue.Queue = queue.Queue()
    pipe = FakePipeline(tmp)
    srv = Server(cfg, pipe, events)

    ready: dict = {}
    import builtins
    real_print = builtins.print

    def capture_print(*a, **kw):
        s = " ".join(str(x) for x in a)
        if s.startswith("@@READY "):
            ready.update(json.loads(s[8:]))
        real_print(*a, **kw)

    builtins.print = capture_print
    t = threading.Thread(target=srv.run, kwargs={"port": 0, "watch_parent": False}, daemon=True)
    t.start()
    for _ in range(100):
        if ready:
            break
        threading.Event().wait(0.05)
    builtins.print = real_print
    assert ready.get("port"), "服务没有打印 @@READY"

    async def client() -> None:
        from websockets.asyncio.client import connect

        base = f"ws://127.0.0.1:{ready['port']}/"
        # 1. 错误令牌被拒绝
        async with connect(base + "?token=wrong") as ws:
            try:
                await asyncio.wait_for(ws.recv(), 2)
                raise AssertionError("错误令牌不应该收到数据")
            except Exception as e:
                assert "4401" in str(e) or "bad token" in str(e), e
        print("PASS 错误令牌被拒绝")

        async with connect(base + f"?token={ready['token']}") as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello" and hello["language"]["code"] == "th"
            assert "toggle_lock" in hello["hotkeys"]
            print("PASS hello 初始状态")

            mid = 0

            async def call(cmd: str, **kw):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"cmd": cmd, "id": mid, **kw}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if m.get("type") == "reply" and m.get("id") == mid:
                        return m

            # 2. 流水线事件被转发
            events.put({"type": "translation", "id": 7, "text": "你好", "partial": False})
            m = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert m == {"type": "translation", "id": 7, "text": "你好", "partial": False}, m
            print("PASS 事件转发")

            r = await call("toggle_pause")
            assert r["ok"] and r["data"]["paused"] is True and pipe.paused
            r = await call("list_devices")
            assert r["data"]["devices"] == ["耳机 (Test)", "扬声器 (Test)"]
            r = await call("set_device", name="耳机 (Test)")
            assert r["ok"] and pipe.device_hint == "耳机 (Test)"
            assert json.loads((tmp / "user_settings.json").read_text(encoding="utf-8"))["audio"]["device"] == "耳机 (Test)"
            print("PASS 暂停 / 设备列表 / 切换设备并记住")

            r = await call("get_settings")
            assert r["data"]["values"]["language"] == "th" and r["data"]["options"]["claude_models"]
            r = await call("save_settings", values={"backend": "claude"}, api_key="not-a-key")
            assert not r["ok"] and "sk-ant-" in r["error"]
            r = await call("save_settings", values={"language": "en", "backend": "free", "partials": False,
                                                   "asr_model": "evil-model"})
            assert r["ok"], r
            saved = json.loads((tmp / "user_settings.json").read_text(encoding="utf-8"))
            assert saved["asr"]["language"] == "en" and saved["translate"]["backend"] == "free"
            assert saved["asr"]["model"] == "large-v3", "非法的模型名应该被忽略"
            assert saved["vad"]["partial_interval_s"] == 0.0
            print("PASS 读取 / 校验 / 保存设置")

            r = await call("transcript")
            assert r["ok"] and r["data"]["path"] is None
            r = await call("generate_notes")
            assert not r["ok"] and "没有" in r["error"]
            r = await call("nope")
            assert not r["ok"]
            print("PASS 记录 / 笔记 / 未知命令")

    asyncio.run(client())
    assert settings_patch({"backend": "x"})["translate"]["backend"] == "claude"
    srv.shutdown()
    t.join(3)
    assert pipe.stopped and not t.is_alive()
    print("PASS 关闭服务")
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
