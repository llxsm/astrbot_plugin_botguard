"""BotGuard：识别群里的其他机器人 / AI，阻止两个 AI 无限对聊。

背景
----
AstrBot 自带的 ``ignore_bot_self_message`` 只过滤「自己发出的消息」，
对其他机器人完全没有防护；而唤醒检查里 **被 @** 和 **被引用** 两条路都会
触发 LLM 请求。于是两个都挂着大模型的 QQ 号会互相 @、互相引用，
一条「你好」能滚几十轮，既刷屏又烧 token。

三层防护
--------
1. **收（on_llm_request）**：识别出消息来自其他 bot 时，在 LLM 请求真正
   发出之前 ``event.stop_event()`` 掐断，不消耗 token，也不发任何消息。
   识别依据三种，按可靠性排序：
   - 静态名单（面板配置 ``bot_ids``）
   - 平台原始事件里的 ``is_robot`` 一类字段
   - 行为自动学习：本 bot 发言后每次都被某账号「秒回」，累计 N 次即判定
2. **发（on_decorating_result）**：回复时剥掉指向其他 bot 的 ``At`` 和
   ``Reply`` 段。对方没被 @、没被引用就不会被唤醒 —— 从源头断链。
3. **熔断（轮次限流）**：即使认不出来，只要同一个账号和我们连续来回超过
   ``max_rounds`` 轮，就静默 ``cooldown_seconds`` 秒；期间除非真人说话，
   否则不恢复。

设计取舍
--------
- 真人发言会重置整条链，所以正常问答完全不受影响。
- 管理员账号永不判定为 bot，避免误伤。
- 自动学习有误判可能，``/botguard rm <QQ>`` 可一键移除。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools, register

try:  # Reply 段在部分版本里不在 api 层导出
    from astrbot.api.message_components import Reply
except Exception:  # pragma: no cover
    try:
        from astrbot.core.message.components import Reply
    except Exception:
        Reply = None  # type: ignore[assignment]

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_botguard"
VERSION = "1.0.0"

DEFAULTS = {
    "enable": True,
    "bot_ids": "",
    "auto_learn": True,
    "auto_learn_hits": 4,
    "auto_learn_window": 15.0,
    "auto_learn_expire": 1800,
    "max_rounds": 3,
    "chain_window": 120.0,
    "cooldown_seconds": 600,
    "reset_on_human": True,
    "strip_mention": True,
    "inject_hint": True,
    "hint_text": (
        "[对方是 AI] 这条消息来自另一个 AI 助手（QQ {sender}），不是真人。"
        "你和它已经连续来回 {round} 轮（上限 {max_rounds} 轮）。"
        "请只用 1~2 句简短回应：不要反问、不要追问、不要邀请它继续聊、"
        "不要 @ 或引用它；如果这条消息没有需要你回答的实质内容，就回一句极短的话。"
    ),
    "notify_on_block": False,
    "notify_text": "（和另一个 AI 聊太久了，先歇一会儿）",
    "only_groups": "",
}


@register(PLUGIN_NAME, "llxsm", "识别其他 bot，阻止两个 AI 无限互聊", VERSION)
class BotGuard(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config or {}

        # 运行期状态（不落盘，重启清空）
        self._chain: dict[tuple[str, str], dict] = {}  # (session, partner) -> 链状态
        self._pending: dict[str, float] = {}  # session -> 本 bot 刚发完言的时刻
        self._runtime_enabled: bool | None = None  # /botguard on|off 临时开关

        # 持久化状态
        self._learned: set[str] = set()  # 自动学习到的 bot QQ
        self._hits: dict[str, dict] = {}  # QQ -> {"hits", "first", "last"}
        self._blocked = 0  # 本次运行累计拦截数
        self._last_save = 0.0

        self._state_file = self._resolve_data_file()
        self._load_state()

    # ------------------------------------------------------------------ 基础
    def _conf(self, key: str, default=None):
        value = self.cfg.get(key) if isinstance(self.cfg, dict) else None
        if value is None:
            return DEFAULTS.get(key, default)
        return value

    def _is_enabled(self) -> bool:
        if self._runtime_enabled is not None:
            return self._runtime_enabled
        return bool(self._conf("enable", True))

    def _resolve_data_file(self) -> Path:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME) / "state.json"
        except Exception:
            path = Path("data") / "plugin_data" / PLUGIN_NAME
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return path / "state.json"

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                raw = json.loads(self._state_file.read_text("utf-8"))
                self._learned = {str(x) for x in (raw.get("learned") or [])}
                hits = raw.get("hits") or {}
                if isinstance(hits, dict):
                    self._hits = {
                        str(k): v for k, v in hits.items() if isinstance(v, dict)
                    }
                logger.info(
                    f"[botguard] 已加载状态：自动识别名单 {len(self._learned)} 个",
                )
        except Exception as e:
            logger.warning(f"[botguard] 读取状态失败，按空状态启动：{e}")

    def _save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 30:
            return
        self._last_save = now
        try:
            # 只保留最近的命中记录，避免无限增长
            if len(self._hits) > 300:
                keep = sorted(
                    self._hits.items(),
                    key=lambda kv: kv[1].get("last", 0),
                    reverse=True,
                )[:300]
                self._hits = dict(keep)
            payload = {
                "learned": sorted(self._learned),
                "hits": self._hits,
                "updated": now,
            }
            self._state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
        except Exception as e:
            logger.warning(f"[botguard] 保存状态失败：{e}")

    # ------------------------------------------------------------ 身份识别
    def _bot_ids(self) -> set[str]:
        raw = str(self._conf("bot_ids", "") or "")
        ids = {t for t in re.split(r"[,，;；\s]+", raw) if t.strip().isdigit()}
        ids |= set(self._learned)
        return ids

    def _raw_sender(self, event: AstrMessageEvent) -> dict:
        """把不同适配器的 raw_message.sender 统一成 dict。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None:
            return {}
        sender = None
        if isinstance(raw, dict):
            sender = raw.get("sender")
        else:
            sender = getattr(raw, "sender", None)
        if isinstance(sender, dict):
            return sender
        if sender is None:
            return {}
        out = {}
        for key in ("user_id", "nickname", "card", "role", "is_robot", "isRobot"):
            value = getattr(sender, key, None)
            if value is not None:
                out[key] = value
        return out

    def _raw_is_robot(self, event: AstrMessageEvent) -> bool:
        sender = self._raw_sender(event)
        for key in ("is_robot", "isRobot", "is_bot", "isBot"):
            if sender.get(key) in (True, 1, "true", "True"):
                return True
        return False

    def _maybe_learn(self, event: AstrMessageEvent, session: str, sender: str) -> bool:
        """本 bot 发言后第一个接话的人，累计若干次即判定为 bot。"""
        if not bool(self._conf("auto_learn", True)):
            return sender in self._learned
        now = time.time()
        window = float(self._conf("auto_learn_window", 15.0))
        started = self._pending.get(session)
        if started is None:
            return sender in self._learned
        if now - started > window:
            self._pending.pop(session, None)
            return sender in self._learned
        # 一条 bot 发言只算第一个接话的人
        self._pending.pop(session, None)

        record = self._hits.get(sender)
        expire = float(self._conf("auto_learn_expire", 1800))
        if not record or now - float(record.get("last", 0)) > expire:
            record = {"hits": 1, "first": now, "last": now}
        else:
            record["hits"] = int(record.get("hits", 0)) + 1
            record["last"] = now
        self._hits[sender] = record

        threshold = int(self._conf("auto_learn_hits", 4))
        if record["hits"] >= threshold and sender not in self._learned:
            self._learned.add(sender)
            self._save_state(force=True)
            logger.info(
                f"[botguard] 自动识别到疑似 AI 账号 {sender}"
                f"（{record['hits']} 次秒回），已加入名单；误判可用 /botguard rm {sender} 移除",
            )
            return True
        return sender in self._learned

    # ------------------------------------------------------------ 状态管理
    def _reset_session(self, session: str) -> None:
        for key in [k for k in self._chain if k[0] == session]:
            self._chain.pop(key, None)

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        only = str(self._conf("only_groups", "") or "").strip()
        if not only:
            return True
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        if not group_id:  # 私聊不受群白名单限制
            return True
        allowed = {t for t in re.split(r"[,，;；\s]+", only) if t.strip()}
        return group_id in allowed

    def _inject_hint(self, req, text: str) -> None:
        if TextPart is not None:
            try:
                part = TextPart(text=text)
                try:
                    part = part.mark_as_temp()
                except Exception:
                    pass
                req.extra_user_content_parts.append(part)
                return
            except Exception:
                pass
        try:
            req.system_prompt = (req.system_prompt or "") + "\n" + text
        except Exception:
            pass

    # -------------------------------------------------------------- 主拦截
    @filter.on_llm_request(priority=99999)
    async def guard_llm_request(self, event: AstrMessageEvent, req):
        """在 LLM 请求真正发出前判断：这条消息是不是另一个 bot 发的。"""
        try:
            if not self._is_enabled():
                return
            # 同一条消息可能因为 agent 内部多轮而多次触发，只处理一次
            if event.get_extra("_botguard_done", False):
                return
            if not self._session_allowed(event):
                return
            event.set_extra("_botguard_done", True)

            sender = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if not sender or sender == self_id:
                return

            session = event.unified_msg_origin or ""
            now = time.time()

            # 管理员永不当成 bot，只用来重置链（避免误伤正在快速对话的管理员）
            if getattr(event, "role", "") == "admin":
                if bool(self._conf("reset_on_human", True)):
                    self._reset_session(session)
                return

            is_bot = sender in self._bot_ids() or self._raw_is_robot(event)
            if not is_bot:
                is_bot = self._maybe_learn(event, session, sender)

            if not is_bot:
                # 真人说话 = 一次对话结束，整条链清零
                if bool(self._conf("reset_on_human", True)):
                    self._reset_session(session)
                return

            # ---- 到这里确认是对面是 bot，开始限流 ----
            key = (session, sender)
            state = self._chain.get(key)
            if state is None:
                state = {"count": 0, "ts": 0.0, "until": 0.0}
                self._chain[key] = state

            if now < float(state.get("until", 0)):
                self._blocked += 1
                logger.info(
                    f"[botguard] 静默中，丢弃 {sender} 的消息"
                    f"（还剩 {int(state['until'] - now)} 秒）",
                )
                event.stop_event()
                return

            chain_window = float(self._conf("chain_window", 120.0))
            if now - float(state.get("ts", 0)) <= chain_window:
                state["count"] = int(state.get("count", 0)) + 1
            else:
                state["count"] = 1
            state["ts"] = now

            max_rounds = max(1, int(self._conf("max_rounds", 3)))
            if state["count"] > max_rounds:
                cooldown = float(self._conf("cooldown_seconds", 600))
                state["until"] = now + cooldown
                state["count"] = 0
                self._blocked += 1
                logger.info(
                    f"[botguard] 与 {sender} 已连续来回 {max_rounds} 轮，"
                    f"进入静默 {int(cooldown)} 秒（真人说话会立即解除）",
                )
                event.stop_event()
                if bool(self._conf("notify_on_block", False)):
                    try:
                        await event.send(
                            event.plain_result(str(self._conf("notify_text", ""))),
                        )
                    except Exception:
                        pass
                return

            logger.debug(
                f"[botguard] 第 {state['count']}/{max_rounds} 轮，放行并提醒模型收敛",
            )
            if bool(self._conf("inject_hint", True)):
                try:
                    hint = str(self._conf("hint_text", "")).format(
                        sender=sender,
                        round=state["count"],
                        max_rounds=max_rounds,
                    )
                except Exception:
                    hint = str(self._conf("hint_text", ""))
                self._inject_hint(req, hint)
        except Exception as e:  # 任何异常都不能影响正常聊天
            logger.error(f"[botguard] 拦截逻辑异常（已放行）：{e}")

    # ------------------------------------------------------ 发送侧断链
    @filter.on_decorating_result(priority=-99999)
    async def strip_bot_mentions(self, event: AstrMessageEvent):
        """回复里如果 @ 或引用了别的 bot，直接删掉，别把对方叫醒。"""
        try:
            if not self._is_enabled() or not bool(self._conf("strip_mention", True)):
                return
            result = event.get_result()
            if not result:
                return
            chain = getattr(result, "chain", None)
            if not chain:
                return

            ids = self._bot_ids()
            self_id = str(event.get_self_id() or "")
            kept = []
            removed = 0
            for seg in chain:
                if isinstance(seg, At):
                    target = str(getattr(seg, "qq", "") or "")
                    if target in ids and target != self_id:
                        removed += 1
                        continue
                if Reply is not None and isinstance(seg, Reply):
                    target = str(getattr(seg, "sender_id", "") or "")
                    if target in ids:
                        removed += 1
                        continue
                kept.append(seg)

            if removed == 0 or not kept:
                return
            # 删掉开头的 @ 后可能留下空格，顺手清掉
            first = kept[0]
            if isinstance(first, Plain) and getattr(first, "text", ""):
                first.text = first.text.lstrip()
                if not first.text:
                    kept.pop(0)
            if not kept:
                return
            try:
                chain[:] = kept
            except Exception:
                result.chain = kept
            logger.debug(f"[botguard] 已从回复中移除 {removed} 个指向 bot 的 @/引用")
        except Exception as e:
            logger.error(f"[botguard] 移除 @ 失败（不影响发送）：{e}")

    # ------------------------------------------------------ 学习窗口
    @filter.after_message_sent()
    async def mark_bot_spoke(self, event: AstrMessageEvent):
        """记下本 bot 刚刚发过言，后面第一个接话的人进学习统计。"""
        try:
            if not self._is_enabled() or not bool(self._conf("auto_learn", True)):
                return
            self._pending[event.unified_msg_origin or ""] = time.time()
            self._save_state()
        except Exception:
            pass

    # ---------------------------------------------------------- 指令
    def _parse_args(self, event: AstrMessageEvent) -> list[str]:
        raw = (event.message_str or "").strip()
        tokens = [t for t in re.split(r"\s+", raw) if t]
        if tokens and tokens[0].lstrip("/").lower() in {
            "botguard",
            "bg",
            "防ai",
            "防AI",
        }:
            tokens = tokens[1:]
        return tokens

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("botguard", alias={"防AI", "bg"})
    async def botguard_cmd(self, event: AstrMessageEvent):
        """查看/管理 BotGuard：/botguard [status|list|add QQ|rm QQ|reset|on|off]"""
        tokens = self._parse_args(event)
        action = (tokens[0].lower() if tokens else "status")
        arg = tokens[1] if len(tokens) > 1 else ""

        if action in ("status", "状态"):
            ids = sorted(self._bot_ids())
            lines = [
                f"BotGuard v{VERSION} — {'开启' if self._is_enabled() else '已暂停'}",
                f"名单（{len(ids)}）：{', '.join(ids) if ids else '空'}",
                f"自动学习：{'开' if self._conf('auto_learn', True) else '关'}"
                f"（{self._conf('auto_learn_hits', 4)} 次秒回判定）",
                f"限流：最多 {self._conf('max_rounds', 3)} 轮，"
                f"之后静默 {self._conf('cooldown_seconds', 600)} 秒",
                f"发送侧去 @：{'开' if self._conf('strip_mention', True) else '关'}",
                f"本次已拦截：{self._blocked} 条",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("list", "列表"):
            ids = sorted(self._bot_ids())
            yield event.plain_result(
                "当前名单：\n" + ("\n".join(f"- {i}" for i in ids) if ids else "（空）"),
            )
            return

        if action in ("add", "添加"):
            if not arg.isdigit():
                yield event.plain_result("用法：/botguard add QQ号")
                return
            self._learned.add(arg)
            self._save_state(force=True)
            yield event.plain_result(f"已加入名单：{arg}")
            return

        if action in ("rm", "del", "remove", "删除"):
            if not arg.isdigit():
                yield event.plain_result("用法：/botguard rm QQ号")
                return
            removed = arg in self._learned
            self._learned.discard(arg)
            self._hits.pop(arg, None)
            self._save_state(force=True)
            note = "" if removed else "（它不在自动学习名单里，若写在配置 bot_ids 中请去面板删除）"
            yield event.plain_result(f"已从自动学习名单移除：{arg}{note}")
            return

        if action in ("reset", "重置"):
            self._chain.clear()
            self._pending.clear()
            self._hits.clear()
            self._save_state(force=True)
            yield event.plain_result("已清空当前对话链与学习统计")
            return

        if action in ("on", "开"):
            self._runtime_enabled = True
            yield event.plain_result("BotGuard 已开启")
            return

        if action in ("off", "关"):
            self._runtime_enabled = False
            yield event.plain_result("BotGuard 已暂停（重启后恢复配置值）")
            return

        yield event.plain_result(
            "用法：/botguard [status|list|add QQ|rm QQ|reset|on|off]",
        )
