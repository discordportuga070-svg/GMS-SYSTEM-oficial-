import asyncio
import base64
import io
import os
import sys
from typing import Optional

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

DATA_DIR = os.environ.get("DATA_DIR", "data")
BANNER_URL = os.environ.get("BANNER_URL")
BANNER_PATH = os.path.join(DATA_DIR, "banner.png")
os.makedirs(DATA_DIR, exist_ok=True)

API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
FALLBACK_TYPE = {5: 0, 15: 0, 13: 2}
PURPLE = 0x6D28D9

HOUSES = {
    1: ("Bravery", 0x9C84EF, 1 << 6),
    2: ("Brilliance", 0xF47B67, 1 << 7),
    3: ("Balance", 0x45DDC0, 1 << 8),
}

sessions = {}
running = set()


# ---------- API da conta ----------

class RateLimited(RuntimeError):
    pass


class Api:
    def __init__(self, token):
        self.s = aiohttp.ClientSession(
            headers={"Authorization": token, "User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.s.close()

    async def call(self, method, path, **kw):
        max_wait = kw.pop("max_wait", 60)
        while True:
            async with self.s.request(method, API + path, **kw) as r:
                if r.status == 429:
                    data = await r.json(content_type=None)
                    wait = float(data.get("retry_after", 1)) + 0.3
                    if wait > max_wait:
                        raise RateLimited(f"limite de taxa do Discord ({int(wait)}s de espera)")
                    await asyncio.sleep(wait)
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


# ---------- Clone ----------

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


async def clone_emojis(api, src_id, dst_id, errors, progress):
    emojis = await api.call("GET", f"/guilds/{src_id}/emojis")
    total = len(emojis)
    done = failures = 0
    for i, e in enumerate(emojis, 1):
        if i % 5 == 1:
            await progress(f"Clonando emojis... {i}/{total}")
        ext = "gif" if e.get("animated") else "png"
        try:
            img = await api.data_uri(f"{CDN}/emojis/{e['id']}.{ext}")
        except Exception:
            img = None
        if not img:
            continue
        try:
            await api.call("POST", f"/guilds/{dst_id}/emojis",
                           json={"name": e["name"], "image": img}, max_wait=10)
            done += 1
        except RateLimited as ex:
            errors.append(f"emojis: parei em {done}/{total}, {ex}")
            return
        except RuntimeError as ex:
            errors.append(f"emoji {e['name']}: {ex}")
            failures += 1
            if failures >= 3:
                errors.append(f"emojis: parei em {done}/{total} após 3 falhas (provável limite de slots)")
                return


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
        await clone_emojis(api, src_id, dst_id, errors, progress)
    return len(role_map), len(ch_map), errors


# ---------- Visual ----------

def read_banner():
    try:
        with open(BANNER_PATH, "rb") as f:
            return f.read()
    except OSError:
        return None


def with_banner(embed):
    data = read_banner()
    if data:
        embed.set_image(url="attachment://banner.png")
        return [discord.File(io.BytesIO(data), filename="banner.png")]
    if BANNER_URL:
        embed.set_image(url=BANNER_URL)
    return []


def styled(embed, author):
    icon = bot.user.display_avatar.url if bot.user else None
    embed.set_author(name=author, icon_url=icon)
    return embed


def public_embed(kind):
    if kind == "hype":
        text = (
            "**[+] HypeSquad:**\n"
            "> Escolha **Bravery / Brilliance / Balance**.\n"
            "> ⚙️ **Config** → painel privado (**só quem clicou**).\n"
            "• Troca a casa direto na conta\n"
            "• Requer login"
        )
        return styled(discord.Embed(description=text, color=PURPLE), "HypeSquad")
    text = (
        "**[+] Cloner de servidor:**\n"
        "> Clona canais, cargos e visual.\n"
        "> ⚙️ **Config** → painel privado (**só quem clicou**).\n"
        "• Token com admin no destino\n"
        "• Não clona mensagens"
    )
    return styled(discord.Embed(description=text, color=PURPLE), "Cloner")


def clone_panel_embed(s):
    lines = ["**[+] Painel do Cloner:**"]
    lines.append("> Token: " + ("✅ configurado" if s.get("token") else "❌ não definido"))
    for key, label in (("src", "Origem"), ("dst", "Destino")):
        val = f"{s[key + '_name']} (`{s[key]}`)" if s.get(key) else "❌ não definido"
        lines.append(f"> {label}: {val}")
    lines.append("• O destino é apagado antes de clonar")
    return styled(discord.Embed(description="\n".join(lines), color=PURPLE), "Cloner")


def current_house(me):
    flags = me.get("public_flags", 0)
    for hid, (_, _, bit) in HOUSES.items():
        if flags & bit:
            return hid
    return None


def hype_panel_embed(me):
    hid = current_house(me)
    name = HOUSES[hid][0] if hid else "nenhuma"
    color = HOUSES[hid][1] if hid else PURPLE
    lines = [
        "**[+] Painel HypeSquad:**",
        f"> Conta: **{me.get('username', '?')}**",
        f"> Casa atual: **{name}**",
        "• Escolha uma casa abaixo ou remova a atual",
    ]
    return styled(discord.Embed(description="\n".join(lines), color=color), "HypeSquad")


async def build_panel(kind, s):
    if kind == "hype":
        async with Api(s["token"]) as api:
            me = await api.call("GET", "/users/@me")
        embed, view = hype_panel_embed(me), HypeView()
    else:
        embed, view = clone_panel_embed(s), PanelView()
    return embed, view, with_banner(embed)


# ---------- Modais ----------

class TokenModal(discord.ui.Modal, title="Configurar token"):
    token = discord.ui.TextInput(label="Token da conta", max_length=200)

    def __init__(self, kind):
        super().__init__()
        self.kind = kind

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
        try:
            embed, view, files = await build_panel(self.kind, s)
        except RuntimeError as e:
            return await interaction.followup.send(f"Erro: {e}", ephemeral=True)
        await interaction.followup.send(embed=embed, view=view, files=files, ephemeral=True)


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
        embed = clone_panel_embed(s)
        files = with_banner(embed)
        await interaction.edit_original_response(embed=embed, view=PanelView(), attachments=files)


# ---------- Painéis privados ----------

class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=1800)

    @discord.ui.button(label="Origem", emoji="📤", style=discord.ButtonStyle.secondary)
    async def origem(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in sessions:
            return await interaction.response.send_message("Sessão expirada. Clique em Config.", ephemeral=True)
        await interaction.response.send_modal(IdModal("src"))

    @discord.ui.button(label="Destino", emoji="📥", style=discord.ButtonStyle.secondary)
    async def destino(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in sessions:
            return await interaction.response.send_message("Sessão expirada. Clique em Config.", ephemeral=True)
        await interaction.response.send_modal(IdModal("dst"))

    @discord.ui.button(label="Clonar", emoji="🚀", style=discord.ButtonStyle.danger)
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
            roles, chans, errors = await asyncio.wait_for(
                run_clone(s["token"], s["src"], s["dst"], progress), timeout=1800
            )
            text = f"Clone concluído: {roles} cargos e {chans} canais criados."
            if errors:
                text += f"\n{len(errors)} avisos:\n" + "\n".join(errors[:10])
        except asyncio.TimeoutError:
            text = "Clone interrompido após 30 minutos. Confira o destino e tente de novo."
        except Exception as e:
            text = f"Erro: {e}"
        finally:
            running.discard(uid)
            sessions.pop(uid, None)
        await progress(text[:1900])


class HypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=1800)

    async def apply(self, interaction: discord.Interaction, house):
        s = sessions.get(interaction.user.id)
        if not s or not s.get("token"):
            return await interaction.response.send_message("Sessão expirada. Clique em Config.", ephemeral=True)
        await interaction.response.defer()
        try:
            async with Api(s["token"]) as api:
                if house:
                    await api.call("POST", "/hypesquad/online", json={"house_id": house})
                else:
                    await api.call("DELETE", "/hypesquad/online")
                me = await api.call("GET", "/users/@me")
        except RuntimeError as e:
            return await interaction.followup.send(f"Erro: {e}", ephemeral=True)
        embed = hype_panel_embed(me)
        files = with_banner(embed)
        await interaction.edit_original_response(embed=embed, view=HypeView(), attachments=files)

    @discord.ui.button(label="Bravery", style=discord.ButtonStyle.primary, row=0)
    async def bravery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply(interaction, 1)

    @discord.ui.button(label="Brilliance", style=discord.ButtonStyle.danger, row=0)
    async def brilliance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply(interaction, 2)

    @discord.ui.button(label="Balance", style=discord.ButtonStyle.success, row=0)
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply(interaction, 3)

    @discord.ui.button(label="Remover casa", emoji="🗑️", style=discord.ButtonStyle.secondary, row=1)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.apply(interaction, None)

    @discord.ui.button(label="Sair", emoji="🔒", style=discord.ButtonStyle.secondary, row=1)
    async def sair(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(interaction.user.id, None)
        await interaction.response.edit_message(content="Token removido da memória.", embed=None, view=None, attachments=[])


# ---------- Painel público ----------

class ConfigView(discord.ui.View):
    def __init__(self, kind):
        super().__init__(timeout=None)
        self.kind = kind
        btn = discord.ui.Button(label="Config", emoji="⚙️", style=discord.ButtonStyle.secondary,
                                custom_id=f"{kind}:config")
        btn.callback = self.config
        self.add_item(btn)

    async def config(self, interaction: discord.Interaction):
        s = sessions.get(interaction.user.id)
        if not s or not s.get("token"):
            return await interaction.response.send_modal(TokenModal(self.kind))
        await interaction.response.defer(ephemeral=True)
        try:
            embed, view, files = await build_panel(self.kind, s)
        except RuntimeError:
            sessions.pop(interaction.user.id, None)
            return await interaction.followup.send("Token expirou. Clique em Config de novo.", ephemeral=True)
        await interaction.followup.send(embed=embed, view=view, files=files, ephemeral=True)


class ClonerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(ConfigView("cloner"))
        self.add_view(ConfigView("hype"))
        await self.tree.sync()

    async def on_ready(self):
        print(f"Online como {self.user} ({self.user.id})", flush=True)


bot = ClonerBot()


@bot.tree.command(name="painel", description="Publica um painel neste canal")
@app_commands.describe(tipo="Qual painel publicar")
@app_commands.choices(tipo=[
    app_commands.Choice(name="Cloner de servidor", value="cloner"),
    app_commands.Choice(name="HypeSquad", value="hype"),
])
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def painel(interaction: discord.Interaction, tipo: app_commands.Choice[str]):
    embed = public_embed(tipo.value)
    files = with_banner(embed)
    await interaction.response.send_message(embed=embed, view=ConfigView(tipo.value), files=files)


@bot.tree.command(name="banner", description="Define ou remove o banner dos painéis")
@app_commands.describe(imagem="Imagem do banner (deixe vazio para remover)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def banner(interaction: discord.Interaction, imagem: Optional[discord.Attachment] = None):
    if imagem is None:
        try:
            os.remove(BANNER_PATH)
        except OSError:
            pass
        return await interaction.response.send_message("Banner removido.", ephemeral=True)
    if not (imagem.content_type or "").startswith("image/"):
        return await interaction.response.send_message("Envie um arquivo de imagem.", ephemeral=True)
    if imagem.size > 8 * 1024 * 1024:
        return await interaction.response.send_message("Imagem grande demais (máximo 8 MB).", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    with open(BANNER_PATH, "wb") as f:
        f.write(await imagem.read())
    await interaction.followup.send("Banner salvo. Use /painel de novo para publicar com ele.", ephemeral=True)


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
