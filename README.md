# Ghovy Cloner

Bot de Discord com painel privado para clonar cargos, canais e configurações de um servidor para outro.

## Variável de ambiente

| Nome | Valor |
| --- | --- |
| `DISCORD_TOKEN` | Token do bot (Developer Portal → Bot → Reset Token) |

Nunca coloque o token no código nem no GitHub. No host, defina-o como variável de ambiente. Localmente, copie `.env.example` para `.env`.

## Instalar e rodar

```bash
pip install -r requirements.txt
python bot.py
```

## Rodar no host

- Comando de instalação: `pip install -r requirements.txt`
- Comando de início: `python bot.py`
- Python: 3.10 ou superior
- Alternativa: se o host aceitar Docker, ele usa o `Dockerfile` deste repositório.

## Uso

1. Convide o bot ao seu servidor com os escopos `bot` e `applications.commands`, mais permissão de enviar mensagens e embeds.
2. Use `/painel` no canal onde o painel deve ficar.
3. Clique em **Config**, informe o token da conta e use **Origem**, **Destino** e **Clonar**.

O destino é apagado antes de clonar. Mensagens, bots e membros não são copiados. Token e IDs ficam só na memória e são descartados ao fim do clone.
