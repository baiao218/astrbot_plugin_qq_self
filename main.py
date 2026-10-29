"""自我形象 —— 让她自己换 QQ 头像、改个性签名。

NapCat 里有这三个扩展接口，本插件只用到前两个：

| OneBot 动作 | 作用 | 参数 |
|---|---|---|
| `set_qq_avatar` | 设置 QQ 头像 | `file`：本地路径 / URL / base64 |
| `set_self_longnick` | 设置个性签名 | `longNick` |
| `set_qq_profile` | 昵称 + 签名 + 性别 | `nickname` 必填，会改昵称，不用 |

**关于 QQ 零钱**

NapCat 是聊天客户端的钩子，不是支付客户端。整个包里没有钱包、余额、转账
相关的任何接口——支付要过财付通的密码与人脸校验，那不在协议层，也不该在。
所以「让她管理零钱」这件事没有技术路径，也不建议绕。要做账目管理只能另做
一本自己的账，而不是动真钱。

**为什么要限流**

QQ 对头像、签名的修改有频率风控，短时间反复改容易触发验证甚至临时限制。
所以默认头像 6 小时一次、签名 1 小时一次、每天最多 4 次。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_qq_self"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

ACTION_AVATAR = "set_qq_avatar"
ACTION_LONGNICK = "set_self_longnick"
ACTION_LOGIN_INFO = "get_login_info"


class QQSelfPlugin(Star):
    """让她自己决定长什么样。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.settings = config

        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)

        raw_dir = str(self.settings.get("avatar_dir") or "").strip()
        self.avatar_dir = Path(raw_dir) if raw_dir else (self.data_dir / "avatars")
        self.avatar_dir.mkdir(parents=True, exist_ok=True)

        self.state_path = self.data_dir / "state.json"
        self.state = self._load_state()

    # ---------------- 状态 ----------------

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f"[qq_self] 状态写入失败: {e}")

    # ---------------- 调用 OneBot ----------------

    async def _call(self, event: AstrMessageEvent, action: str, **params):
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            raise RuntimeError("当前平台不支持 OneBot 动作，本功能只在 aiocqhttp 下可用")
        ret = await bot.call_action(action, **params)
        if isinstance(ret, dict):
            code = ret.get("retcode")
            if ret.get("status") == "failed" or (code not in (None, 0)):
                raise RuntimeError(str(ret.get("wording") or ret.get("message") or ret))
        return ret

    async def _nickname(self, event: AstrMessageEvent) -> str:
        try:
            info = await self._call(event, ACTION_LOGIN_INFO)
            return str((info or {}).get("data", {}).get("nickname") or "")
        except Exception:
            return ""

    # ---------------- 图片解析 ----------------

    def _candidates(self) -> list[Path]:
        if not self.avatar_dir.exists():
            return []
        return sorted(
            [f for f in self.avatar_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXT],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

    def _resolve_image(self, raw: str) -> str:
        """把「她说的名字」变成一个 NapCat 能吃的路径 / URL / base64。"""
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("没有给图片")

        if raw.startswith(("http://", "https://", "base64://", "file://")):
            return raw

        p = Path(raw)
        if p.is_file():
            return str(p.resolve())

        files = self._candidates()
        if not files:
            raise FileNotFoundError(
                f"头像目录是空的：{self.avatar_dir}。往里面放几张图再用。"
            )

        key = raw.lower().strip()
        exact = [f for f in files if f.stem.lower() == key]
        if exact:
            return str(exact[0].resolve())

        sub = [f for f in files if key and key in f.stem.lower()]
        if len(sub) == 1:
            return str(sub[0].resolve())
        if sub:
            names = "、".join(f.stem for f in sub[:8])
            raise ValueError(f"「{key}」匹配到多张：{names}。说具体一点。")

        names = "、".join(f.stem for f in files[:12])
        raise ValueError(f"目录里没有「{key}」。可选：{names}")

    # ---------------- 节流 ----------------

    def _cool(self, key: str, minutes: int) -> tuple[bool, str]:
        last = float(self.state.get(key) or 0)
        gap = time.time() - last
        if minutes > 0 and gap < minutes * 60:
            left = int((minutes * 60 - gap) / 60) + 1
            return False, f"还要等约 {left} 分钟（QQ 对频繁修改有风控）"
        return True, ""

    def _today_key(self) -> str:
        return f"count_{datetime.now():%Y%m%d}"

    def _bump_today(self) -> int:
        k = self._today_key()
        n = int(self.state.get(k) or 0) + 1
        self.state[k] = n
        return n

    def _daily_left(self) -> int:
        cap = int(self.settings.get("daily_max") or 4)
        if cap <= 0:
            return 999
        return cap - int(self.state.get(self._today_key()) or 0)

    def _owner_ok(self, event: AstrMessageEvent) -> bool:
        if not bool(self.settings.get("owner_only", True)):
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        raw = str(self.settings.get("owner_umo") or "").strip()
        if raw:
            umos = {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}
            return (getattr(event, "unified_msg_origin", "") or "") in umos
        return False

    def _deny_reason(self, event: AstrMessageEvent) -> str:
        if self._owner_ok(event):
            return ""
        return (
            "换头像和改签名是账号级操作，只在她本人的会话里生效。"
            "这里不行——不要替任何人改自己的形象。"
        )

    # ---------------- 工具：她自己调用 ----------------

    @llm_tool(name="set_avatar")
    async def tool_set_avatar(self, event: AstrMessageEvent, image: str) -> str:
        """换掉自己的 QQ 头像。

        只在你自己想换的时候用，或者主人明确让你换。别人让你换，不要换。

        Args:
            image(string): 头像目录里的名字（可以只写关键词），或者一个图片 URL
        """
        if not bool(self.settings.get("enable", True)):
            return "换头像功能已关闭。"
        deny = self._deny_reason(event)
        if deny:
            return deny

        ok, why = self._cool(
            "last_avatar", int(self.settings.get("avatar_cooldown_minutes") or 360)
        )
        if not ok:
            return f"换不了。{why}。"
        if self._daily_left() <= 0:
            return "今天换的次数用完了。明天再说。"

        try:
            path = self._resolve_image(image)
        except (ValueError, FileNotFoundError) as e:
            return f"没找到那张图。{e}"

        try:
            await self._call(event, ACTION_AVATAR, file=path)
        except Exception as e:
            logger.error(f"[qq_self] 设置头像失败: {e}")
            return f"设置失败了：{e}"

        self.state["last_avatar"] = time.time()
        self.state["last_avatar_file"] = Path(path).name if "://" not in path else path[:80]
        self._bump_today()
        self._save_state()
        return "头像换好了。"

    @llm_tool(name="set_signature")
    async def tool_set_signature(self, event: AstrMessageEvent, text: str) -> str:
        """改自己的 QQ 个性签名。

        只在你自己想改的时候用，或者主人明确让你改。可以写一句此刻的心情。

        Args:
            text(string): 新的签名，40 字以内，不要带换行
        """
        if not bool(self.settings.get("enable", True)):
            return "改签名功能已关闭。"
        deny = self._deny_reason(event)
        if deny:
            return deny

        text = re.sub(r"\s+", " ", str(text or "")).strip()
        limit = int(self.settings.get("signature_max_chars") or 40)
        if not text:
            return "签名不能是空的。"
        if len(text) > limit:
            return f"太长了，{limit} 字以内。现在是 {len(text)} 字。"

        ok, why = self._cool(
            "last_signature", int(self.settings.get("signature_cooldown_minutes") or 60)
        )
        if not ok:
            return f"改不了。{why}。"
        if self._daily_left() <= 0:
            return "今天改的次数用完了。明天再说。"

        try:
            await self._call(event, ACTION_LONGNICK, longNick=text)
        except Exception as e:
            logger.error(f"[qq_self] 设置签名失败: {e}")
            return f"设置失败了：{e}"

        self.state["last_signature"] = time.time()
        self.state["last_signature_text"] = text
        self._bump_today()
        self._save_state()
        return "签名改好了。"

    @llm_tool(name="list_avatar_options")
    async def tool_list_avatars(self, event: AstrMessageEvent) -> str:
        """看看头像目录里有哪些图可以换。"""
        files = self._candidates()
        if not files:
            return f"目录是空的：{self.avatar_dir}"
        names = "、".join(f.stem for f in files[:20])
        more = "" if len(files) <= 20 else f" 等 {len(files)} 张"
        return f"可选：{names}{more}"

    # ---------------- 命令 ----------------

    @filter.command("换头像")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_avatar(self, event: AstrMessageEvent, image: str = ""):
        """手动换头像。"""
        if not image:
            files = self._candidates()
            if not files:
                yield event.plain_result(f"头像目录是空的：{self.avatar_dir}")
                return
            yield event.plain_result("可选：" + "、".join(f.stem for f in files[:20]))
            return
        try:
            path = self._resolve_image(image)
            await self._call(event, ACTION_AVATAR, file=path)
        except Exception as e:
            yield event.plain_result(f"失败：{e}")
            return
        self.state["last_avatar"] = time.time()
        self.state["last_avatar_file"] = Path(path).name if "://" not in path else path[:80]
        self._bump_today()
        self._save_state()
        yield event.plain_result("头像换好了。")

    @filter.command("签名")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_signature(self, event: AstrMessageEvent, text: str = ""):
        """手动改签名。"""
        if not text:
            yield event.plain_result("用法：/签名 今天读了三十页书")
            return
        text = re.sub(r"\s+", " ", text).strip()
        limit = int(self.settings.get("signature_max_chars") or 40)
        if len(text) > limit:
            yield event.plain_result(f"太长了，{limit} 字以内。")
            return
        try:
            await self._call(event, ACTION_LONGNICK, longNick=text)
        except Exception as e:
            yield event.plain_result(f"失败：{e}")
            return
        self.state["last_signature"] = time.time()
        self.state["last_signature_text"] = text
        self._bump_today()
        self._save_state()
        yield event.plain_result("签名改好了。")

    @filter.command("我的形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """看看当前形象与剩余次数。"""
        nick = await self._nickname(event)
        last_av = float(self.state.get("last_avatar") or 0)
        last_sig = float(self.state.get("last_signature") or 0)
        fmt = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "还没动过"

        lines = ["【自我形象】"]
        lines.append(f"账号昵称：{nick or '（取不到）'}")
        lines.append(f"上次换头像：{fmt(last_av)}" + (f"（{self.state.get('last_avatar_file')}）" if self.state.get("last_avatar_file") else ""))
        lines.append(f"上次改签名：{fmt(last_sig)}" + (f"（{self.state.get('last_signature_text')}）" if self.state.get("last_signature_text") else ""))
        lines.append(f"头像目录：{len(self._candidates())} 张 · {self.avatar_dir}")
        lines.append(f"今天还能改：{max(0, self._daily_left())} 次")
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        self._save_state()
