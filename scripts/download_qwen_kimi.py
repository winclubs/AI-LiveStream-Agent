import httpx
import re

targets = {
    'model_qwen.svg': 'https://unpkg.com/@lobehub/icons-static-svg@latest/icons/qwen-color.svg',
    'model_kimi.svg': 'https://unpkg.com/@lobehub/icons-static-svg@latest/icons/kimi-color.svg'
}

for fname, url in targets.items():
    res = httpx.get(url, follow_redirects=True, timeout=10)
    if res.status_code == 200:
        content = res.text
        vb = re.search(r'viewBox="([^"]+)"', content)
        view_box = vb.group(1) if vb else '0 0 24 24'
        content = re.sub(r'<svg[^>]*>', f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" width="100%" height="100%">', content)
        with open(f'server/static/svg/{fname}', 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'Saved {fname}: {len(content)} bytes')
    else:
        print(f'Failed {fname}: {res.status_code}')
