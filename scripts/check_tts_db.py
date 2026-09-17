import sqlite3

conn = sqlite3.connect('server/data.db')
c = conn.cursor()
c.execute("SELECT id, config_group, provider_name, model_name, is_active FROM api_provider_configs WHERE config_group='tts'")
rows = c.fetchall()
print("TTS configs in DB:")
for r in rows:
    print(r)
