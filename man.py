import asyncio
import json
import os
import random
import tempfile
from contextlib import suppress
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError

from man_config import API_HASH, API_ID, SESSION_STRING


def _build_client() -> TelegramClient:
	"""兼容 StringSession 与本地 .session 文件名两种输入。"""
	raw = str(SESSION_STRING or "").strip()
	api_id = int(API_ID)

	# StringSession 通常是较长 token，不应被当作 sqlite 文件路径
	if raw and len(raw) > 80 and not raw.endswith(".session") and "/" not in raw and "\\" not in raw:
		return TelegramClient(StringSession(raw), api_id, API_HASH)

	return TelegramClient(raw or "man", api_id, API_HASH)


async def _handle_healthcheck(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
	try:
		await reader.read(1024)
		body = b"ok"
		response = (
			b"HTTP/1.1 200 OK\r\n"
			b"Content-Type: text/plain; charset=utf-8\r\n"
			+ f"Content-Length: {len(body)}\r\n".encode("ascii")
			+ b"Connection: close\r\n\r\n"
			+ body
		)
		writer.write(response)
		await writer.drain()
	finally:
		writer.close()
		with suppress(Exception):
			await writer.wait_closed()


async def run_health_server() -> None:
	host = os.getenv("HOST", "0.0.0.0")
	port = int(os.getenv("PORT", "10000"))
	server = await asyncio.start_server(_handle_healthcheck, host, port)
	print(f"HEALTHCHECK server listening on {host}:{port}", flush=True)
	async with server:
		await server.serve_forever()


async def main() -> None:
	await asyncio.gather(
		forwarder2.run(),
		run_health_server(),
	)


class GroupMediaForwarder:
	"""从指定群组抓取媒体消息并转发到目标。"""

	def __init__(
		self,
		target_group: int | str,
		forward_to: str,
		start_message_id: int = 1,
		caption_json_mode: bool = False,
		skip_caption_check: bool = False,
		state_file: Path | None = None,
		white_list_group_1: list[str] | None = None,
		white_list_group_2: list[str] | None = None,
		black_list: list[str] | None = None,
	) -> None:
		self.target_group = target_group
		self.forward_to = forward_to
		self.default_start_message_id = start_message_id
		self.caption_json_mode = caption_json_mode
		self.skip_caption_check = skip_caption_check
		self.state_file = state_file or Path(__file__).with_name("man_last_message_id.txt")
		self.white_list_group_1 = white_list_group_1 or []
		self.white_list_group_2 = white_list_group_2 or []
		self.black_list = black_list or []

	# ── 工具方法 ─────────────────────────────────────────────

	@staticmethod
	def serialize_message(message) -> dict:
		return {
			"id": message.id,
			"date": message.date.isoformat() if message.date else None,
			"sender_id": getattr(message, "sender_id", None),
			"text": message.message or "",
		}

	def classify_text(self, text: str) -> str:
		for keyword in self.white_list_group_1:
			if keyword and keyword in text:
				return "group_1"
		for keyword in self.white_list_group_2:
			if keyword and keyword in text:
				return "group_2"
		return "group_3"

	def is_blacklisted(self, text: str) -> bool:
		return any(kw and kw in text for kw in self.black_list)

	def _load_state_data(self) -> dict[str, int]:
		if not self.state_file.exists():
			return {}

		content = self.state_file.read_text(encoding="utf-8").strip()
		if not content:
			return {}

		# 向下兼容旧格式：文件只是一個數字
		if content.isdigit():
			return {str(self.target_group): int(content)}

		try:
			data = json.loads(content)
		except json.JSONDecodeError:
			return {}

		if not isinstance(data, dict):
			return {}

		state_data: dict[str, int] = {}
		for group_key, msg_id in data.items():
			if isinstance(group_key, str) and isinstance(msg_id, int):
				state_data[group_key] = msg_id

		return state_data

	def _write_state_data(self, data: dict[str, int]) -> None:
		self.state_file.write_text(
			json.dumps(data, ensure_ascii=False, indent=2),
			encoding="utf-8",
		)

	def resolve_start_message_id(self) -> int:
		state_data = self._load_state_data()
		last_id = state_data.get(str(self.target_group))
		if isinstance(last_id, int):
			return last_id
		return self.default_start_message_id

	def write_last_message_id(self, message_id: int) -> None:
		state_data = self._load_state_data()
		state_data[str(self.target_group)] = message_id
		self._write_state_data(state_data)

	@staticmethod
	def _extract_button_info(message) -> dict:
		"""解析按鈕資訊，並特別提取「复制链接」按鈕連結。"""
		buttons = []
		copy_link_targets = []

		rows = getattr(message, "buttons", None)
		if rows:
			for row in rows:
				for btn in row:
					text = getattr(btn, "text", "") or ""
					url = getattr(btn, "url", None)
					copy_text = getattr(btn, "copy_text", None)
					button_obj = getattr(btn, "button", None)
					if not url and button_obj is not None:
						url = getattr(button_obj, "url", None)
					if copy_text is None and button_obj is not None:
						copy_text = getattr(button_obj, "copy_text", None)

					# 某些封裝下 copy_text 可能是物件，實際值在 .text
					if copy_text is not None and not isinstance(copy_text, str):
						copy_text = getattr(copy_text, "text", None)

					buttons.append({
						"text": text,
						"url": url,
						"copy_text": copy_text,
					})

					if "复制链接" in text:
						target = copy_text or url
						if target:
							copy_link_targets.append(target)

		return {
			"buttons": buttons,
			"copy_link_targets": copy_link_targets,
		}

	@staticmethod
	def _extract_photo_info(message) -> dict:
		"""提取 photo 基本資訊。"""
		photo = getattr(message, "photo", None)
		if not photo:
			return {"has_photo": False}

		return {
			"has_photo": True,
			"photo_id": getattr(photo, "id", None),
		}

	def _format_caption(self, message, caption: str) -> str:
		"""根据配置决定是否将 caption 封装为 JSON。"""
		if not self.caption_json_mode:
			return caption

		media_type = "photo" if getattr(message, "photo", None) else (
			"video" if getattr(message, "video", None) else (
				"document" if getattr(message, "document", None) else "text"
			)
		)

		payload = {
			"caption": caption,
			"media_type": media_type,
			"message_id": getattr(message, "id", None),
			"sender_id": getattr(message, "sender_id", None),
			"date": message.date.isoformat() if getattr(message, "date", None) else None,
		}

		if media_type == "photo":
			payload["photo"] = self._extract_photo_info(message)
			payload["inline_buttons"] = self._extract_button_info(message)

		return json.dumps(payload, ensure_ascii=False)

	async def _resolve_source_entity(self, client: TelegramClient):
		"""
		解析來源實體：
		1) 先用 Telethon 直接解析（群組 id / username / chat id）
		2) 若是 user/bot 純數字 id 失敗，則從現有 dialogs 以 id 補找
		"""
		try:
			return await client.get_entity(self.target_group)
		except ValueError as exc:
			numeric_id = None
			if isinstance(self.target_group, int):
				numeric_id = self.target_group
			elif isinstance(self.target_group, str) and self.target_group.lstrip("-").isdigit():
				numeric_id = int(self.target_group)

			if numeric_id is not None:
				target_abs = abs(numeric_id)
				async for dialog in client.iter_dialogs():
					entity_id = getattr(dialog.entity, "id", None)
					if entity_id == target_abs:
						return dialog.entity

			raise ValueError(
				f"无法解析来源 target_group={self.target_group}。"
				"若这是机器人 user_id，请先私聊该机器人一次，"
				"或改用 @username 作为 target_group。"
			) from exc

	async def _resend_message(self, client: TelegramClient, forward_entity, message, caption_override: str | None = None) -> None:
		"""當來源聊天禁止轉傳時，改為下載並重新發送內容。"""
		caption = caption_override if caption_override is not None else (message.message or "")

		if getattr(message, "media", None):
			with tempfile.TemporaryDirectory(prefix="man_media_") as tmp_dir:
				downloaded_path = await client.download_media(message, file=tmp_dir)
				if downloaded_path:
					send_kwargs = {
						"entity": forward_entity,
						"file": downloaded_path,
						"caption": caption,
					}

					# 盡量保留訊息型態
					if getattr(message, "video", None):
						send_kwargs["supports_streaming"] = True
					if getattr(message, "voice", None):
						send_kwargs["voice_note"] = True
					if getattr(message, "video_note", None):
						send_kwargs["video_note"] = True

					await client.send_file(**send_kwargs)
					return

		if caption:
			await client.send_message(entity=forward_entity, message=caption)

	# ── 核心异步方法 ──────────────────────────────────────────

	async def fetch_messages(self, start_message_id: int, limit: int) -> list[dict]:
		client = _build_client()
		await client.start()
		me = await client.get_me()
		print(f"已登入 Telegram 帳號：{me.username} (id={me.id})",flush=True)


		try:
			entity = await self._resolve_source_entity(client)
			messages = []
			async for message in client.iter_messages(
				entity,
				min_id=start_message_id - 1,
				reverse=True,
				limit=limit,
			):
				messages.append(self.serialize_message(message))
			return messages
		finally:
			await client.disconnect()

	async def fetch_and_forward(self, start_message_id: int) -> int:
		client = _build_client()
		await client.start()
		me = await client.get_me()
		print(f"SESSION_READY 已登入 Telegram 帳號：{me.username} (id={me.id})", flush=True)
		
		try:
			source_entity = await self._resolve_source_entity(client)
			forward_entity = await client.get_entity(self.forward_to)
			last_message_id = start_message_id - 1

			async for message in client.iter_messages(
				source_entity,
				min_id=start_message_id - 1,
				reverse=True,
			):
				last_message_id = message.id
				text = self.serialize_message(message).get("text", "")

				if self.skip_caption_check:
					should_forward = True
				else:
					if self.is_blacklisted(text):
						continue
					should_forward = self.classify_text(text) in {"group_1", "group_2"}

				if should_forward:
					formatted_caption = self._format_caption(message, text)

					if self.caption_json_mode:
						await self._resend_message(client, forward_entity, message, caption_override=formatted_caption)
					else:
						try:
							await client.forward_messages(
								entity=forward_entity,
								messages=[message.id],
								from_peer=source_entity,
							)
						except ChatForwardsRestrictedError:
							await self._resend_message(client, forward_entity, message, caption_override=formatted_caption)
					await asyncio.sleep(random.randint(300, 1200))

				# 不论是否转发，已检查过的消息都推进游标，避免重复检查旧消息
				self.write_last_message_id(message.id)

			return last_message_id
		finally:
			await client.disconnect()

	async def wait_for_new_message(self, last_seen_message_id: int, poll_interval_sec: int = 5) -> int:
		"""阻塞等待，直到 target_group 出现比 last_seen_message_id 更新的消息。"""
		client = _build_client()
		await client.start()
		try:
			source_entity = await self._resolve_source_entity(client)
			while True:
				latest_id = None
				async for latest_msg in client.iter_messages(source_entity, limit=1):
					latest_id = latest_msg.id
					break

				if latest_id is not None and latest_id > last_seen_message_id:
					return latest_id

				await asyncio.sleep(poll_interval_sec)
		finally:
			await client.disconnect()

	async def has_messages_in_target_group(self) -> bool:
		"""检查来源 target_group 是否至少有一条消息。"""
		client = _build_client()
		await client.start()
		try:
			source_entity = await self._resolve_source_entity(client)
			async for _ in client.iter_messages(source_entity, limit=1):
				return True
			return False
		finally:
			await client.disconnect()

	async def run(self) -> None:
		next_start_id = self.resolve_start_message_id()
		print(f"[Run] 从 start_message_id={next_start_id} 开始检查。", flush=True)

		while True:
			last_checked_id = await self.fetch_and_forward(next_start_id)
			print(f"[Done] 已检查到 message_id={last_checked_id}。进入等待新消息...", flush=True)

			last_seen = max(last_checked_id, next_start_id - 1)
			new_latest_id = await self.wait_for_new_message(last_seen)
			next_start_id = last_seen + 1
			print(f"[Wake] 检测到新消息 latest_id={new_latest_id}，从 message_id={next_start_id} 继续检查。", flush=True)


# ── 实例配置 ──────────────────────────────────────────────────

forwarder = GroupMediaForwarder(
	target_group=-1001907741385,
	forward_to="ziyuanbudengbot",
	start_message_id=0,
	caption_json_mode=False,
	skip_caption_check=False,
	white_list_group_1=[
		"时代峰峻","TF家族","佟弋","渣苏感","计铭浩","文铭","铭罕","刘瀚辰","穆祉丞","陈浚铭",
		"陈思罕","张桂源","朱映宸","杨智岩","严浩翔","沈子航","智恩涵","朱广伦","萌娃","人类幼崽",
		"男孩","小宝宝","小孩","韩维辰","星星贴纸","少年感","养成系","练习生","骗你生儿子",
	],
	white_list_group_2=[
		"小男娘","正太","弟弟","初中","男初","南梁",
	],
	black_list=[
		"白肥","狂野男孩","想法哭小正太","橘子海","巨乳","男同","小孩姐","小萝莉","腹肌体育生",
		"蜜桃洨小孩","学妹","兵哥","18岁","19岁","遇上歹徒","大学生","薄肌男孩","男高","肌肉",
		"GV","女儿","健身","男大","女初","绿帽癖","体院","羊毛卷","wataa","radewa","Haley",
		"从地板干到落地窗",
	],
)

forwarder2 = GroupMediaForwarder(
	target_group=7294369541,
	forward_to="Tin9HutBot",
	start_message_id=0,
	caption_json_mode=True,
	skip_caption_check=True,
	white_list_group_1=[],
	white_list_group_2=[],
	black_list=[],
)

if __name__ == "__main__":
	asyncio.run(main())
