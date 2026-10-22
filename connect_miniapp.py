"""Bind a running HTTPS tunnel to the Telegram menu. No credentials in URLs/logs."""
import asyncio
from pathlib import Path
import re
import os
import time
from urllib.request import urlopen
from campus_bot.config import Config
from campus_bot.network import Telegram
from campus_bot.storage import Store

async def main():
    c=Config.load()
    log=c.data_dir/'tunnel.log'
    url=os.getenv('MINI_APP_URL')
    if url and not url.startswith('https://'):
        raise SystemExit('MINI_APP_URL must use HTTPS')
    for _ in range(45):
        if url:break
        text=log.read_text(encoding='utf-8',errors='replace') if log.exists() else ''
        urls=re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com',text)
        if urls:
            url=urls[-1];break
        await asyncio.sleep(1)
    if not url:
        raise SystemExit('HTTPS tunnel did not return a URL. Check runtime/tunnel.log')
    def reachable():
        try:
            with urlopen(url,timeout=3) as response:
                return response.status==200
        except Exception:
            return False
    for _ in range(20):
        if await asyncio.to_thread(reachable):break
        await asyncio.sleep(1)
    else:
        raise SystemExit('HTTPS endpoint is not ready; retry python connect_miniapp.py.')
    telegram=Telegram(c.token)
    await telegram.call('setChatMenuButton',chat_id=c.owner_id,menu_button={'type':'web_app','text':'Календарь','web_app':{'url':url}})
    s=Store(c.data_dir/'bot.sqlite3')
    s.set('mini_app_url',url);s.close()
    print('Mini App connected:',url)

if __name__=='__main__':asyncio.run(main())
