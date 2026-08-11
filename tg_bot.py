from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from carrier_profile import ACTIVE_CARRIER, ServiceSms
from config_store import ConfigStore, ConfigValidationError

try:
    import requests
except ImportError:  # requests 缺失时静默禁用 Telegram，不影响 Web 和串口功能
    requests = None


MAX_PENDING_MESSAGES = 100
LONG_POLL_SECONDS = 20
CONFIRMATION_TTL_SECONDS = 10 * 60
MAX_BACKOFF_SECONDS = 5 * 60
INCOMING_SETTLE_SECONDS = 8.0
TELEGRAM_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "status", "description": "查看设备、IMS、余额和存储状态"},
    {"command": "balance", "description": "查看本地保存的余额"},
    {"command": "querybalance", "description": "向 888 查询余额（需确认）"},
    {"command": "history", "description": "查看最近收发记录"},
    {"command": "send", "description": "发送短信（需确认）"},
    {"command": "help", "description": "显示帮助"},
)


class TelegramRequestError(RuntimeError):
    """Telegram 请求失败，区分可重试和永久错误。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class PendingRequest:
    method: str
    payload: dict[str, Any]
    use_owner_chat: bool = False
    archive_message_id: str | None = None
    source_archive_ids: tuple[str, ...] = ()


@dataclass
class PendingSend:
    recipient: str
    text: str
    created_at: float


def _create_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"sms-tool.telegram.{log_path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s [Telegram] %(levelname)s %(message)s")
            )
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
    return logger


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _archive_inbound_messages(path: Path) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("direction", "in") == "in"
                    and item.get("id")
                ):
                    messages.append(item)
    except OSError:
        pass
    return messages


class TelegramBot(threading.Thread):
    """轻量 Telegram long-polling 客户端，不直接访问串口。"""

    def __init__(
        self,
        *,
        config_path: Path,
        state_path: Path,
        archive_path: Path,
        log_path: Path,
        send_sms: Callable[[str, str], dict[str, Any]],
        status_provider: Callable[[], dict[str, Any]],
        balance_setter: Callable[[float], None],
        message_provider: Callable[[], list[dict[str, Any]]] | None = None,
        session: Any | None = None,
        config_store: ConfigStore | None = None,
    ) -> None:
        super().__init__(name="sms-telegram-bot", daemon=True)
        self.config_path = Path(config_path)
        self.config_store = config_store or ConfigStore(self.config_path)
        self.state_path = Path(state_path)
        self.archive_path = Path(archive_path)
        self.send_sms = send_sms
        self.status_provider = status_provider
        self.balance_setter = balance_setter
        self.message_provider = message_provider
        self.logger = _create_logger(Path(log_path))
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._outbox: deque[PendingRequest] = deque()
        self._queued_archive_ids: set[str] = set()
        self._pending_sends: dict[str, PendingSend] = {}
        self._incoming_refresh_due: float | None = None
        self._last_connected: bool | None = None
        self._has_seen_connected = False
        # 用于识别"软件离线期间"积压的命令:Telegram 消息 date 早于启动时刻即视为积压
        self._started_at = time.time()
        self._offline_notice_sent = False
        self._commands_registered = False

        self._config = self.config_store.read_runtime()
        telegram_config = (
            self._config.get("telegram")
            if isinstance(self._config, dict)
            else None
        )
        if not isinstance(telegram_config, dict):
            telegram_config = {}
        self.token = str(telegram_config.get("token") or "").strip()
        self.chat_id = telegram_config.get("chat_id")
        self.proxy = str(telegram_config.get("proxy") or "").strip()

        alerts = self._config.get("alerts") if isinstance(self._config, dict) else None
        if not isinstance(alerts, dict):
            alerts = {}
        self.low_balance_gbp = self._finite_number(
            alerts.get("low_balance_gbp"), default=2.0
        )
        self.keepalive_warn_days = int(
            self._finite_number(alerts.get("keepalive_warn_days"), default=14.0)
        )

        backend_available = session is not None or requests is not None
        configured_enabled = telegram_config.get("enabled")
        if not isinstance(configured_enabled, bool):
            configured_enabled = bool(self.token)
        self.enabled = bool(configured_enabled and self.token and backend_available)
        self._session = session
        self._state: dict[str, Any] = {}
        self._pushed_archive_ids: set[str] = set()
        self._last_update_id: int | None = None
        if not self.enabled:
            return

        if self._session is None:
            self._session = requests.Session()
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        if hasattr(self._session, "proxies"):
            self._session.proxies.clear()
            if self.proxy:
                self._session.proxies.update(
                    {"http": self.proxy, "https": self.proxy}
                )

        state_exists = self.state_path.exists()
        loaded_state = _read_json_object(self.state_path)
        self._state = loaded_state if loaded_state is not None else {}
        pushed_ids = self._state.get("pushed_message_ids")
        if isinstance(pushed_ids, list):
            self._pushed_archive_ids = {
                str(item) for item in pushed_ids if isinstance(item, (str, int))
            }
        last_update_id = self._state.get("last_update_id")
        if isinstance(last_update_id, int):
            self._last_update_id = last_update_id
        if not isinstance(self._state.get("alerts"), dict):
            self._state["alerts"] = {}

        archive_messages = self._incoming_messages()

        # 首次接入只建立历史基线，不把已有 archive 当作新短信轰炸 owner。
        if not state_exists or loaded_state is None:
            for item in archive_messages:
                archive_id = str(item.get("id") or "").strip()
                if archive_id:
                    self._pushed_archive_ids.add(archive_id)
                self._pushed_archive_ids.update(self._source_ids(item))
            self._save_state_safely()
        else:
            # 已有状态文件时，补排队上次断网或异常退出前尚未送达的逻辑短信。
            # 旧版本按物理分段记录成功状态；全部分段已推送时只迁移逻辑 ID，
            # 不重新发送整条历史长短信。
            self._queue_incoming_messages(archive_messages)

    @staticmethod
    def _source_ids(message: dict[str, Any]) -> tuple[str, ...]:
        value = message.get("_source_ids")
        if not isinstance(value, (list, tuple, set)):
            return ()
        return tuple(
            str(item).strip()
            for item in value
            if isinstance(item, (str, int)) and str(item).strip()
        )

    def _incoming_messages(self) -> list[dict[str, Any]]:
        if self.message_provider is None:
            return _archive_inbound_messages(self.archive_path)
        try:
            value = self.message_provider()
        except Exception as exc:
            self.logger.exception(
                "读取逻辑短信快照失败：%s",
                self._redact(exc),
            )
            return []
        if not isinstance(value, list):
            self.logger.error("逻辑短信快照格式无效")
            return []
        return [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("direction", "in") == "in"
            and item.get("id")
        ]

    def _queue_incoming_messages(self, messages: list[dict[str, Any]]) -> None:
        state_changed = False
        for message in messages:
            archive_id = str(message.get("id") or "").strip()
            if not archive_id:
                continue
            source_ids = self._source_ids(message)
            with self._lock:
                if (
                    archive_id in self._pushed_archive_ids
                    or archive_id in self._queued_archive_ids
                ):
                    continue
                if source_ids and all(
                    source_id in self._pushed_archive_ids
                    for source_id in source_ids
                ):
                    self._pushed_archive_ids.add(archive_id)
                    state_changed = True
                    continue
            self._queue_incoming_message(
                message,
                archive_message_id=archive_id,
                source_archive_ids=source_ids,
            )
        if state_changed:
            self._save_state_safely()

    def _queue_due_incoming(self, *, force: bool = False) -> None:
        if self.message_provider is None:
            return
        with self._lock:
            due = self._incoming_refresh_due
            if due is None:
                return
            if not force and time.monotonic() < due:
                return
            self._incoming_refresh_due = None
        self._queue_incoming_messages(self._incoming_messages())

    @staticmethod
    def _finite_number(value: Any, *, default: float) -> float:
        if isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @property
    def owner_bound(self) -> bool:
        return self.chat_id is not None

    def stop(self) -> None:
        self._stop_event.set()

    def _redact(self, value: Any) -> str:
        text = str(value)
        return text.replace(self.token, "***") if self.token else text

    def _save_state_locked(self) -> None:
        self._state["pushed_message_ids"] = sorted(self._pushed_archive_ids)
        self._state["last_update_id"] = self._last_update_id
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.state_path)

    def _save_state_safely(self) -> None:
        try:
            with self._lock:
                self._save_state_locked()
        except OSError as exc:
            self.logger.error("Telegram 状态保存失败：%s", self._redact(exc))

    def _enqueue(self, item: PendingRequest) -> None:
        with self._lock:
            archive_id = item.archive_message_id
            if archive_id and (
                archive_id in self._pushed_archive_ids
                or archive_id in self._queued_archive_ids
            ):
                return
            if len(self._outbox) >= MAX_PENDING_MESSAGES:
                dropped = self._outbox.popleft()
                if dropped.archive_message_id:
                    self._queued_archive_ids.discard(dropped.archive_message_id)
                self.logger.warning("Telegram 待发队列已满，已丢弃最旧消息")
            self._outbox.append(item)
            if archive_id:
                self._queued_archive_ids.add(archive_id)

    def _queue_text(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        archive_message_id: str | None = None,
        source_archive_ids: tuple[str, ...] = (),
    ) -> None:
        payload: dict[str, Any] = {"text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._enqueue(
            PendingRequest(
                "sendMessage",
                payload,
                use_owner_chat=True,
                archive_message_id=archive_message_id,
                source_archive_ids=source_archive_ids,
            )
        )

    def _queue_plain_chunks(self, text: str) -> None:
        remaining = text
        while remaining:
            if len(remaining) <= 3800:
                chunk, remaining = remaining, ""
            else:
                split_at = remaining.rfind("\n", 0, 3800)
                if split_at < 1000:
                    split_at = 3800
                chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip("\n")
            self._queue_text(chunk)

    def _api_call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        read_timeout: float = 25.0,
    ) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            response = self._session.post(
                url,
                json=payload,
                timeout=(10.0, read_timeout),
            )
        except Exception as exc:
            raise TelegramRequestError(
                f"{method} 网络请求失败：{self._redact(exc)}"
            ) from None

        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            result = response.json()
        except Exception:
            result = None
        if status_code >= 500:
            raise TelegramRequestError(f"{method} 服务端返回 HTTP {status_code}")
        if status_code >= 400 or not isinstance(result, dict) or not result.get("ok"):
            error_code = result.get("error_code") if isinstance(result, dict) else status_code
            description = (
                result.get("description") if isinstance(result, dict) else "响应格式无效"
            )
            retryable = error_code == 429 or (isinstance(error_code, int) and error_code >= 500)
            raise TelegramRequestError(
                f"{method} 失败（{error_code}）：{description}",
                retryable=retryable,
            )
        return result.get("result")

    def _ensure_commands_registered(self) -> None:
        if self._commands_registered:
            return
        self._api_call(
            "setMyCommands",
            {"commands": [dict(item) for item in TELEGRAM_COMMANDS]},
            read_timeout=15.0,
        )
        self._commands_registered = True

    def _flush_outbox(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                if not self._outbox:
                    return
                item = self._outbox[0]
                if item.use_owner_chat and not self.owner_bound:
                    return
                payload = dict(item.payload)
                if item.use_owner_chat:
                    payload["chat_id"] = self.chat_id
            try:
                self._api_call(item.method, payload)
            except TelegramRequestError as exc:
                if exc.retryable:
                    raise
                self.logger.error("丢弃不可重试的 Telegram 请求：%s", self._redact(exc))
            with self._lock:
                if self._outbox and self._outbox[0] is item:
                    self._outbox.popleft()
                if item.archive_message_id:
                    self._queued_archive_ids.discard(item.archive_message_id)
                    self._pushed_archive_ids.add(item.archive_message_id)
                    self._pushed_archive_ids.update(item.source_archive_ids)
                    try:
                        self._save_state_locked()
                    except OSError as exc:
                        self.logger.error("推送去重状态保存失败：%s", self._redact(exc))

    def _queue_api_request(self, method: str, payload: dict[str, Any]) -> None:
        self._enqueue(PendingRequest(method, payload))

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            self._api_call("answerCallbackQuery", payload, read_timeout=15.0)
        except TelegramRequestError as exc:
            self.logger.warning("回调确认暂未送达：%s", self._redact(exc))
            self._queue_api_request("answerCallbackQuery", payload)

    def notify_incoming_sms(self, message: dict[str, Any]) -> None:
        """串口线程调用的非阻塞入口。

        使用逻辑短信 provider 时只刷新防抖期限，等同一条长短信的物理分段
        稳定后再读取合并快照；旧调用方没有 provider 时保持即时推送行为。
        """
        if not self.enabled or message.get("direction", "in") != "in":
            return
        if self.message_provider is not None:
            with self._lock:
                self._incoming_refresh_due = (
                    time.monotonic() + INCOMING_SETTLE_SECONDS
                )
            return
        self._queue_incoming_message(message)

    def _queue_incoming_message(
        self,
        message: dict[str, Any],
        *,
        archive_message_id: str | None = None,
        source_archive_ids: tuple[str, ...] = (),
    ) -> None:
        try:
            archive_id = str(
                archive_message_id or message.get("id") or ""
            ).strip()
            if not archive_id:
                return
            sender = html.escape(str(message.get("sender") or "未知发件人"))
            timestamp = html.escape(str(message.get("time") or "未知"))
            raw_body = str(message.get("body") or "")
            code = str(message.get("code") or "").strip()

            suffix = ""
            body = raw_body
            while True:
                escaped_body = html.escape(body)
                text = (
                    f"<b>收到新短信</b>\n"
                    f"发件人：{sender}\n"
                    f"原始时间：{timestamp}\n"
                    f"正文：\n{escaped_body}{suffix}"
                )
                if code:
                    text += f"\n验证码：\n<code>{html.escape(code)}</code>"
                if len(text) <= 4000:
                    break
                body = body[: max(1, int(len(body) * 0.8))]
                suffix = "\n（正文过长，已截断）"
            self._queue_text(
                text,
                parse_mode="HTML",
                archive_message_id=archive_id,
                source_archive_ids=source_archive_ids,
            )
        except Exception as exc:
            self.logger.exception("新短信加入 Telegram 队列失败：%s", self._redact(exc))

    def _write_owner(self, chat_id: Any) -> bool:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            return False
        try:
            self.config_store.bind_telegram_owner(chat_id)
        except (OSError, ConfigValidationError) as exc:
            self.logger.error("owner 绑定写入配置失败：%s", self._redact(exc))
            return False
        self._config = self.config_store.read_runtime()
        self.chat_id = chat_id
        self.logger.info("Telegram owner 首次绑定完成")
        return True

    def _is_owner_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat")
        sender = message.get("from")
        return bool(
            self.owner_bound
            and isinstance(chat, dict)
            and chat.get("type") == "private"
            and isinstance(sender, dict)
            and str(chat.get("id")) == str(self.chat_id)
            and str(sender.get("id")) == str(self.chat_id)
        )

    def _help_text(self) -> str:
        return (
            f"{ACTIVE_CARRIER.name} DJI QDC507 短信工具远程命令\n\n"
            "/status - 查看设备、网络、余额和短信活跃参考\n"
            "/send 号码 内容 - 预览并确认后发送短信\n"
            "/querybalance - 确认后向 888 发送 BAL 查询余额\n"
            "/balance - 查看本地保存的余额（不发短信）\n"
            "/setbalance 14.40 - 手动校准本地余额（不发短信）\n"
            "/history [n] - 最近 n 条收发记录，默认 5，最多 20\n"
            "/help - 显示本说明\n\n"
            f"{ACTIVE_CARRIER.send_warning}只有点击“确认发送”后才会实际发送。"
        )

    def _format_status(self) -> str:
        status = self.status_provider()
        connected = bool(status.get("connected"))
        port = status.get("port") or "未知端口"
        connection = f"已连接 {port}" if connected else "未连接"
        own_number = str(status.get("own_number") or "未知")
        signal = status.get("signal")
        signal_text = f"{signal}/31" if isinstance(signal, int) else "未知"
        registered = "已注册" if status.get("registered") else "未注册"
        roaming = "漫游" if status.get("roaming") else "非漫游"
        operator = str(status.get("operator") or "未知")
        sms_ready = status.get("sms_ready")
        ims_text = (
            "已注册"
            if sms_ready is True
            else "未注册"
            if sms_ready is False
            else "未提供查询"
        )

        balance = status.get("balance")
        if isinstance(balance, dict) and isinstance(balance.get("amount"), (int, float)):
            balance_text = f"£{float(balance['amount']):.2f}"
        else:
            balance_text = "未知"
        days = status.get("keepalive_days_left")
        if isinstance(days, int):
            keepalive = f"剩余 {days} 天" if days >= 0 else f"已超期 {-days} 天"
        else:
            keepalive = "未知"

        wwan = status.get("wwan")
        wwan_state = wwan.get("state") if isinstance(wwan, dict) else "unknown"
        wwan_text = {
            "disabled": "已禁用（安全）",
            "enabled": "未禁用（有流量风险）",
            "absent": "未检测到",
            "unknown": "状态未知",
        }.get(wwan_state, "状态未知")
        used = status.get("storage_used")
        total = status.get("storage_total")
        storage = f"{used}/{total}" if isinstance(used, int) and isinstance(total, int) else "未知"

        return (
            f"设备：{connection}\n"
            f"本机号码：{own_number}\n"
            f"余额：{balance_text}\n"
            f"短信活跃参考：{keepalive}\n"
            f"信号：{signal_text}\n"
            f"网络：{registered} · {roaming} · {operator}\n"
            f"IMS 短信：{ims_text}\n"
            f"WWAN：{wwan_text}\n"
            f"模块收件槽：{storage}"
        )

    def _history_text(self, count: int) -> str:
        provider_ordered = self.message_provider is not None
        if self.message_provider is not None:
            try:
                value = self.message_provider()
                messages = [
                    item for item in value if isinstance(item, dict)
                ]
            except Exception as exc:
                self.logger.exception("读取逻辑短信历史失败：%s", self._redact(exc))
                return "读取短信存档失败，请查看 app.log。"
        else:
            messages = []
            try:
                with self.archive_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict):
                            messages.append(item)
            except OSError as exc:
                self.logger.exception("读取短信存档失败：%s", self._redact(exc))
                return "读取短信存档失败，请查看 app.log。"
        recent = messages[:count] if provider_ordered else list(reversed(messages[-count:]))
        if not recent:
            return "短信存档为空。"
        blocks = [f"最近 {len(recent)} 条短信："]
        for item in recent:
            direction = "发送" if item.get("direction") == "out" else "接收"
            sender = str(item.get("sender") or "未知")
            timestamp = str(item.get("time") or "未知")
            body = str(item.get("body") or "")
            blocks.append(f"[{direction}] {sender}\n时间：{timestamp}\n{body}")
        return "\n\n".join(blocks)

    def _queue_send_confirmation(
        self,
        recipient: str,
        text: str,
        *,
        title: str = "发送预览",
        preview_text: str | None = None,
    ) -> None:
        try:
            status = self.status_provider()
            if isinstance(status, dict) and not status.get("connected"):
                self._queue_text("设备当前未连接（等待设备接入），无法发送短信。")
                return
        except Exception as exc:
            # 状态读取失败不拦截预览,真正的错误会在确认发送时反馈
            self.logger.exception("发送前读取设备状态失败：%s", self._redact(exc))
        token = secrets.token_urlsafe(9)
        with self._lock:
            self._pending_sends[token] = PendingSend(recipient, text, time.monotonic())
        preview = (
            f"{title}\n"
            f"收件人：{recipient}\n"
            f"正文：\n{preview_text if preview_text is not None else text}\n\n"
            f"{ACTIVE_CARRIER.send_warning}请确认是否发送。"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "确认发送", "callback_data": f"send_ok:{token}"},
                {"text": "取消", "callback_data": f"send_cancel:{token}"},
            ]]
        }
        self._queue_text(preview, reply_markup=keyboard)

    def _handle_send_command(self, arguments: str) -> None:
        parts = arguments.split(maxsplit=1)
        if len(parts) != 2:
            self._queue_text("用法：/send 号码 内容")
            return
        recipient, text = parts[0].strip(), parts[1]
        if not re.fullmatch(r"\+?[0-9]{3,20}", recipient):
            self._queue_text("请输入有效的收件人号码，例如 +8613xxxxxxxxx。")
            return
        if not text:
            self._queue_text("短信内容不能为空。")
            return
        if len(text) > 2000:
            self._queue_text("短信内容过长，最多 2000 个字符。")
            return
        self._queue_send_confirmation(recipient, text)

    def _handle_service_sms_command(
        self, arguments: str, action: ServiceSms, usage: str
    ) -> None:
        if arguments.strip():
            self._queue_text(f"用法：{usage}")
            return
        self._queue_send_confirmation(
            action.recipient,
            action.text,
            title=action.title,
            preview_text=action.preview_text,
        )

    def _handle_balance_command(self, arguments: str) -> None:
        if arguments.strip():
            self._queue_text(
                "用法：/balance\n手动校准请用：/setbalance 14.40"
            )
            return

        try:
            status = self.status_provider()
            balance = status.get("balance") if isinstance(status, dict) else None
            amount = balance.get("amount") if isinstance(balance, dict) else None
            if (
                isinstance(amount, (int, float))
                and not isinstance(amount, bool)
                and math.isfinite(float(amount))
            ):
                source = {
                    "sms": "短信解析",
                    "manual": "手动校准",
                }.get(balance.get("source"), "未知")
                timestamp = str(balance.get("time") or "未知")
                current = (
                    f"当前余额：£{float(amount):.2f}"
                    f"（来源：{source}，时间：{timestamp}）"
                )
            else:
                current = "当前余额：未知"
        except Exception as exc:
            self.logger.exception("读取当前余额失败：%s", self._redact(exc))
            current = "当前余额：暂时无法读取，请查看 app.log。"
        self._queue_text(
            current
            + "\n查询最新余额请用：/querybalance"
            + "\n手动校准请用：/setbalance 14.40"
        )

    def _handle_set_balance_command(self, arguments: str) -> None:
        value = arguments.strip()
        if not value:
            self._queue_text("用法：/setbalance 14.40")
            return

        try:
            amount = float(value)
        except (TypeError, ValueError):
            self._queue_text("请输入有效的余额数字，例如 /setbalance 14.40")
            return

        try:
            self.balance_setter(amount)
        except ValueError as exc:
            # StateStore 的 ValueError 是已经过中文整理的 0~1000 业务校验信息。
            self._queue_text(str(exc))
            return
        except OSError as exc:
            self.logger.exception("余额保存失败：%s", self._redact(exc))
            self._queue_text("余额保存失败，请查看 app.log。")
            return
        except Exception as exc:
            self.logger.exception("余额校准异常：%s", self._redact(exc))
            self._queue_text("余额校准失败，请查看 app.log。")
            return
        self._queue_text(f"余额已校准为 £{amount:.2f}。")

    def _handle_history_command(self, arguments: str) -> None:
        if arguments.strip():
            try:
                count = int(arguments.strip())
            except ValueError:
                self._queue_text("用法：/history [n]，n 为 1 到 20。")
                return
        else:
            count = 5
        if not 1 <= count <= 20:
            self._queue_text("历史条数必须在 1 到 20 之间。")
            return
        self._queue_plain_chunks(self._history_text(count))

    def _handle_owner_message(self, message: dict[str, Any]) -> None:
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            self._queue_text(self._help_text())
            return
        command_text, _, arguments = text.partition(" ")
        command = command_text.split("@", 1)[0].lower()
        arguments = arguments.lstrip()
        if command in {"/start", "/help"}:
            self._queue_text(self._help_text())
        elif command == "/status":
            try:
                self._queue_text(self._format_status())
            except Exception as exc:
                self.logger.exception("生成状态消息失败：%s", self._redact(exc))
                self._queue_text("读取状态失败，请查看 app.log。")
        elif command == "/send":
            self._handle_send_command(arguments)
        elif command == "/test":
            self._handle_service_sms_command(
                arguments, ACTIVE_CARRIER.balance_query, "/test"
            )
        elif command == "/querybalance":
            self._handle_service_sms_command(
                arguments, ACTIVE_CARRIER.balance_query, "/querybalance"
            )
        elif command == "/balance":
            self._handle_balance_command(arguments)
        elif command == "/setbalance":
            self._handle_set_balance_command(arguments)
        elif command == "/history":
            self._handle_history_command(arguments)
        else:
            self._queue_text("未知命令。\n\n" + self._help_text())

    def _prune_pending_sends(self) -> None:
        cutoff = time.monotonic() - CONFIRMATION_TTL_SECONDS
        with self._lock:
            expired = [
                token
                for token, pending in self._pending_sends.items()
                if pending.created_at < cutoff
            ]
            for token in expired:
                self._pending_sends.pop(token, None)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from")
        message = callback.get("message")
        if not (
            isinstance(sender, dict)
            and isinstance(message, dict)
            and self._is_owner_message({"from": sender, "chat": message.get("chat")})
        ):
            return
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        action, separator, token = data.partition(":")
        if not separator or action not in {"send_ok", "send_cancel"}:
            return
        self._prune_pending_sends()
        with self._lock:
            pending = self._pending_sends.pop(token, None)
        if pending is None:
            self._answer_callback(callback_id, "该确认已失效")
            return

        self._answer_callback(callback_id, "已确认" if action == "send_ok" else "已取消")
        chat = message.get("chat")
        if isinstance(chat, dict) and message.get("message_id") is not None:
            self._queue_api_request(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat.get("id"),
                    "message_id": message.get("message_id"),
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        if action == "send_cancel":
            self._queue_text("已取消发送。")
            return

        try:
            result = self.send_sms(pending.recipient, pending.text)
        except Exception as exc:
            self.logger.exception("Telegram 发短信调用异常：%s", self._redact(exc))
            result = {"ok": False, "error": "内部异常，请查看 app.log"}
        if result.get("ok"):
            self._queue_text(f"短信已成功发送至 {pending.recipient}。")
        else:
            error = str(result.get("error") or "未知错误")
            self._queue_text(f"短信发送失败：{error}")

    def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        sender = message.get("from")
        if not (
            isinstance(chat, dict)
            and chat.get("type") == "private"
            and isinstance(sender, dict)
            and not sender.get("is_bot")
        ):
            return
        if not self.owner_bound:
            if not self._write_owner(chat.get("id")):
                return
            self._queue_text("已将当前私聊绑定为唯一 owner。")
        if not self._is_owner_message(message):
            return
        message_date = message.get("date")
        if (
            not self._offline_notice_sent
            and isinstance(message_date, int)
            and message_date < self._started_at - 30
        ):
            self._offline_notice_sent = True
            self._queue_text(
                "提示：短信工具此前未在运行，你在离线期间发送的命令刚刚才收到，"
                "以下按当前状态回复。"
            )
        self._handle_owner_message(message)

    def _poll_updates(self) -> None:
        payload: dict[str, Any] = {
            "timeout": LONG_POLL_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._last_update_id is not None:
            payload["offset"] = self._last_update_id + 1
        updates = self._api_call(
            "getUpdates", payload, read_timeout=LONG_POLL_SECONDS + 10.0
        )
        if not isinstance(updates, list):
            raise TelegramRequestError("getUpdates 返回了无效数据", retryable=False)
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            try:
                self._handle_update(update)
            except Exception as exc:
                self.logger.exception("处理 Telegram update 异常：%s", self._redact(exc))
            if isinstance(update_id, int):
                self._last_update_id = update_id
                self._save_state_safely()

    def _check_alerts(self) -> None:
        status = self.status_provider()
        messages: list[str] = []
        should_save = False
        with self._lock:
            alerts = self._state.setdefault("alerts", {})
            if not isinstance(alerts, dict):
                alerts = {}
                self._state["alerts"] = alerts

            wwan = status.get("wwan")
            current_wwan = wwan.get("state") if isinstance(wwan, dict) else None
            previous_wwan = alerts.get("last_wwan_state")
            if current_wwan in {"disabled", "enabled"}:
                if previous_wwan == "disabled" and current_wwan == "enabled":
                    messages.append(
                        "高危告警：WWAN 已从安全禁用状态变为未禁用，存在漫游流量风险，请立即到设备管理器禁用。"
                    )
                if previous_wwan != current_wwan:
                    alerts["last_wwan_state"] = current_wwan
                    should_save = True

            balance = status.get("balance")
            amount = balance.get("amount") if isinstance(balance, dict) else None
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                amount_value = float(amount)
                if math.isfinite(amount_value) and amount_value < self.low_balance_gbp:
                    low_key = (
                        f"{amount_value:.2f}|{balance.get('time')}|{self.low_balance_gbp:.2f}"
                    )
                    if alerts.get("last_low_balance_key") != low_key:
                        messages.append(
                            f"余额告警：当前余额 £{amount_value:.2f}，低于阈值 £{self.low_balance_gbp:.2f}。"
                        )
                        alerts["last_low_balance_key"] = low_key
                        should_save = True
                elif alerts.get("last_low_balance_key") is not None:
                    alerts["last_low_balance_key"] = None
                    should_save = True

            days = status.get("keepalive_days_left")
            today = date.today().isoformat()
            if (
                isinstance(days, int)
                and days <= self.keepalive_warn_days
                and alerts.get("keepalive_last_date") != today
            ):
                messages.append(
                    f"短信活跃提醒：剩余 {days} 天，已达到 "
                    f"{self.keepalive_warn_days} 天提醒阈值；实际保号以 CTExcel 套餐为准。"
                )
                alerts["keepalive_last_date"] = today
                should_save = True

            used = status.get("storage_used")
            total = status.get("storage_total")
            if (
                isinstance(used, int)
                and not isinstance(used, bool)
                and isinstance(total, int)
                and not isinstance(total, bool)
                and total > 0
                and 0 <= used <= total
            ):
                warning_threshold = max(1, total - 5)
                urgent_threshold = max(warning_threshold, total - 2)
                if used >= urgent_threshold:
                    current_storage_level = "urgent"
                elif used >= warning_threshold:
                    current_storage_level = "warning"
                else:
                    current_storage_level = "normal"

                previous_storage_level = alerts.get("last_storage_level")
                if current_storage_level != previous_storage_level:
                    remaining = total - used
                    if current_storage_level == "urgent":
                        messages.append(
                            f"模块收件槽紧急告警：已使用 {used}/{total}，仅剩 "
                            f"{remaining} 格；存储满后可能无法接收新短信。请立即检查。"
                        )
                    elif (
                        current_storage_level == "warning"
                        and previous_storage_level not in {"warning", "urgent"}
                    ):
                        messages.append(
                            f"模块收件槽预警：已使用 {used}/{total}，剩余 "
                            f"{remaining} 格。请检查本地归档和自动清理是否正常。"
                        )
                    elif (
                        current_storage_level == "normal"
                        and previous_storage_level in {"warning", "urgent"}
                    ):
                        messages.append(
                            f"模块收件槽已恢复：当前 {used}/{total}。"
                        )
                    alerts["last_storage_level"] = current_storage_level
                    should_save = True

            if should_save:
                try:
                    self._save_state_locked()
                except OSError as exc:
                    self.logger.error("告警状态保存失败：%s", self._redact(exc))

        connected = bool(status.get("connected"))
        if self._last_connected is None:
            self._last_connected = connected
            self._has_seen_connected = connected
        elif connected != self._last_connected:
            if self._has_seen_connected:
                if connected:
                    port = status.get("port") or "未知端口"
                    messages.append(f"串口已重连：{port}。")
                else:
                    detail = str(status.get("error") or "设备连接已中断")
                    messages.append(f"串口已断开：{detail}")
            if connected:
                self._has_seen_connected = True
            self._last_connected = connected

        for message in messages:
            self._queue_text(message)

    def run(self) -> None:
        if not self.enabled:
            return
        backoff = 1.0
        self.logger.info("Telegram Bot 后台线程已启动")
        try:
            while not self._stop_event.is_set():
                try:
                    self._ensure_commands_registered()
                    self._queue_due_incoming()
                    self._flush_outbox()
                    self._poll_updates()
                    self._queue_due_incoming()
                    self._flush_outbox()
                    self._check_alerts()
                    backoff = 1.0
                except TelegramRequestError as exc:
                    self.logger.warning(
                        "Telegram 通信失败，%.0f 秒后重试：%s",
                        backoff,
                        self._redact(exc),
                    )
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2.0, MAX_BACKOFF_SECONDS)
                except Exception as exc:
                    self.logger.exception(
                        "Telegram Bot 未处理异常，%.0f 秒后重试：%s",
                        backoff,
                        self._redact(exc),
                    )
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2.0, MAX_BACKOFF_SECONDS)
        finally:
            try:
                close = getattr(self._session, "close", None)
                if callable(close):
                    close()
            except Exception as exc:
                self.logger.warning("关闭 Telegram HTTP 会话失败：%s", self._redact(exc))
            self.logger.info("Telegram Bot 后台线程已停止")
