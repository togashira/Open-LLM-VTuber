# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import asyncio
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
import contextlib

class Live2DGuard:
    def __init__(self, app: FastAPI, mount_path: str, base_dir: str, model_name: str):
        self.app = app
        self.mount_path = mount_path
        self.base_dir = os.path.abspath(base_dir)
        self.model_name = model_name
        self._task: asyncio.Task | None = None

    def mount(self) -> None:
        # 冪等マウント（既にあるなら外してから付け直し）
        routes = [r for r in self.app.router.routes if getattr(r, "path", "") == self.mount_path]
        for r in routes:
            with contextlib.suppress(Exception):
                self.app.router.routes.remove(r)
        self.app.mount(
            self.mount_path,
            StaticFiles(directory=self.base_dir, follow_symlink=True, check_dir=True),
            name="live2d",
        )

    async def _probe_loop(self, port: int, interval_sec: float) -> None:
        url = f"http://127.0.0.1:{port}{self.mount_path}/{self.model_name}/model.json"
        try:
            import httpx
        except Exception:
            httpx = None  # httpx 未導入なら監視は無効（マウントだけ有効）
        while True:
            if httpx:
                try:
                    async with httpx.AsyncClient(timeout=3.0) as cli:
                        r = await cli.head(url)
                        if r.status_code != 200:
                            self.mount()  # 壊れてたら即リマウント
                except Exception:
                    self.mount()      # 失敗時も保守的にリマウント
            await asyncio.sleep(interval_sec)

    def start_watch(self, port: int, interval_sec: float = 60.0) -> None:
        self.mount()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._probe_loop(port, interval_sec))
