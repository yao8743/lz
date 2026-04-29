import json
import os

from dotenv import load_dotenv

load_dotenv(dotenv_path='.man.env')

config: dict = {}

try:
	configuration_json = json.loads(os.getenv('CONFIGURATION', '') or '{}')
	if isinstance(configuration_json, dict):
		config.update(configuration_json)
except Exception as error:
	print(f"⚠️ 無法解析 CONFIGURATION：{error}")


API_ID = int(config.get('api_id', os.getenv('API_ID', 0)))
API_HASH = config.get('api_hash', os.getenv('API_HASH', ''))
SESSION_STRING = config.get('session_string', os.getenv('SESSION_STRING', ''))


