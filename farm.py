import asyncio
import json
import random
import time

import aiohttp

GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"
ROTATE_SECONDS = 1800
FATAL_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

_games_cache = None


async def load_games():
    global _games_cache
    if _games_cache:
        return _games_cache
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.get(DETECTABLE_URL) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
    seen, games = set(), []
    for g in data:
        name = (g.get("name") or "").strip()
        if not name or not g.get("id") or name.lower() in seen:
            continue
        seen.add(name.lower())
        games.append({"id": g["id"], "name": name})
    if not games:
        raise RuntimeError("lista de jogos vazia")
    _games_cache = games
    return games


class Farm:
    def __init__(self, token, all_games, target_games, target_hours):
        self.token = token
        self.target_games = target_games
        self.target_hours = target_hours
        self.games = random.sample(all_games, min(target_games, len(all_games)))
        self.seen = set()
        self.current = None
        self.status = "iniciando"
        self.error = None
        self.finished = False
        self.task = None
        self._seq = None
        self._idx = 0
        self._acc = 0.0
        self._since = None
        self._ready = False

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    @property
    def hours_online(self):
        total = self._acc
        if self._since is not None:
            total += time.monotonic() - self._since
        return total / 3600

    def is_done(self):
        return len(self.seen) >= self.target_games and self.hours_online >= self.target_hours

    def start(self):
        self.task = asyncio.create_task(self._run())

    def stop(self):
        if self.active:
            self.task.cancel()
        if not self.finished:
            self.status = "parado"

    def _identify(self):
        return {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {"os": "Windows", "browser": "Chrome", "device": ""},
                "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
                "compress": False,
            },
        }

    async def _heartbeat(self, ws, interval):
        await asyncio.sleep(interval * random.random())
        while True:
            await ws.send_json({"op": 1, "d": self._seq})
            await asyncio.sleep(interval)

    async def _rotate(self, ws):
        while True:
            game = self.games[self._idx % len(self.games)]
            self._idx += 1
            self.current = game["name"]
            self.seen.add(game["id"])
            now = int(time.time() * 1000)
            await ws.send_json({
                "op": 3,
                "d": {
                    "since": None,
                    "status": "online",
                    "afk": False,
                    "activities": [{
                        "name": game["name"],
                        "type": 0,
                        "application_id": game["id"],
                        "timestamps": {"start": now},
                        "created_at": now,
                    }],
                },
            })
            await asyncio.sleep(ROTATE_SECONDS)
            if self.is_done():
                self.finished = True
                await ws.close()
                return

    async def _session(self, http):
        self._seq = None
        self._ready = False
        async with http.ws_connect(GATEWAY, max_msg_size=0, heartbeat=None) as ws:
            hello = await ws.receive_json()
            interval = hello["d"]["heartbeat_interval"] / 1000
            await ws.send_json(self._identify())
            hb = asyncio.create_task(self._heartbeat(ws, interval))
            rot = None
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    p = json.loads(msg.data)
                    if p.get("s") is not None:
                        self._seq = p["s"]
                    op = p.get("op")
                    if op == 0 and p.get("t") == "READY" and rot is None:
                        self._ready = True
                        self._since = time.monotonic()
                        self.status = "farmando"
                        self.error = None
                        rot = asyncio.create_task(self._rotate(ws))
                    elif op == 1:
                        await ws.send_json({"op": 1, "d": self._seq})
                    elif op in (7, 9):
                        break
            finally:
                hb.cancel()
                if rot:
                    rot.cancel()
                if self._since is not None:
                    self._acc += time.monotonic() - self._since
                    self._since = None
        return ws.close_code

    async def _run(self):
        backoff = 5
        async with aiohttp.ClientSession() as http:
            while not self.finished:
                self.status = "conectando"
                code = None
                try:
                    code = await self._session(http)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.error = str(e)[:200] or e.__class__.__name__
                if self.finished:
                    break
                if code in FATAL_CODES:
                    self.status = "erro"
                    self.error = "Token recusado pelo Discord." if code == 4004 else f"Gateway recusou a conexão (código {code})."
                    return
                self.status = "reconectando"
                if self._ready:
                    backoff = 5
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
        self.status = "concluído"
        self.current = None
