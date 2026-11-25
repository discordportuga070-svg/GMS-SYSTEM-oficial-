import asyncio
import base64
import os
import sys

import aiohttp
import discord
from discord import app_commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
if not BOT_TOKEN:
    sys.exit("Variável de ambiente DISCORD_TOKEN não definida.")

API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
FALLBACK_TYPE = {5: 0, 15: 0, 13: 2}

sessions = {}
running = set()


class Api:
    def __init__(self, token):
        self.s = aiohttp.ClientSession(headers={"Authorization": token, "User-Agent": UA})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.s.close()

    async def call(self, method, path, **kw):
        while True:
            async with self.s.request(method, API + path, **kw) as r:
                if r.status == 429:
                    data = await r.json(content_type=None)
                    await asyncio.sleep(float(data.get("retry_after", 1)) + 0.3)
                    continue
                await asyncio.sleep(0.35)
                if r.status == 204:
                    return None
                data = await r.json(content_type=None)
                if r.status >= 400:
                    msg = data.get("message") if isinstance(data, dict) else data
                    raise RuntimeError(f"{r.status} {msg}")
                return data

    async def data_uri(self, url):
        async with self.s.get(url) as r:
            if r.status != 200:
                return None
            raw = await r.read()
            mime = r.headers.get("Content-Type", "image/png")
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def map_overwrites(ch, role_map, src_id, dst_id):
    out = []
    for o in ch.get("permission_overwrites", []):
        if o["type"] != 0:
            continue
        new_id = dst_id if o["id"] == src_id else role_map.get(o["id"])
        if new_id:
            out.append({"id": new_id, "type": 0, "allow": o["allow"], "deny": o["deny"]})
    return out


def channel_payload(ch, ctype, parent_id, overwrites):
    p = {
        "name": ch["name"],
        "type": ctype,
        "permission_overwrites": overwrites,
        "position": ch.get("position", 0),
    }
    if parent_id:
        p["parent_id"] = parent_id
    if ctype in (0, 5, 15):
        p["topic"] = ch.get("topic")
        p["nsfw"] = ch.get("nsfw", False)
        p["rate_limit_per_user"] = ch.get("rate_limit_per_user", 0)
    elif ctype in (2, 13):
        p["bitrate"] = min(ch.get("bitrate") or 64000, 96000)
        if ctype == 2:
            p["user_limit"] = ch.get("user_limit", 0)
    return p


async def wipe(api, dst_id, errors):
    for ch in await api.call("GET", f"/guilds/{dst_id}/channels"):
        try:
            await api.call("DELETE", f"/channels/{ch['id']}")
        except RuntimeError as e:
            errors.append(f"apagar canal {ch['name']}: {e}")
    for r in await api.call("GET", f"/guilds/{dst_id}/roles"):
        if r["id"] == dst_id or r.get("managed"):
            continue
        try:
            await api.call("DELETE", f"/guilds/{dst_id}/roles/{r['id']}")
        except RuntimeError as e:
            errors.append(f"apagar cargo {r['name']}: {e}")


async def clone_roles(api, src_id, dst_id, errors):
    roles = await api.call("GET", f"/guilds/{src_id}/roles")
    role_map = {}
    for r in roles:
        if r["id"] == src_id:
            try:
                await api.call("PATCH", f"/guilds/{dst_id}/roles/{dst_id}",
                               json={"permissions": r["permissions"]})
            except RuntimeError as e:
                errors.append(f"permissões do @everyone: {e}")
    ordered = [r for r in sorted(roles, key=lambda r: r["position"])
               if r["id"] != src_id and not r.get("managed")]
    for r in ordered:
        try:
            new = await api.call("POST", f"/guilds/{dst_id}/roles", json={
                "name": r["name"],
                "permissions": r["permissions"],
                "color": r["color"],
                "hoist": r["hoist"],
                "mentionable": r["mentionable"],
            })
            role_map[r["id"]] = new["id"]
        except RuntimeError as e:
            errors.append(f"criar cargo {r['name']}: {e}")
    if role_map:
        positions = [{"id": role_map[r["id"]], "position": i + 1}
                     for i, r in enumerate(x for x in ordered if x["id"] in role_map)]
        try:
            await api.call("PATCH", f"/guilds/{dst_id}/roles", json=positions)
        except RuntimeError as e:
            errors.append(f"ordem dos cargos: {e}")
    return role_map


async def clone_channels(api, src_id, dst_id, role_map, errors):
    chans = sorted(await api.call("GET", f"/guilds/{src_id}/channels"),
                   key=lambda c: c.get("position", 0))
    ch_map = {}
    for ch in chans:
        if ch["type"] != 4:
            continue
        try:
            new = await api.call("POST", f"/guilds/{dst_id}/channels", json={
                "name": ch["name"],
                "type": 4,
                "position": ch.get("position", 0),
                "permission_overwrites": map_overwrites(ch, role_map, src_id, dst_id),
            })
            ch_map[ch["id"]] = new["id"]
        except RuntimeError as e:
            errors.append(f"criar categoria {ch['name']}: {e}")
    for ch in chans:
        if ch["type"] == 4:
            continue
        parent = ch_map.get(ch.get("parent_id"))
        ow = map_overwrites(ch, role_map, src_id, dst_id)
        ctype = ch["type"]
        try:
            new = await api.call("POST", f"/guilds/{dst_id}/channels",
                                 json=channel_payload(ch, ctype, parent, ow))
        except RuntimeError as e:
            if ctype not in FALLBACK_TYPE:
                errors.append(f"criar canal {ch['name']}: {e}")
                continue
            try:
                new = await api.call("POST", f"/guilds/{dst_id}/channels",
                                     json=channel_payload(ch, FALLBACK_TYPE[ctype], parent, ow))
                errors.append(f"canal {ch['name']} (tipo {ctype}) criado como tipo {FALLBACK_TYPE[ctype]}")
            except RuntimeError as e2:
                errors.append(f"criar canal {ch['name']}: {e2}")
                continue
        ch_map[ch["id"]] = new["id"]
    return ch_map


async def clone_settings(api, src_id, dst_id, ch_map, errors):
    g = await api.call("GET", f"/guilds/{src_id}")
    payload = {
        "name": g["name"],
        "verification_level": g["verification_level"],
        "default_message_notifications": g["default_message_notifications"],
        "explicit_content_filter": g["explicit_content_filter"],
        "afk_timeout": g["afk_timeout"],
        "afk_channel_id": ch_map.get(g.get("afk_channel_id")),
        "system_channel_id": ch_map.get(g.get("system_channel_id")),
    }
    if g.get("icon"):
        icon = await api.data_uri(f"{CDN}/icons/{src_id}/{g['icon']}.png?size=1024")
        if icon:
            payload["icon"] = icon
    try:
        await api.call("PATCH", f"/guilds/{dst_id}", json=payload)
    except RuntimeError as e:
        errors.append(f"configurações do servidor: {e}")


async def clone_emojis(api, src_id, dst_id, errors):
    failures = 0
    for e in await api.call("GET", f"/guilds/{src_id}/emojis"):
        ext = "gif" if e.get("animated") else "png"
        img = await api.data_uri(f"{CDN}/emojis/{e['id']}.{ext}")
        if not img:
            continue
        try:
            await api.call("POST", f"/guilds/{dst_id}/emojis", json={"name": e["name"], "image": img})
        except RuntimeError as ex:
            errors.append(f"emoji {e['name']}: {ex}")
            failures += 1
            if failures >= 3:
                errors.append("emojis: parei após 3 falhas (provável limite de slots)")
                break


async def run_clone(token, src_id, dst_id, progress):
    errors = []
    async with Api(token) as api:
        await progress("Limpando o destino...")
        await wipe(api, dst_id, errors)
        await progress("Clonando cargos...")
        role_map = await clone_roles(api, src_id, dst_id, errors)
        await progress("Clonando canais...")
        ch_map = await clone_channels(api, src_id, dst_id, role_map, errors)
        await progress("Aplicando configurações...")
        await clone_settings(api, src_id, dst_id, ch_map, errors)
        await progress("Clonando emojis...")
        await clone_emojis(api, src_id, dst_id, errors)
    return len(role_map), len(ch_map), errors


def panel_embed(s):
    e = discord.Embed(title="Painel do Cloner", color=0x6D28D9)
    e.add_field(name="Token", value="✅ configurado" if s.get("token") else "❌ não definido", inline=False)
    for key, label in (("src", "Origem"), ("dst", "Destino")):
        val = f"{s[key + '_name']} (`{s[key]}`)" if s.get(key) else "❌ não definido"
        e.add_field(name=label, value=val, inline=False)
    e.set_footer(text="O destino é apagado antes de clonar.")
    return e


class TokenModal(discord.ui.Modal, title="Configurar token"):
    token = discord.ui.TextInput(label="Token da conta", max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tk = self.token.value.strip().strip('"')
        try:
            async with Api(tk) as api:
                await api.call("GET", "/users/@me")
        except Exception:
            return await interaction.followup.send("Token inválido.", ephemeral=True)
        s = sessions.setdefault(interaction.user.id, {})
        s["token"] = tk
        await interaction.followup.send(embed=panel_embed(s), view=PanelView(), ephemeral=True)


class IdModal(discord.ui.Modal):
    sid = discord.ui.TextInput(label="ID do servidor", max_length=25)

    def __init__(self, key):
        super().__init__(title="Servidor de origem" if key == "src" else "Servidor de destino")
        self.key = key

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        s = sessions.get(interaction.user.id)
        if not s:
            return await interaction.followup.send("Sessão expirada. Clique em Config de novo.", ephemeral=True)
        sid = self.sid.value.strip()
        if not sid.isdigit():
            return await interaction.followup.send("ID inválido.", ephemeral=True)
        try:
            async with Api(s["token"]) as api:
                g = await api.call("GET", f"/guilds/{sid}")
        except RuntimeError as e:
            return await interaction.followup.send(f"Não consegui acessar esse servidor: {e}", ephemeral=True)
        s[self.key] = sid
        s[self.key + "_name"] = g["name"]
        await interaction.edit_original_response(embed=panel_embed(s), view=PanelView())


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=1800)

    @discord.ui.button(label="Origem", style=discord.ButtonStyle.secondary)
    async def origem(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in sessions:
            return await interaction.response.send_message("Sessão expirada. Clique em Config.", ephemeral=True)
        await interaction.response.send_modal(IdModal("src"))

    @discord.ui.button(label="Destino", style=discord.ButtonStyle.secondary)
    async def destino(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in sessions:
            return await interaction.response.send_message("Sessão expirada. Clique em Config.", ephemeral=True)
        await interaction.response.send_modal(IdModal("dst"))

    @discord.ui.button(label="Clonar", style=discord.ButtonStyle.danger)
    async def clonar(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        s = sessions.get(uid)
        if not s or not s.get("token"):
            return await interaction.response.send_message("Sessão expirada. Clique em Config.", ephemeral=True)
        if not s.get("src") or not s.get("dst"):
            return await interaction.response.send_message("Defina a origem e o destino primeiro.", ephemeral=True)
        if s["src"] == s["dst"]:
            return await interaction.response.send_message("Origem e destino são iguais.", ephemeral=True)
        if uid in running:
            return await interaction.response.send_message("Você já tem um clone em andamento.", ephemeral=True)

        running.add(uid)
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.followup.send("Iniciando...", ephemeral=True, wait=True)

        async def progress(text):
            try:
                await msg.edit(content=text)
            except discord.HTTPException:
                pass

        try:
            roles, chans, errors = await run_clone(s["token"], s["src"], s["dst"], progress)
            text = f"Clone concluído: {roles} cargos e {chans} canais criados."
            if errors:
                text += f"\n{len(errors)} avisos:\n" + "\n".join(errors[:10])
        except Exception as e:
            text = f"Erro: {e}"
        finally:
            running.discard(uid)
            sessions.pop(uid, None)
        await progress(text[:1900])


class ConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Config", style=discord.ButtonStyle.secondary, custom_id="cloner:config")
    async def config(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = sessions.get(interaction.user.id)
        if s and s.get("token"):
            return await interaction.response.send_message(embed=panel_embed(s), view=PanelView(), ephemeral=True)
        await interaction.response.send_modal(TokenModal())


class ClonerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(ConfigView())
        await self.tree.sync()

    async def on_ready(self):
        print(f"Online como {self.user} ({self.user.id})", flush=True)


bot = ClonerBot()


@bot.tree.command(name="painel", description="Publica o painel do cloner neste canal")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def painel(interaction: discord.Interaction):
    e = discord.Embed(
        title="Cloner de servidor",
        description="Clona canais, cargos e visual.\nClique em **Config** para abrir seu painel privado.",
        color=0x6D28D9,
    )
    await interaction.channel.send(embed=e, view=ConfigView())
    await interaction.response.send_message("Painel publicado.", ephemeral=True)


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
