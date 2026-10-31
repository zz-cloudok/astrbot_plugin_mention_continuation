import copy
import time
from datetime import datetime, timezone

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll, Image, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)


FOLLOWUP_WAIT_SECONDS = 10
RECENT_IMAGE_TTL_SECONDS = 120
RECENT_IMAGE_MAX_COUNT = 4
RECENT_CONTEXT_MAX_COUNT = 8
RECENT_CONTEXT_MAX_CHARS = 3000
DETAILED_IMAGE_REQUEST_MARKERS = (
    "详细",
    "仔细",
    "认真看",
    "看清",
    "识别",
    "识图",
    "分析",
    "看看",
    "看一下",
    "这张图",
    "这些图",
    "刚才的图",
    "图片里",
    "图里",
    "图中",
    "截图",
    "写了什么",
    "说的什么",
    "什么字",
    "什么内容",
)

CONTINUATION_PROMPT = (
    "这是一条只 @ 了你、没有附带文字的群聊消息。"
    "请结合最近群聊上下文，继续回应上文里尚未处理的话题；"
    "不要询问对方为什么只 @ 不说话，也不要复述或解释这次 @。"
    "如果上文没有可承接的内容，再用一句自然简短的话问对方想说什么。"
)

RECENT_CONTEXT_PROMPT = (
    "\n\n[紧邻这次 @ 的群聊记录]\n"
    "下面是同一群聊在本次 @ 之前最近的少量消息，按时间从早到晚排列。"
    "请优先承接最后一条有正文的消息；只有最后一条确实没有可承接内容时，"
    "才考虑更早的记录。不要因为更早的旧话题较长或更有故事性，就跳过紧邻的上下文。\n"
)

MERGED_FOLLOWUP_PROMPT = (
    "\n\n[单独 @ 后的补充消息]\n"
    "用户先单独 @ 了你，随后又发送了一条补充内容。"
    "请把新的补充内容视为本次真正要对你说的话，优先直接回应它，"
    "并结合最近群聊上下文理解指代。不要解释等待过程，也不要询问用户为什么先只 @。"
)

CURRENT_MESSAGE_PRIORITY_PROMPT = (
    "\n\n[当前消息优先]\n"
    "这一轮用户已经 @ 了你，并提供了明确正文。必须优先、直接回应正文表达的问题或话题；"
    "群聊历史、图片描述和长期记忆只用于理解人物与指代，不能覆盖当前正文。"
    "如果正文与旧话题不同，就按新正文回答，不要继续上一轮旧话题。"
)

RECENT_IMAGE_PROMPT = (
    "\n\n[最近图片原文]\n"
    "用户明确要求查看最近发送的图片。请求中已附带该用户近期发送的原图，"
    "请结合当前正文详细观察这些图片，优先回答用户指定的问题，"
    "不要只依赖此前的简短图片摘要。"
)


def is_standalone_self_mention(event: AstrMessageEvent) -> bool:
    messages = tuple(event.get_messages())
    return (
        len(messages) == 1
        and isinstance(messages[0], At)
        and str(messages[0].qq) == str(event.get_self_id())
    )


def has_self_mention(event: AstrMessageEvent) -> bool:
    return any(
        isinstance(component, At)
        and str(component.qq) == str(event.get_self_id())
        for component in event.get_messages()
    )


def is_mergeable_followup(event: AstrMessageEvent) -> bool:
    messages = tuple(event.get_messages())
    if not messages:
        return False
    if any(isinstance(component, (At, AtAll, Reply)) for component in messages):
        return False
    has_content = any(
        isinstance(component, Image)
        or (
            isinstance(component, Plain)
            and bool((component.text or "").strip())
        )
        for component in messages
    )
    if not has_content:
        return False
    text = (event.message_str or "").strip()
    return not text.startswith(("/", "!", "#sl"))


def format_history_parts(content: dict) -> str:
    parts: list[str] = []
    for part in content.get("message", []):
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type", ""))
        if part_type == "plain":
            text = str(part.get("text", "")).strip()
            if text:
                parts.append(text)
        elif part_type == "at":
            name = str(part.get("name", "")).strip()
            user_id = str(part.get("user_id", "")).strip()
            parts.append(f"@{name or user_id}")
        elif part_type == "reply":
            text = str(part.get("text", "")).strip()
            if text:
                parts.append(f"[引用：{text}]")
        else:
            parts.append(f"[{part_type or '消息'}]")
    return "".join(parts).strip()


class SenderScopedSessionFilter(SessionFilter):
    """Only let the same user in the same chat wake this waiter."""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}\x1f{event.get_sender_id()}"


class MentionContinuationPlugin(Star):
    """Wait briefly for content after a standalone mention before replying."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._recent_images: dict[
            tuple[str, str],
            list[tuple[float, Image]],
        ] = {}

    @staticmethod
    def wants_detailed_image_review(text: str) -> bool:
        normalized = (text or "").strip()
        return any(marker in normalized for marker in DETAILED_IMAGE_REQUEST_MARKERS)

    def remember_recent_images(self, event: AstrMessageEvent) -> None:
        images = [
            copy.deepcopy(component)
            for component in event.get_messages()
            if isinstance(component, Image)
        ]
        if not images:
            return

        now = time.monotonic()
        key = (event.unified_msg_origin, str(event.get_sender_id()))
        recent = [
            item
            for item in self._recent_images.get(key, [])
            if now - item[0] <= RECENT_IMAGE_TTL_SECONDS
        ]
        recent.extend((now, image) for image in images)
        self._recent_images[key] = recent[-RECENT_IMAGE_MAX_COUNT:]

    def attach_recent_images(self, event: AstrMessageEvent, text: str) -> int:
        if not self.wants_detailed_image_review(text):
            return 0
        if any(isinstance(component, Image) for component in event.get_messages()):
            return 0

        key = (event.unified_msg_origin, str(event.get_sender_id()))
        now = time.monotonic()
        recent = [
            item
            for item in self._recent_images.get(key, [])
            if now - item[0] <= RECENT_IMAGE_TTL_SECONDS
        ]
        if not recent:
            self._recent_images.pop(key, None)
            return 0

        images = [copy.deepcopy(image) for _, image in recent]
        event.message_obj.message.extend(images)
        event.set_extra("_recent_images_attached", len(images))
        self._recent_images.pop(key, None)
        self.logger.info(
            "Attached %d recent image(s) for detailed review: %s sender=%s",
            len(images),
            event.unified_msg_origin,
            event.get_sender_id(),
        )
        return len(images)

    async def build_recent_context(self, event: AstrMessageEvent) -> str:
        history_manager = getattr(
            self.context,
            "message_history_manager",
            None,
        )
        if history_manager is None:
            return ""

        try:
            records = await history_manager.get(
                platform_id=event.get_platform_id(),
                user_id=event.unified_msg_origin,
                page=1,
                page_size=RECENT_CONTEXT_MAX_COUNT + 1,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to load recent group context for standalone mention: %s",
                exc,
            )
            return ""

        self_id = str(event.get_self_id())
        event_time = datetime.fromtimestamp(
            event.created_at,
            timezone.utc,
        ).replace(tzinfo=None)
        lines: list[str] = []
        for record in records:
            created_at = getattr(record, "created_at", None)
            if isinstance(created_at, datetime):
                record_time = created_at.replace(tzinfo=None)
                if record_time > event_time:
                    continue
            content = record.content if isinstance(record.content, dict) else {}
            text = format_history_parts(content)
            if not text:
                continue

            parts = content.get("message", [])
            only_self_mention = (
                len(parts) == 1
                and isinstance(parts[0], dict)
                and parts[0].get("type") == "at"
                and str(parts[0].get("user_id", "")) == self_id
            )
            if only_self_mention and str(record.sender_id) == str(
                event.get_sender_id()
            ):
                continue

            sender = (
                str(record.sender_name or "").strip()
                or str(record.sender_id or "").strip()
                or "群友"
            )
            lines.append(f"{sender}：{text}")

        compact: list[str] = []
        remaining = RECENT_CONTEXT_MAX_CHARS
        for line in reversed(lines[-RECENT_CONTEXT_MAX_COUNT:]):
            if len(line) > remaining:
                break
            compact.append(line)
            remaining -= len(line)
        compact.reverse()
        return "\n".join(compact)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    async def handle_standalone_mention(self, event: AstrMessageEvent) -> None:
        self.remember_recent_images(event)
        direct_mention = has_self_mention(event)

        if direct_mention and (event.message_str or "").strip():
            event.set_extra("_mention_current_message_priority", True)
            event.set_extra("_ambient_reply", False)
            self.attach_recent_images(event, event.message_str)
            return

        if not is_standalone_self_mention(event):
            return

        event.set_extra("_mention_continuation", True)
        outcome = {"kind": ""}

        @session_waiter(FOLLOWUP_WAIT_SECONDS)
        async def wait_for_followup(
            controller: SessionController,
            followup: AstrMessageEvent,
        ) -> None:
            if not is_mergeable_followup(followup):
                # Do not swallow commands, another @, or replies.
                forwarded = copy.copy(followup)
                self.context.get_event_queue().put_nowait(forwarded)
                outcome["kind"] = "pass_through"
                followup.stop_event()
                controller.stop()
                return

            followup.message_obj.message.insert(
                0,
                At(qq=followup.get_self_id(), name=followup.get_self_id()),
            )
            merged = copy.copy(followup)
            merged.set_extra("_mention_followup_merged", True)
            self.attach_recent_images(merged, followup.message_str)
            self.context.get_event_queue().put_nowait(merged)
            self.logger.info(
                "Merged standalone mention follow-up for %s sender=%s",
                followup.unified_msg_origin,
                followup.get_sender_id(),
            )
            outcome["kind"] = "merged"
            followup.stop_event()
            controller.stop()

        try:
            await wait_for_followup(event, SenderScopedSessionFilter())
        except TimeoutError:
            outcome["kind"] = "timeout"
        except Exception as exc:
            self.logger.error(
                "Standalone mention follow-up wait failed: %s",
                exc,
            )
            event.should_call_llm(True)
            event.stop_event()
            return

        if outcome["kind"] in {"merged", "pass_through"}:
            event.should_call_llm(True)
            event.stop_event()
            return

        self.logger.info(
            "Standalone mention follow-up timed out after %ss for %s sender=%s",
            FOLLOWUP_WAIT_SECONDS,
            event.unified_msg_origin,
            event.get_sender_id(),
        )
        recent_context = await self.build_recent_context(event)
        continuation_prompt = CONTINUATION_PROMPT
        if recent_context:
            continuation_prompt += RECENT_CONTEXT_PROMPT + recent_context
            event.set_extra("_mention_recent_context_injected", True)
            self.logger.info(
                "Injected %d recent line(s) into standalone mention context "
                "for %s sender=%s",
                recent_context.count("\n") + 1,
                event.unified_msg_origin,
                event.get_sender_id(),
            )
        event.message_str = continuation_prompt
        event.message_obj.message_str = continuation_prompt

    @filter.on_llm_request(priority=1000)
    async def add_merged_followup_context(
        self,
        event: AstrMessageEvent,
        *args,
    ) -> None:
        # AstrBot versions and hook wrappers can add positional arguments here.
        # Locate ProviderRequest by its stable interface instead of relying on
        # the argument position, which previously broke standalone mentions.
        req = next(
            (
                candidate
                for candidate in args
                if hasattr(candidate, "system_prompt")
            ),
            None,
        )
        if req is None:
            self.logger.warning(
                "LLM request hook did not receive a ProviderRequest: %s",
                [type(candidate).__name__ for candidate in args],
            )
            return
        if event.get_extra("_mention_current_message_priority", False):
            req.system_prompt += CURRENT_MESSAGE_PRIORITY_PROMPT
        if event.get_extra("_mention_followup_merged", False):
            req.system_prompt += MERGED_FOLLOWUP_PROMPT
        if event.get_extra("_recent_images_attached", 0):
            req.system_prompt += RECENT_IMAGE_PROMPT
