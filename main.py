import asyncio
import json
import os
import random
import re
import tempfile
import time
from typing import Dict, List, Any, Optional, Set, Tuple

import httpx
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, StarTools, register

# 测试用户的常量
TEST_USER_STEAM_ID = "70000000000000001"
TEST_USER_INITIAL_STATE = {
    "personaname": "测试ID",
    "personastate": 1,
    "gameid": "774241",  # Cyberpunk 2077 App ID
    "gameextrainfo": "Cyberpunk 2077",
}

STEAM_ID_PREFIX_PATTERN = re.compile(r"\s*(\d{17})(?!\d)")
STEAM_ID_FALLBACK_PATTERN = re.compile(r"(?<!\d)(\d{17})(?!\d)")
REMARK_FALLBACK_PATTERN = re.compile(r"（([^（）]*)）")
LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{17}(?!\d)")


@register(
    "steam_monitor",
    "Singularity2000",
    "一个简单但强大的 Steam 游戏状态监控插件，用于推送游戏开始/结束和成就获得通知。",
    "1.1.0",
    "https://github.com/Singularity2000/astrbot_plugin_steam_monitor",
)
class SteamMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_steam_monitor")

        # 数据文件路径
        self.last_states_path = os.path.join(self.data_dir, "last_states.json")
        self.last_achievements_path = os.path.join(
            self.data_dir, "last_achievements.json"
        )
        self.last_playtimes_path = os.path.join(self.data_dir, "last_playtimes.json")
        self.game_cache_path = os.path.join(self.data_dir, "game_cache.json")
        self.achievement_schema_path = os.path.join(
            self.data_dir, "achievement_schema.json"
        )
        self.player_profiles_path = os.path.join(
            self.data_dir, "player_profiles.json"
        )
        self.id_remarks_path = os.path.join(self.data_dir, "id_remarks.json")

        # 加载数据。备注状态需要在解析配置前加载，用于识别用户手动修改。
        self.last_states: Dict[str, Dict[str, Any]] = self._load_data(
            self.last_states_path
        )
        self.last_achievements: Dict[str, Dict[str, List[str]]] = self._load_data(
            self.last_achievements_path
        )
        self.last_playtimes: Dict[str, Dict[str, int]] = self._load_data(
            self.last_playtimes_path
        )
        self.game_cache: Dict[str, str] = self._load_data(self.game_cache_path)
        self.achievement_schema: Dict[str, Dict[str, Any]] = self._load_data(
            self.achievement_schema_path
        )
        self.player_profiles: Dict[str, Dict[str, Any]] = self._load_data(
            self.player_profiles_path
        )
        self.id_remarks: Dict[str, Dict[str, Any]] = self._load_data(
            self.id_remarks_path
        )

        # 加载配置
        self._config_dirty = False
        self._remarks_dirty = False
        self._load_config()
        bot_config = self.context.get_config()
        self.admins: List[str] = [
            str(admin) for admin in bot_config.get("admins_id", [])
        ]
        self._sync_remarks_from_profiles()

        # --- 为测试用户设置独立的模拟状态 ---
        self.test_user_mock_state: Dict[str, Any] = TEST_USER_INITIAL_STATE.copy()
        # 初始化测试用户的成就，确保键存在
        self.test_user_mock_achievements: Dict[str, List[str]] = {
            TEST_USER_INITIAL_STATE["gameid"]: []
        }
        # --- 模拟状态结束 ---

        # 初始化测试用户状态
        if TEST_USER_STEAM_ID not in self.last_states:
            self.last_states[TEST_USER_STEAM_ID] = TEST_USER_INITIAL_STATE.copy()
        if TEST_USER_STEAM_ID not in self.last_achievements:
            self.last_achievements[TEST_USER_STEAM_ID] = {
                TEST_USER_INITIAL_STATE["gameid"]: []
            }

        self.is_first_status_run = True
        self.is_first_achievement_run = True
        self.status_monitor_task: Optional[asyncio.Task] = None
        self.achievement_monitor_task: Optional[asyncio.Task] = None
        self.test_reset_task: Optional[asyncio.Task] = None
        self._test_monitor_group: Optional[Dict] = None
        self._last_status_success_at: Optional[float] = None
        self._last_status_failure_at: Optional[float] = None
        self._last_status_error: Optional[str] = None
        self._last_achievement_success_at: Optional[float] = None
        self._last_achievement_failure_at: Optional[float] = None
        self._last_achievement_error: Optional[str] = None
        self._last_api_error: Optional[str] = None
        self._last_api_error_at: Optional[float] = None
        self._last_api_success_at: Optional[float] = None
        self._remark_lock = asyncio.Lock()
        self.achievement_semaphore = asyncio.Semaphore(4)

        # 极致优化：商店API专用信号量与请求合并字典
        self.store_api_semaphore = asyncio.Semaphore(
            2
        )  # 限制同时查询游戏名的并发数，防止商店API封IP
        self._pending_game_tasks: Dict[
            str, asyncio.Task
        ] = {}  # 用于请求合并（惊群效应保护）

        # 初始化共享的 HTTP 客户端，复用连接池以减少 SSL 握手开销
        self.http_client = httpx.AsyncClient(timeout=20)

    async def initialize(self) -> None:
        """启动监控循环。"""
        await self._save_remark_changes()
        if self.api_key:
            self.status_monitor_task = asyncio.create_task(
                self.status_monitoring_loop()
            )
            # 错开启动，避免两个循环同时发起首批请求
            if self.achievement_poll_interval > 0:
                self.achievement_monitor_task = asyncio.create_task(
                    self.achievement_monitoring_loop(delay_seconds=5)
                )
            else:
                logger.info("成就检查间隔设置为 0，已关闭成就监控循环。")
        else:
            logger.warning("Steam API Key 未配置，插件不会启动。")

    def _load_config(self):
        """从配置对象加载或重载配置"""
        self.api_key: str = self.config.get("steam_api_key", "")
        self.admin_only_sensitive_operations: bool = self.config.get(
            "admin_only_sensitive_operations", True
        )
        self.status_poll_interval: int = self.config.get("status_poll_interval", 180)
        self.achievement_poll_interval: int = self.config.get(
            "achievement_poll_interval", 1800
        )
        self.retry_times: int = self.config.get("retry_times", 3)
        self.detailed_log: bool = self.config.get("detailed_poll_log", False)
        self.log_masking: bool = self.config.get("log_masking", True)

        # 全局通知设置
        self.global_status_notification: bool = self.config.get(
            "status_notification", True
        )
        self.global_online_offline: bool = self.config.get(
            "online_offline_notification", False
        )
        self.global_achievements: bool = self.config.get(
            "achievements_notification", True
        )
        self.global_playtime_notification: bool = self.config.get(
            "playtime_notification", True
        )
        self.private_mode: bool = self.config.get("private_mode", False)
        self.private_name: str = self.config.get("private_name", "")

        # template_list 类型直接返回 List[Dict]，无需 JSON 解析
        raw_targets = self.config.get("monitored_targets", [])
        if isinstance(raw_targets, list):
            self.monitored_groups: List[Dict] = raw_targets
        else:
            self.monitored_groups = []
            logger.error("'monitored_targets' 配置格式异常，期望列表类型。")
        self._migrate_group_settings()
        self._prepare_remark_states()

    def _migrate_group_settings(self) -> None:
        """把旧版“自定义开关 + 通知开关”迁移为三态设置。"""
        mappings = (
            ("status_notification_mode", "use_custom_status", "status_notification", True),
            (
                "online_offline_notification_mode",
                "use_custom_online_offline",
                "online_offline_notification",
                False,
            ),
            (
                "achievements_notification_mode",
                "use_custom_achievements",
                "achievements_notification",
                True,
            ),
            (
                "playtime_notification_mode",
                "use_custom_playtime",
                "playtime_notification",
                True,
            ),
            ("privacy_mode_setting", "use_custom_private", "private_mode", False),
        )

        for group in self.monitored_groups:
            if not isinstance(group, dict):
                continue
            settings = group.get("settings")
            if not isinstance(settings, dict):
                continue

            changed = False
            for mode_key, old_use_key, old_value_key, old_default in mappings:
                mode = settings.get(mode_key)
                if mode not in ("inherit", "on", "off"):
                    if settings.get(old_use_key, False):
                        mode = (
                            "on"
                            if settings.get(old_value_key, old_default)
                            else "off"
                        )
                    else:
                        mode = "inherit"
                    settings[mode_key] = mode
                    changed = True

                for old_key in (old_use_key, old_value_key):
                    if old_key in settings:
                        settings.pop(old_key)
                        changed = True

            if changed:
                self._config_dirty = True

    @staticmethod
    def _default_group_settings() -> Dict[str, str]:
        return {
            "status_notification_mode": "inherit",
            "online_offline_notification_mode": "inherit",
            "achievements_notification_mode": "inherit",
            "playtime_notification_mode": "inherit",
            "privacy_mode_setting": "inherit",
        }

    def _mask_text(self, value: Any) -> str:
        """返回可安全写入日志的文本。"""
        text = str(value)
        if not self.log_masking:
            return text
        if self.api_key:
            text = text.replace(self.api_key, "***")
        text = re.sub(r"(?i)(key=)[^&\s]+", r"\1***", text)
        text = LONG_NUMBER_PATTERN.sub("***", text)
        return text

    def _log_warning(self, message: Any) -> None:
        logger.warning(self._mask_text(message))

    def _log_error(self, message: Any) -> None:
        logger.error(self._mask_text(message))

    @staticmethod
    def _parse_steam_id_entry(raw: Any) -> Tuple[Optional[str], Optional[str], bool]:
        """解析 ID（备注）。备注为 None 表示没有括号，空字符串表示“自动获取”。"""
        if not isinstance(raw, str):
            return None, None, False
        text = raw.strip()
        prefix_match = STEAM_ID_PREFIX_PATTERN.match(text)
        if prefix_match:
            rest = text[prefix_match.end() :].strip()
            if not rest:
                return prefix_match.group(1), None, True
            if rest.startswith("（") and rest.endswith("）") and len(rest) >= 2:
                return prefix_match.group(1), rest[1:-1].strip(), True

        id_match = STEAM_ID_FALLBACK_PATTERN.search(text)
        if not id_match:
            return None, None, False

        remark_match = REMARK_FALLBACK_PATTERN.search(text, id_match.end())
        remark = remark_match.group(1).strip() if remark_match else None
        return id_match.group(1), remark, False

    @staticmethod
    def _sanitize_remark(value: Any) -> str:
        """昵称备注需要避免破坏中文括号语法。"""
        remark = str(value or "").strip()
        remark = remark.replace("（", "(").replace("）", ")")
        remark = re.sub(r"[\r\n\t]+", " ", remark)
        return remark.strip()

    def _iter_steam_id_entries(self):
        for group in self.monitored_groups:
            if not isinstance(group, dict):
                continue
            raw_ids = group.get("steam_ids")
            if not isinstance(raw_ids, list):
                continue
            for index, raw in enumerate(raw_ids):
                yield group, index, raw, self._parse_steam_id_entry(raw)

    def _prepare_remark_states(self) -> None:
        """根据本次配置和上次备注状态，识别手动修改、自动获取和关闭备注。"""
        # 先清理旧版本误写入的内置测试用户，避免边遍历边修改列表。
        for group in self.monitored_groups:
            if not isinstance(group, dict):
                continue
            raw_ids = group.get("steam_ids")
            if not isinstance(raw_ids, list):
                continue
            filtered_ids = [
                raw
                for raw in raw_ids
                if self._parse_steam_id_entry(raw)[0] != TEST_USER_STEAM_ID
            ]
            if len(filtered_ids) != len(raw_ids):
                group["steam_ids"] = filtered_ids
                self._config_dirty = True
                self._log_warning("检测到内置测试用户已写入配置，已自动移除。")

        current_remarks: Dict[str, List[Optional[str]]] = {}
        current_ids: Set[str] = set()
        invalid_entries = 0

        for group, index, raw, (steam_id, remark, exact) in self._iter_steam_id_entries():
            if not steam_id:
                invalid_entries += 1
                self._log_warning(f"监控组里的 Steam ID 格式无效，已忽略：{raw}")
                continue
            if not exact:
                invalid_entries += 1
                self._log_warning(
                    f"Steam ID 条目格式不标准，将保底识别其中的 ID：{raw}"
                )
            current_ids.add(steam_id)
            current_remarks.setdefault(steam_id, []).append(remark)

        for steam_id in list(self.id_remarks):
            if steam_id not in current_ids:
                self.id_remarks.pop(steam_id)
                self._remarks_dirty = True

        for steam_id, remarks in current_remarks.items():
            new_state = self._choose_remark_state(remarks, self.id_remarks.get(steam_id))
            if self.id_remarks.get(steam_id) != new_state:
                self.id_remarks[steam_id] = new_state
                self._remarks_dirty = True

        if invalid_entries:
            self._log_warning(
                f"共有 {invalid_entries} 个 Steam ID 条目格式不标准，请在 WebUI 中检查。"
            )

    @staticmethod
    def _choose_remark_state(
        remarks: List[Optional[str]], old_state: Optional[Dict[str, str]]
    ) -> Dict[str, str]:
        non_empty = [remark for remark in remarks if remark]
        has_empty = any(remark == "" for remark in remarks)

        if old_state is None:
            if non_empty:
                return {
                    "source": "manual",
                    "value": SteamMonitor._sanitize_remark(non_empty[0]),
                }
            return {"source": "pending", "marker": False}

        old_value = old_state.get("value", "")
        changed = [remark for remark in non_empty if remark != old_value]
        if changed:
            return {
                "source": "manual",
                "value": SteamMonitor._sanitize_remark(changed[0]),
            }
        if has_empty:
            return {"source": "pending", "marker": True}
        if non_empty:
            if any(remark is None for remark in remarks):
                return {"source": "disabled"}
            return {
                "source": old_state.get("source", "auto"),
                "value": SteamMonitor._sanitize_remark(old_value),
            }
        if old_state.get("source") == "pending" and not old_state.get("marker"):
            return {"source": "pending", "marker": False}
        return {"source": "disabled"}

    def _get_cached_player_name(self, steam_id: str) -> Optional[str]:
        profile = self.player_profiles.get(steam_id)
        if isinstance(profile, dict):
            name = profile.get("personaname") or profile.get("name")
        else:
            name = profile
        name = str(name or "").strip()
        return name or None

    def _sync_remarks_from_profiles(self) -> None:
        """用已缓存的昵称补全待获取备注，并同步所有相同 ID。"""
        for steam_id, state in self.id_remarks.items():
            cached_name = self._get_cached_player_name(steam_id)
            if not cached_name:
                continue
            if state.get("source") in ("pending", "auto"):
                sanitized = self._sanitize_remark(cached_name)
                if state.get("source") != "auto" or state.get("value") != sanitized:
                    self.id_remarks[steam_id] = {
                        "source": "auto",
                        "value": sanitized,
                    }
                    self._remarks_dirty = True

        for group, index, raw, (steam_id, _, _) in self._iter_steam_id_entries():
            state = self.id_remarks.get(steam_id)
            if not state:
                continue
            source = state.get("source")
            if source in ("manual", "auto") and state.get("value"):
                formatted = f"{steam_id}（{state['value']}）"
            elif source == "disabled":
                formatted = steam_id
            elif source == "pending" and state.get("marker"):
                formatted = f"{steam_id}（）"
            else:
                # 没有昵称时不把纯 ID 改写成空括号，避免网络失败时配置被无意义改写。
                continue
            if raw != formatted:
                group["steam_ids"][index] = formatted
                self._config_dirty = True

    async def _save_plugin_config(self) -> None:
        if hasattr(self.config, "save_config_async"):
            await self.config.save_config_async()
        else:
            await asyncio.to_thread(self.config.save_config)

    async def _save_remark_changes(self) -> None:
        async with self._remark_lock:
            await self._save_remark_changes_locked()

    async def _save_remark_changes_locked(self) -> None:
        if self._config_dirty:
            self.config["monitored_targets"] = self.monitored_groups
            await self._save_plugin_config()
            self._config_dirty = False
        if self._remarks_dirty:
            await self._save_data(self.id_remarks_path, self.id_remarks)
            self._remarks_dirty = False

    def _load_data(self, path: str) -> Dict:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"加载数据失败 {path}: {e}")
        return {}

    async def _save_data(self, path: str, data: Any):
        try:
            await asyncio.to_thread(self._write_json_sync, path, data)
        except Exception as e:
            logger.error(f"保存数据失败 {path}: {e}")

    def _write_json_sync(self, path: str, data: Any):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        directory = os.path.dirname(os.path.abspath(path))
        basename = os.path.basename(path)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{basename}.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _effective_monitor_groups(self) -> List[Dict]:
        groups = [group for group in self.monitored_groups if isinstance(group, dict)]
        if self._test_monitor_group:
            groups.append(self._test_monitor_group)
        return groups

    def _get_group_steam_ids(self, group: Dict) -> List[str]:
        raw_ids = group.get("steam_ids", []) if isinstance(group, dict) else []
        if not isinstance(raw_ids, list):
            return []
        steam_ids = []
        for raw in raw_ids:
            steam_id, _, _ = self._parse_steam_id_entry(raw)
            if steam_id and steam_id not in steam_ids:
                steam_ids.append(steam_id)
        return steam_ids

    def _get_all_steam_ids(self) -> Set[str]:
        """从监控组列表中提取所有唯一的Steam ID"""
        return {
            steam_id
            for group in self._effective_monitor_groups()
            for steam_id in self._get_group_steam_ids(group)
        }

    def _get_group_settings(self, group: Dict) -> Tuple[bool, bool, bool, bool, bool]:
        """获取指定监控组的通知设置，inherit 时回退到全局设置"""
        settings = group.get("settings") if isinstance(group, dict) else None

        if settings and isinstance(settings, dict):
            status_enabled = self._resolve_tri_state(
                settings,
                "status_notification_mode",
                self.global_status_notification,
            )
            online_offline_enabled = status_enabled and self._resolve_tri_state(
                settings,
                "online_offline_notification_mode",
                self.global_online_offline,
            )
            achievement_enabled = self._resolve_tri_state(
                settings,
                "achievements_notification_mode",
                self.global_achievements,
            )
            playtime_enabled = self._resolve_tri_state(
                settings,
                "playtime_notification_mode",
                self.global_playtime_notification,
            )
            private_mode_enabled = self._resolve_tri_state(
                settings,
                "privacy_mode_setting",
                self.private_mode,
            )
            return (
                status_enabled,
                online_offline_enabled,
                achievement_enabled,
                private_mode_enabled,
                playtime_enabled,
            )

        # 回退到全局
        status_enabled = self.global_status_notification
        online_offline_enabled = status_enabled and self.global_online_offline
        achievement_enabled = self.global_achievements
        playtime_enabled = self.global_playtime_notification
        private_mode_enabled = self.private_mode
        return (
            status_enabled,
            online_offline_enabled,
            achievement_enabled,
            private_mode_enabled,
            playtime_enabled,
        )

    @staticmethod
    def _resolve_tri_state(
        settings: Dict, key: str, global_enabled: bool
    ) -> bool:
        mode = settings.get(key, "inherit")
        if mode == "on":
            return True
        if mode == "off":
            return False
        return global_enabled

    async def _make_request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        ignore_errors: bool = False,
    ) -> Optional[Dict]:
        """发起HTTP请求。4xx 致命错误不重试，限流和瞬时错误使用退避重试。"""
        attempts = max(1, self.retry_times)
        endpoint = url.split("?", 1)[0]
        for attempt in range(1, attempts + 1):
            try:
                # 方案一：如果客户端已关闭，主动重建
                if self.http_client.is_closed:
                    self._log_warning("HTTP客户端已关闭，正在重建...")
                    self.http_client = httpx.AsyncClient(timeout=20)

                resp = await self.http_client.get(url, params=params)
                resp.raise_for_status()
                result = resp.json()
                self._last_api_error = None
                self._last_api_error_at = None
                self._last_api_success_at = time.time()
                return result
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                reason = f"HTTP {status_code}"
                retryable = status_code in (408, 425, 429) or status_code >= 500
                retry_after = e.response.headers.get("Retry-After")
            except (httpx.RequestError, ValueError, RuntimeError) as e:
                if isinstance(e, RuntimeError) and "client has been closed" in str(e):
                    self._log_warning(
                        f"请求时发现客户端已关闭，尝试重建 (第 {attempt} 次)"
                    )
                    self.http_client = httpx.AsyncClient(timeout=20)
                    continue

                reason = str(e)
                retryable = True
                retry_after = None
            self._last_api_error = self._mask_text(reason)
            self._last_api_error_at = time.time()
            if not retryable:
                if not ignore_errors:
                    self._log_warning(f"请求失败且不可重试：{reason}")
                return None

            if not ignore_errors:
                self._log_warning(
                    f"请求失败（第 {attempt}/{attempts} 次）：{reason}"
                )
            if attempt >= attempts:
                if not ignore_errors:
                    self._log_error(f"请求失败，已达最大重试次数：{endpoint}")
                return None

            base_delay = min(30.0, 2.0 ** (attempt - 1))
            delay = base_delay + random.uniform(0.0, min(2.0, base_delay * 0.2))
            if retry_after:
                try:
                    delay = max(delay, min(120.0, float(retry_after)))
                except ValueError:
                    pass
            await asyncio.sleep(delay)

    async def _update_player_profiles(self, players: List[Dict]) -> None:
        """只在拿到有效昵称时更新缓存，避免网络波动清空资料。"""
        changed = False
        now = time.time()
        for player in players:
            steam_id = str(player.get("steamid", ""))
            name = str(player.get("personaname") or "").strip()
            if not steam_id or not name:
                continue
            old_profile = self.player_profiles.get(steam_id)
            old_name = (
                old_profile.get("personaname")
                if isinstance(old_profile, dict)
                else old_profile
            )
            if old_name != name or not isinstance(old_profile, dict):
                self.player_profiles[steam_id] = {
                    "personaname": name,
                    "updated_at": now,
                }
                changed = True

        if changed:
            await self._save_data(self.player_profiles_path, self.player_profiles)
            async with self._remark_lock:
                self._sync_remarks_from_profiles()
                await self._save_remark_changes_locked()

    def _get_display_name(
        self, steam_id: str, fresh_name: Optional[str] = None, for_log: bool = False
    ) -> str:
        name = str(fresh_name or "").strip() or self._get_cached_player_name(steam_id)
        if name:
            return name
        if for_log and not self.log_masking:
            return f"昵称未知（{steam_id}）"
        return "昵称未知"

    def _get_log_display_name(self, steam_id: str, fresh_name: Optional[str] = None) -> str:
        return self._get_display_name(steam_id, fresh_name, for_log=True)

    # --- API 调用封装 ---
    async def get_player_summaries(self, steam_ids: List[str]) -> Optional[List[Dict]]:
        if not steam_ids:
            return None

        # 命令层也可能传入带备注的字符串，这里统一保底解析。
        real_steam_ids = []
        for raw in steam_ids:
            parsed_id, _, _ = self._parse_steam_id_entry(raw)
            if parsed_id and parsed_id not in real_steam_ids:
                real_steam_ids.append(parsed_id)

        mock_player_data = []
        if TEST_USER_STEAM_ID in real_steam_ids:
            real_steam_ids.remove(TEST_USER_STEAM_ID)
            mock_player_data.append(
                {
                    "steamid": TEST_USER_STEAM_ID,
                    "personaname": self.test_user_mock_state.get(
                        "personaname", "测试ID"
                    ),
                    "personastate": self.test_user_mock_state.get("personastate", 1),
                    "gameid": self.test_user_mock_state.get("gameid"),
                    "gameextrainfo": self.test_user_mock_state.get("gameextrainfo"),
                }
            )

        if not real_steam_ids:
            return mock_player_data
        if not self.api_key:
            return None

        players: List[Dict] = []
        failed_batches = 0
        batch_count = (len(real_steam_ids) + 99) // 100
        for batch_start in range(0, len(real_steam_ids), 100):
            batch = real_steam_ids[batch_start : batch_start + 100]
            data = await self._make_request(
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                params={"key": self.api_key, "steamids": ",".join(batch)},
            )
            if data is None:
                failed_batches += 1
                continue
            response_players = data.get("response", {}).get("players", [])
            if isinstance(response_players, list):
                players.extend(response_players)

        if failed_batches == batch_count:
            return None

        await self._update_player_profiles(players)
        return players + mock_player_data

    async def get_game_name(self, app_id: str) -> str:
        app_id = str(app_id)
        if app_id in self.game_cache:
            return self.game_cache[app_id]

        # 请求合并：如果已有任务在查询该ID，直接等待其结果，不重复发起请求
        if app_id in self._pending_game_tasks:
            return await self._pending_game_tasks[app_id]

        # 创建新任务并记录
        task = asyncio.create_task(self._fetch_game_name_internal(app_id))
        self._pending_game_tasks[app_id] = task

        try:
            return await task
        finally:
            # 任务结束后清理记录
            self._pending_game_tasks.pop(app_id, None)

    async def _fetch_game_name_internal(self, app_id: str) -> str:
        """内部方法：受信号量控制的实际网络请求"""
        async with self.store_api_semaphore:
            # 并发请求中英文名称
            store_url = "https://store.steampowered.com/api/appdetails"

            task_zh = self._make_request(
                store_url,
                params={"appids": app_id, "l": "schinese"},
                ignore_errors=True,
            )
            task_en = self._make_request(
                store_url,
                params={"appids": app_id, "l": "english"},
                ignore_errors=True,
            )
            data_zh, data_en = await asyncio.gather(task_zh, task_en)

            name_zh = None
            if data_zh and app_id in data_zh and data_zh[app_id].get("success"):
                name_zh = data_zh[app_id]["data"]["name"]

            name_en = None
            if data_en and app_id in data_en and data_en[app_id].get("success"):
                name_en = data_en[app_id]["data"]["name"]

            final_name = f"未知游戏({app_id})"
            if name_zh and name_en and name_zh != name_en:
                final_name = f"{name_zh} ({name_en})"
            elif name_zh:
                final_name = name_zh
            elif name_en:
                final_name = name_en

            if name_zh or name_en:
                self.game_cache[app_id] = final_name
                await self._save_data(self.game_cache_path, self.game_cache)

            return final_name

    async def get_recently_played_games(self, steam_id: str) -> Optional[List[Dict]]:
        data = await self._make_request(
            "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/",
            params={"key": self.api_key, "steamid": steam_id},
            ignore_errors=True,
        )
        return (
            data.get("response", {}).get("games")
            if data and data.get("response", {}).get("total_count", 0) > 0
            else None
        )

    async def get_player_achievements(
        self, steam_id: str, app_id: str
    ) -> Optional[List[Dict]]:
        # --- 模拟测试用户 ---
        if steam_id == TEST_USER_STEAM_ID:
            # 从专用的模拟成就变量中读取
            achieved_apis = self.test_user_mock_achievements.get(app_id, [])
            return [{"apiname": api_name, "achieved": 1} for api_name in achieved_apis]
        # --- 模拟结束 ---

        data = await self._make_request(
            "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/",
            params={
                "key": self.api_key,
                "steamid": steam_id,
                "appid": app_id,
                "l": "schinese",
            },
            ignore_errors=True,
        )
        if data and data.get("playerstats", {}).get("success"):
            return data["playerstats"].get("achievements", [])
        return None

    async def get_achievement_schema(self, app_id: str) -> Optional[Dict[str, Any]]:
        if app_id in self.achievement_schema:
            return self.achievement_schema[app_id]

        data = await self._make_request(
            "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/",
            params={"key": self.api_key, "appid": app_id, "l": "schinese"},
            ignore_errors=True,
        )
        if data and "game" in data and "availableGameStats" in data["game"]:
            schema = {
                ach["name"]: ach
                for ach in data["game"]["availableGameStats"]["achievements"]
            }
            self.achievement_schema[app_id] = schema
            await self._save_data(self.achievement_schema_path, self.achievement_schema)
            return schema
        return None

    # --- 监控循环 ---
    async def status_monitoring_loop(self):
        """独立的在线/游戏状态监控循环"""
        while True:
            try:
                if self.detailed_log:
                    logger.info("开始新一轮在线、游戏状态检查...")

                all_steam_ids = self._get_all_steam_ids()
                if not all_steam_ids:
                    await asyncio.sleep(self.status_poll_interval)
                    continue

                steam_id_to_umos = self._build_reverse_map()
                players = await self.get_player_summaries(list(all_steam_ids))

                if players is None:
                    self._last_status_failure_at = time.time()
                    self._last_status_error = self._last_api_error or "请求失败"
                    self._log_warning("无法从Steam API获取玩家信息，跳过本轮状态检查。")
                    await asyncio.sleep(self.status_poll_interval)
                    continue

                if not players:
                    self._log_warning("Steam API 本轮未返回任何玩家信息，保留旧状态。")
                    await asyncio.sleep(self.status_poll_interval)
                    continue

                # 以旧状态为基底：部分用户获取失败时，不能把他们的状态和昵称丢掉。
                current_states = {
                    steam_id: dict(self.last_states[steam_id])
                    for steam_id in all_steam_ids
                    if steam_id in self.last_states
                }
                for player in players:
                    steam_id = str(player["steamid"])
                    fresh_name = str(player.get("personaname") or "").strip()
                    player_name = fresh_name or self._get_cached_player_name(steam_id)
                    current_state = {
                        "personaname": player_name or "",
                        "personastate": player.get("personastate", 0),
                        "gameid": player.get("gameid"),
                        "gameextrainfo": player.get("gameextrainfo"),
                    }

                    # 尝试统一游戏名为双语名（利用缓存，避免重复网络请求）
                    if current_state["gameid"]:
                        cached_name = self.game_cache.get(str(current_state["gameid"]))
                        if cached_name:
                            current_state["gameextrainfo"] = cached_name

                    last_state = self.last_states.get(steam_id, {})

                    if self.detailed_log:
                        log_name = self._get_log_display_name(steam_id, fresh_name)
                        log_msg = (
                            f"{log_name}({steam_id}): "
                            f"当前状态 {current_state['personastate']}, 游戏ID {current_state.get('gameid')} | "
                            f"上次状态 {last_state.get('personastate')}, 游戏ID {last_state.get('gameid')}"
                        )
                        logger.info(self._mask_text(log_msg))

                    # 首次运行不推送
                    if not self.is_first_status_run and last_state:
                        await self._check_and_notify_status_change(
                            steam_id, last_state, current_state, steam_id_to_umos
                        )

                    current_states[steam_id] = current_state

                if self.last_states != current_states:
                    self.last_states = current_states
                    await self._save_data(self.last_states_path, self.last_states)

                if self.is_first_status_run:
                    self.is_first_status_run = False

                self._last_status_success_at = time.time()
                self._last_status_error = None
                logger.info(
                    f"本轮在线、游戏状态检查成功，等待 {self.status_poll_interval} 秒。"
                )

            except Exception as e:
                self._last_status_failure_at = time.time()
                self._last_status_error = self._mask_text(e)
                self._log_error(f"状态监控循环发生未捕获的异常: {e}")

            await asyncio.sleep(self.status_poll_interval)

    async def achievement_monitoring_loop(self, delay_seconds: int = 0):
        """独立的成就监控循环（并行优化版）"""
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        while True:
            try:
                if self.detailed_log:
                    logger.info("开始新一轮成就对比循环…")

                all_steam_ids = self._get_all_steam_ids()
                if not all_steam_ids:
                    await asyncio.sleep(self.achievement_poll_interval)
                    continue

                steam_id_to_umos = self._build_reverse_map()

                # 创建并发任务
                tasks = [
                    self._check_achievements_for_user(steam_id, steam_id_to_umos)
                    for steam_id in all_steam_ids
                ]
                results = await asyncio.gather(*tasks)

                # 处理结果并更新状态
                data_changed = False
                playtime_data_changed = False
                # (umo, group_obj_id) -> {steam_id -> [msg_lines]}
                target_playtime_msgs: Dict[Tuple[str, int], Dict[str, List[str]]] = {}
                # (umo, group_obj_id) -> group
                target_group_map: Dict[Tuple[str, int], Dict] = {}

                for result_data in results:
                    if result_data:
                        steam_id, user_achievements, playtime_updates = result_data
                        if steam_id not in self.last_achievements:
                            self.last_achievements[steam_id] = {}

                        for app_id, new_achs in user_achievements.items():
                            old_achs = self.last_achievements[steam_id].get(app_id, [])
                            if set(new_achs) != set(old_achs):
                                self.last_achievements[steam_id][app_id] = new_achs
                                data_changed = True

                        # 处理游戏时长更新
                        if playtime_updates:
                            if steam_id not in self.last_playtimes:
                                self.last_playtimes[steam_id] = {}

                            for app_id, info in playtime_updates.items():
                                self.last_playtimes[steam_id][app_id] = info["current"]
                                playtime_data_changed = True

                                # 准备推送消息 (仅当不是首次运行且有差异时)
                                if (
                                    not self.is_first_achievement_run
                                    and info["diff"] > 0
                                ):
                                    targets = steam_id_to_umos.get(steam_id, [])
                                    for umo, group in targets:
                                        _, _, _, _, playtime_enabled = (
                                            self._get_group_settings(group)
                                        )
                                        if playtime_enabled:
                                            key = (umo, id(group))
                                            target_group_map[key] = group
                                            if key not in target_playtime_msgs:
                                                target_playtime_msgs[key] = {}
                                            if (
                                                steam_id
                                                not in target_playtime_msgs[key]
                                            ):
                                                target_playtime_msgs[key][steam_id] = []

                                            target_playtime_msgs[key][steam_id].append(
                                                f"  - {info['name']} {info['diff']}分钟"
                                            )

                if data_changed:
                    await self._save_data(
                        self.last_achievements_path, self.last_achievements
                    )

                if playtime_data_changed:
                    await self._save_data(self.last_playtimes_path, self.last_playtimes)

                # 发送聚合的游戏时长通知
                for (umo, _gid), steam_data in target_playtime_msgs.items():
                    group = target_group_map[(umo, _gid)]
                    msg_lines = []
                    for steam_id, game_lines in steam_data.items():
                        _, _, _, private_mode_enabled, _ = self._get_group_settings(
                            group
                        )
                        player_name = self.last_states.get(steam_id, {}).get(
                            "personaname"
                        )
                        if not player_name:
                            player_name = self._get_display_name(steam_id)
                        display_name = (
                            self.private_name or "有人"
                            if private_mode_enabled
                            else player_name
                        )

                        msg_lines.append(f"{display_name} 在上一个检测周期内玩了：")
                        msg_lines.extend(game_lines)
                        msg_lines.append("")  # 空行分隔不同玩家

                    if msg_lines:
                        try:
                            await self.context.send_message(
                                umo,
                                MessageChain().message("\n".join(msg_lines).strip()),
                            )
                        except Exception as e:
                            logger.warning(f"推送游戏时长消息到 {umo} 失败: {e}")

                if self.is_first_achievement_run:
                    self.is_first_achievement_run = False

                self._last_achievement_success_at = time.time()
                self._last_achievement_error = None
                logger.info(
                    f"本轮成就检查成功，等待 {self.achievement_poll_interval} 秒。"
                )

            except Exception as e:
                self._last_achievement_failure_at = time.time()
                self._last_achievement_error = self._mask_text(e)
                self._log_error(f"成就监控循环发生未捕获的异常: {e}")

            await asyncio.sleep(self.achievement_poll_interval)

    async def _check_achievements_for_user(
        self, steam_id: str, steam_id_to_umos: Dict[str, List[Tuple[str, Dict]]]
    ) -> Optional[Tuple[str, Dict[str, List[str]], Dict[str, Dict]]]:
        """获取并比对单个用户的成就，返回需要更新的数据"""
        async with self.achievement_semaphore:
            try:
                app_ids_to_check = set()
                playtime_updates = {}  # {app_id: {name, diff, current}}

                # 策略1: 获取最近玩过的游戏
                recent_games = await self.get_recently_played_games(steam_id)
                if recent_games:
                    for index, game in enumerate(recent_games):
                        app_id = str(game["appid"])

                        # 计算游戏时长变化
                        playtime_forever = game.get("playtime_forever", 0)
                        stored_playtime = self.last_playtimes.get(steam_id, {}).get(
                            app_id
                        )

                        diff = 0
                        should_update = False

                        if stored_playtime is None:
                            # 首次记录该游戏：视为初始化，不计算差值（避免将历史总时长误报为新增时长），但需要更新存储
                            should_update = True
                            diff = 0
                        elif playtime_forever > stored_playtime:
                            should_update = True
                            diff = playtime_forever - stored_playtime

                        # 智能过滤：仅检查 前2名(兜底) 或 时长增加 的游戏的成就
                        # 这样即使返回了50个游戏，也只会检查真正活跃的那几个，避免API爆炸
                        if index < 2 or diff > 0:
                            app_ids_to_check.add(app_id)

                        if should_update:
                            # 仅当需要推送(diff>0)时才调用API获取双语名，否则用API自带名或暂存名
                            game_name = game.get("name", f"未知游戏({app_id})")
                            if diff > 0:
                                game_name = await self.get_game_name(app_id)
                                if "未知游戏" in game_name and game.get("name"):
                                    game_name = game["name"]

                            playtime_updates[app_id] = {
                                "name": game_name,
                                "diff": diff,
                                "current": playtime_forever,
                            }
                # 策略2: 获取当前正在玩的游戏
                player_state = self.last_states.get(steam_id)
                if player_state and player_state.get("gameid"):
                    app_ids_to_check.add(player_state["gameid"])

                if not app_ids_to_check:
                    return None

                user_achievements_update: Dict[str, List[str]] = {}

                for app_id in app_ids_to_check:
                    player_achievements = await self.get_player_achievements(
                        steam_id, app_id
                    )
                    if player_achievements is None:  # 隐私或API错误
                        continue

                    achieved_list = sorted(
                        [
                            ach["apiname"]
                            for ach in player_achievements
                            if ach["achieved"] == 1
                        ]
                    )
                    last_achieved_list = self.last_achievements.get(steam_id, {}).get(
                        app_id, []
                    )

                    if self.detailed_log:
                        player_name = self._get_log_display_name(steam_id)
                        log_msg = (
                            f"{player_name}({steam_id}) 游戏({app_id}): "
                            f"成就共{len(achieved_list)}个 | 上次成就共{len(last_achieved_list)}个"
                        )
                        logger.info(self._mask_text(log_msg))

                    # 检查是否有新成就
                    new_achievements_names = set(achieved_list) - set(
                        last_achieved_list
                    )

                    # 首次运行不推送，且仅当游戏已有记录时才推送，避免新记录的游戏推送全部历史成就
                    has_prior_record = (
                        steam_id in self.last_achievements
                        and app_id in self.last_achievements[steam_id]
                    )
                    if (
                        not self.is_first_achievement_run
                        and has_prior_record
                        and new_achievements_names
                    ):
                        await self._notify_new_achievements(
                            steam_id,
                            app_id,
                            new_achievements_names,
                            len(achieved_list),
                            steam_id_to_umos,
                        )

                    # 记录需要更新的成就数据
                    user_achievements_update[app_id] = achieved_list

                return steam_id, user_achievements_update, playtime_updates

            except Exception as e:
                self._log_error(f"检查用户 {steam_id} 的成就时出错: {e}")
                return None

    def _build_reverse_map(self) -> Dict[str, List[Tuple[str, Dict]]]:
        """构建 steam_id -> [(umo, group)] 的反向映射"""
        steam_id_to_targets: Dict[str, List[Tuple[str, Dict]]] = {}
        for group in self._effective_monitor_groups():
            if not isinstance(group, dict):
                continue
            steam_ids = self._get_group_steam_ids(group)
            sessions = group.get("sessions", [])
            if not isinstance(sessions, list):
                sessions = []
            for steam_id in steam_ids:
                if steam_id not in steam_id_to_targets:
                    steam_id_to_targets[steam_id] = []
                for umo in sessions:
                    steam_id_to_targets[steam_id].append((umo, group))
        return steam_id_to_targets

    # --- 消息通知 ---
    async def _check_and_notify_status_change(
        self,
        steam_id: str,
        last_state: Dict,
        current_state: Dict,
        steam_id_to_umos: Dict,
    ):
        player_name = self._get_display_name(
            steam_id, current_state.get("personaname")
        )
        last_status = last_state.get("personastate", 0)
        current_status = current_state["personastate"]
        last_game_id = last_state.get("gameid")
        current_game_id = current_state.get("gameid")

        messages_to_send = []

        # 游戏状态变更
        if last_game_id != current_game_id:
            if current_game_id:  # 开始玩新游戏
                # 优先获取商店双语名称
                game_name = await self.get_game_name(current_game_id)
                if "未知游戏" in game_name and current_state.get("gameextrainfo"):
                    game_name = current_state.get("gameextrainfo")

                # 统一更新到状态字典中，确保后续逻辑使用一致的名称
                current_state["gameextrainfo"] = game_name
                messages_to_send.append(
                    (f"{player_name} 开始玩 {game_name} 了", "status")
                )
            else:  # 退出游戏
                last_game_name = await self.get_game_name(last_game_id)
                if "未知游戏" in last_game_name and last_state.get("gameextrainfo"):
                    last_game_name = last_state.get("gameextrainfo")

                # 统一更新到状态字典中，确保后续逻辑（如下方的消息格式化循环）使用一致的名称
                last_state["gameextrainfo"] = last_game_name

                if current_status == 0:  # 游戏中 -> 离线
                    # 使用一个特殊的元组来延迟决定消息内容
                    messages_to_send.append(
                        ((player_name, last_game_name), "game_to_offline")
                    )
                else:  # 游戏中 -> 在线
                    messages_to_send.append(
                        (f"{player_name} 退出了游戏 {last_game_name}", "status")
                    )
        # 在线/离线状态变更 (仅当游戏状态未变时)
        elif last_status != current_status and not current_game_id:
            if last_status == 0 and current_status > 0:  # 上线
                messages_to_send.append((f"{player_name} 上线了", "online_offline"))
            elif last_status > 0 and current_status == 0:  # 下线
                messages_to_send.append((f"{player_name} 下线了", "online_offline"))

        if not messages_to_send:
            return

        umo_targets = steam_id_to_umos.get(steam_id, [])
        for msg_content, msg_type in messages_to_send:
            for umo, group in umo_targets:
                status_ok, online_offline_ok, _, private_mode_enabled, _ = (
                    self._get_group_settings(group)
                )

                display_name = (
                    self.private_name or "有人" if private_mode_enabled else player_name
                )

                final_msg = None
                if msg_type == "game_to_offline":
                    _, l_game_name = msg_content
                    # 根据接收方的配置决定发送哪条消息
                    if online_offline_ok:
                        final_msg = f"{display_name} 下线了"
                    elif status_ok:
                        final_msg = f"{display_name} 退出了游戏 {l_game_name}"
                else:
                    # 原始逻辑
                    should_send = (msg_type == "status" and status_ok) or (
                        msg_type == "online_offline" and online_offline_ok
                    )
                    if should_send:
                        # 重新格式化消息以使用 display_name
                        if msg_type == "status":
                            if "开始玩" in msg_content:
                                game_name = current_state.get("gameextrainfo")
                                final_msg = f"{display_name} 开始玩 {game_name} 了"
                            elif "退出了游戏" in msg_content:
                                last_game_name = last_state.get("gameextrainfo")
                                final_msg = (
                                    f"{display_name} 退出了游戏 {last_game_name}"
                                )
                        elif msg_type == "online_offline":
                            if "上线了" in msg_content:
                                final_msg = f"{display_name} 上线了"
                            elif "下线了" in msg_content:
                                final_msg = f"{display_name} 下线了"

                if final_msg:
                    try:
                        await self.context.send_message(
                            umo, MessageChain().message(final_msg)
                        )
                        logger.info(f"推送消息到 {umo}: {final_msg}")
                    except Exception as e:
                        logger.warning(f"推送消息到 {umo} 失败: {e}")

    async def _notify_new_achievements(
        self,
        steam_id: str,
        app_id: str,
        new_ach_names: Set[str],
        total_achieved: int,
        steam_id_to_umos: Dict,
    ):
        player_name = self._get_display_name(steam_id)
        game_name = await self.get_game_name(app_id)
        schema = await self.get_achievement_schema(app_id)

        if not schema:
            return

        total_schema_count = len(schema)
        ach_details = []
        for name in new_ach_names:
            ach_info = schema.get(name)
            if ach_info:
                ach_details.append(f"  - {ach_info.get('displayName', name)}")

        if not ach_details:
            return

        umo_targets = steam_id_to_umos.get(steam_id, [])
        for umo, group in umo_targets:
            _, _, achievement_ok, private_mode_enabled, _ = self._get_group_settings(
                group
            )
            if achievement_ok:
                display_name = (
                    self.private_name or "有人" if private_mode_enabled else player_name
                )
                msg_body = "\n".join(ach_details)
                msg = (
                    f"{display_name} 在 {game_name} 中获得了新成就：\n{msg_body}\n"
                    f"（已获得{total_achieved}个/共{total_schema_count}个）"
                )
                try:
                    await self.context.send_message(umo, MessageChain().message(msg))
                    logger.info(f"推送成就消息到 {umo}: {msg}")
                except Exception as e:
                    logger.warning(f"推送成就消息到 {umo} 失败: {e}")

    # --- 命令实现 ---
    def _find_groups_by_umo(self, umo: str) -> List[Dict]:
        """查找包含指定会话ID的所有监控组，包括临时测试组"""
        return [
            group
            for group in self._effective_monitor_groups()
            if isinstance(group, dict) and umo in group.get("sessions", [])
        ]

    def _find_config_groups_by_umo(self, umo: str) -> List[Dict]:
        """查找包含指定会话ID的正式监控组"""
        return [
            group
            for group in self.monitored_groups
            if isinstance(group, dict) and umo in group.get("sessions", [])
        ]

    def _get_steam_ids_for_umo(self, umo: str) -> List[str]:
        """获取指定会话关联的所有 Steam ID（去重、保序）"""
        seen = set()
        result = []
        for group in self._find_groups_by_umo(umo):
            for sid in self._get_group_steam_ids(group):
                if sid not in seen:
                    seen.add(sid)
                    result.append(sid)
        return result

    async def _get_formatted_status(
        self, steam_id: str, player: Optional[Dict] = None
    ) -> str:
        """获取单个玩家的格式化状态字符串。可选择传入player字典以避免重复API调用。"""
        # --- 模拟测试用户 ---
        if steam_id == TEST_USER_STEAM_ID:
            game_name = self.test_user_mock_state.get("gameextrainfo", "Cyberpunk 2077")
            if self.test_user_mock_state.get("gameid"):
                return f"{self.test_user_mock_state.get('personaname', '测试ID')} 正在玩 {game_name}"
            state_map = {
                0: "离线",
                1: "在线",
                2: "忙碌",
                3: "离开",
                4: "打盹",
                5: "想交易",
                6: "想玩游戏",
            }
            return f"{self.test_user_mock_state.get('personaname', '测试ID')} {state_map.get(self.test_user_mock_state.get('personastate', 0), '未知状态')}"
        # --- 模拟结束 ---

        if player is None:
            players = await self.get_player_summaries([steam_id])
            if not players:
                return f"{self._get_display_name(steam_id)} 查询失败"
            player = players[0]

        name = self._get_display_name(steam_id, player.get("personaname"))
        game_id = player.get("gameid")
        persona_state = player.get("personastate", 0)

        if game_id:
            game_name = player.get("gameextrainfo") or await self.get_game_name(game_id)
            return f"{name} 正在玩 {game_name}"

        state_map = {
            0: "离线",
            1: "在线",
            2: "忙碌",
            3: "离开",
            4: "打盹",
            5: "想交易",
            6: "想玩游戏",
        }
        return f"{name} {state_map.get(persona_state, '未知状态')}"

    @filter.command("steam list")
    async def steam_list(self, event: AstrMessageEvent):
        """获取当前会话监控的所有玩家的游戏状态。"""
        umo = event.unified_msg_origin
        steam_ids = self._get_steam_ids_for_umo(umo)
        if not steam_ids:
            yield event.plain_result("当前会话未配置监控列表。")
            return

        players = await self.get_player_summaries(steam_ids)
        player_map = {p["steamid"]: p for p in players} if players else {}

        tasks = [
            self._get_formatted_status(sid, player_map.get(sid)) for sid in steam_ids
        ]
        results = await asyncio.gather(*tasks)
        yield event.plain_result("\n".join(results))

    @filter.command("steam alllist")
    async def steam_alllist(self, event: AstrMessageEvent):
        """获取所有会话监控的所有玩家的游戏状态。"""
        if (
            self.admin_only_sensitive_operations
            and str(event.get_sender_id()) not in self.admins
        ):
            yield event.plain_result("此命令仅限管理员使用。")
            return

        effective_groups = self._effective_monitor_groups()
        if not effective_groups:
            yield event.plain_result("没有任何监控配置。")
            return

        all_ids = self._get_all_steam_ids()
        players = await self.get_player_summaries(list(all_ids))
        player_map = {p["steamid"]: p for p in players} if players else {}

        final_reply_parts = []
        for idx, group in enumerate(effective_groups):
            sessions = group.get("sessions", [])
            if not isinstance(sessions, list):
                sessions = []
            steam_ids = self._get_group_steam_ids(group)
            if not steam_ids:
                continue

            session_label = ", ".join(sessions) if sessions else "（未绑定会话）"
            group_note = str(group.get("note", "")).strip()
            group_title = (
                f"监控组{idx + 1}（{group_note}）" if group_note else f"监控组{idx + 1}"
            )
            final_reply_parts.append(f"--- {group_title}: {session_label} ---")
            tasks = [
                self._get_formatted_status(sid, player_map.get(sid))
                for sid in steam_ids
            ]
            results = await asyncio.gather(*tasks)
            final_reply_parts.extend(results)
            final_reply_parts.append("")

        if final_reply_parts:
            final_reply_parts.pop()
        yield event.plain_result("\n".join(final_reply_parts))

    @staticmethod
    def _format_age(timestamp: Optional[float]) -> str:
        if timestamp is None:
            return "尚未运行"
        seconds = max(0, int(time.time() - timestamp))
        if seconds < 60:
            return f"{seconds}秒前"
        if seconds < 3600:
            return f"{seconds // 60}分钟前"
        if seconds < 86400:
            return f"{seconds // 3600}小时前"
        return f"{seconds // 86400}天前"

    def _format_task_state(self, task: Optional[asyncio.Task]) -> str:
        if task is None:
            return "未启动"
        if task.done():
            return "已停止"
        return "运行中"

    def _format_check_state(
        self,
        success_at: Optional[float],
        error: Optional[str],
        failure_at: Optional[float],
    ) -> str:
        if success_at is not None and (failure_at is None or success_at >= failure_at):
            return f"正常（{self._format_age(success_at)}）"
        if error:
            return f"异常（{self._format_age(failure_at)}）：{error}"
        return "尚未检查"

    @filter.command("steam health")
    async def steam_health(self, event: AstrMessageEvent):
        """查看插件运行状态、缓存和监控规模。"""
        if (
            self.admin_only_sensitive_operations
            and str(event.get_sender_id()) not in self.admins
        ):
            yield event.plain_result("此命令仅限管理员使用。")
            return

        actual_groups = [
            group for group in self.monitored_groups if isinstance(group, dict)
        ]
        sessions = {
            umo
            for group in actual_groups
            for umo in group.get("sessions", [])
            if isinstance(umo, str)
        }
        steam_ids = {
            steam_id
            for group in actual_groups
            for steam_id in self._get_group_steam_ids(group)
        }
        cached_names = sum(
            1 for steam_id in steam_ids if self._get_cached_player_name(steam_id)
        )
        remark_counts = {"auto": 0, "manual": 0, "pending": 0, "disabled": 0}
        for state in self.id_remarks.values():
            source = state.get("source", "pending")
            remark_counts[source] = remark_counts.get(source, 0) + 1

        invalid_entries = sum(
            1
            for _, _, _, (steam_id, _, exact) in self._iter_steam_id_entries()
            if not steam_id or not exact
        )
        api_state = "未配置" if not self.api_key else "已配置"
        api_request_state = self._format_check_state(
            self._last_api_success_at,
            self._last_api_error,
            self._last_api_error_at,
        )
        status_state = (
            "未启动"
            if self.status_monitor_task is None
            else self._format_check_state(
                self._last_status_success_at,
                self._last_status_error,
                self._last_status_failure_at,
            )
        )
        if self.achievement_poll_interval <= 0:
            achievement_state = "已关闭"
        elif self.achievement_monitor_task is None:
            achievement_state = "未启动"
        else:
            achievement_state = self._format_check_state(
                self._last_achievement_success_at,
                self._last_achievement_error,
                self._last_achievement_failure_at,
            )

        lines = [
            "Steam 监控健康状态",
            f"API Key：{api_state}",
            f"API请求：{api_request_state}",
            f"状态检查：{status_state}",
            f"成就检查：{achievement_state}",
            f"监控规模：{len(actual_groups)} 个监控组 / {len(sessions)} 个会话 / {len(steam_ids)} 名用户",
            f"昵称缓存：{cached_names}/{len(steam_ids)} 已缓存",
            "ID备注："
            f"自动 {remark_counts['auto']}、手动 {remark_counts['manual']}、"
            f"待获取 {remark_counts['pending']}、关闭 {remark_counts['disabled']}",
            f"数据缓存：游戏名 {len(self.game_cache)} 个，成就纲要 {len(self.achievement_schema)} 个",
        ]
        if invalid_entries:
            lines.append(f"配置提醒：有 {invalid_entries} 个 Steam ID 条目格式不标准")
        lines.extend(
            [
                f"状态任务：{self._format_task_state(self.status_monitor_task)}",
                f"成就任务：{self._format_task_state(self.achievement_monitor_task)}",
                f"日志脱敏：{'开启' if self.log_masking else '关闭'}",
            ]
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("steam add")
    async def steam_add(self, event: AstrMessageEvent, steam_id: str):
        """在当前会话添加一个监控Steam ID。"""
        if (
            self.admin_only_sensitive_operations
            and str(event.get_sender_id()) not in self.admins
        ):
            yield event.plain_result("此命令仅限管理员使用。")
            return

        umo = event.unified_msg_origin
        parsed_id, parsed_remark, _ = self._parse_steam_id_entry(steam_id)
        if not parsed_id:
            yield event.plain_result("请输入一个有效的17位Steam ID。")
            return
        steam_id = parsed_id
        if parsed_remark:
            parsed_remark = self._sanitize_remark(parsed_remark)

        if steam_id == TEST_USER_STEAM_ID:
            yield event.plain_result(
                f"测试用户ID {TEST_USER_STEAM_ID} 为内置ID，无法手动添加。"
            )
            return

        # 查找当前会话所在的监控组，如果没有则创建一个新的
        groups = self._find_config_groups_by_umo(umo)
        if not groups:
            new_group = {
                "__template_key": "monitor_group",
                "sessions": [umo],
                "steam_ids": [],
                "settings": self._default_group_settings(),
            }
            self.monitored_groups.append(new_group)
            groups = [new_group]

        # 添加到第一个匹配的监控组
        target_group = groups[0]
        response = f"{steam_id} 已在监控列表中。"
        async with self._remark_lock:
            existing_ids = self._get_group_steam_ids(target_group)
            if steam_id not in existing_ids:
                if parsed_remark is not None:
                    self.id_remarks[steam_id] = (
                        {"source": "manual", "value": parsed_remark}
                        if parsed_remark
                        else {"source": "pending", "marker": True}
                    )
                    self._remarks_dirty = True
                    if parsed_remark:
                        entry = f"{steam_id}（{parsed_remark}）"
                    else:
                        entry = f"{steam_id}（）"
                else:
                    self.id_remarks.setdefault(
                        steam_id, {"source": "pending", "marker": False}
                    )
                    self._remarks_dirty = True
                    entry = steam_id
                target_group.setdefault("steam_ids", []).append(entry)
                self.config["monitored_targets"] = self.monitored_groups
                await self._save_plugin_config()
                self._config_dirty = False
                if self._remarks_dirty:
                    await self._save_data(self.id_remarks_path, self.id_remarks)
                    self._remarks_dirty = False
                display_name = self._get_cached_player_name(steam_id) or steam_id
                response = f"已将 {display_name} 添加到当前会话的监控列表。"
        yield event.plain_result(response)

    @filter.command("steam remove")
    async def steam_remove(self, event: AstrMessageEvent, steam_id: str):
        """在当前会话移除一个监控Steam ID。"""
        if (
            self.admin_only_sensitive_operations
            and str(event.get_sender_id()) not in self.admins
        ):
            yield event.plain_result("此命令仅限管理员使用。")
            return

        umo = event.unified_msg_origin
        steam_id, _, _ = self._parse_steam_id_entry(steam_id)
        if not steam_id:
            yield event.plain_result("请输入一个有效的17位Steam ID。")
            return
        if steam_id == TEST_USER_STEAM_ID:
            yield event.plain_result(
                f"测试用户ID {TEST_USER_STEAM_ID} 为内置ID，无法移除。"
            )
            return

        removed = False
        async with self._remark_lock:
            for group in self._find_config_groups_by_umo(umo):
                raw_ids = group.get("steam_ids", [])
                if not isinstance(raw_ids, list):
                    continue
                for index, raw in enumerate(raw_ids):
                    if self._parse_steam_id_entry(raw)[0] == steam_id:
                        raw_ids.pop(index)
                        removed = True
                        break
                if removed:
                    break

            if removed:
                self._prepare_remark_states()
                self.config["monitored_targets"] = self.monitored_groups
                await self._save_plugin_config()
                self._config_dirty = False
                if self._remarks_dirty:
                    await self._save_data(self.id_remarks_path, self.id_remarks)
                    self._remarks_dirty = False
                response = f"已将 {steam_id} 从当前会话的监控列表移除。"
            else:
                response = f"当前会话的监控列表中没有找到 {steam_id}。"
        yield event.plain_result(response)

    async def _setup_test_user(self, umo: str):
        """为当前会话创建仅存在于内存中的测试监控组。"""
        if self.test_reset_task and not self.test_reset_task.done():
            self.test_reset_task.cancel()
            await asyncio.gather(self.test_reset_task, return_exceptions=True)

        # 状态循环会在测试结束后移除测试用户；再次测试时先恢复基准状态。
        self.last_states.setdefault(
            TEST_USER_STEAM_ID, TEST_USER_INITIAL_STATE.copy()
        )
        self.last_achievements.setdefault(TEST_USER_STEAM_ID, {}).setdefault(
            TEST_USER_INITIAL_STATE["gameid"], []
        )

        self._test_monitor_group = {
            "note": "插件测试（临时）",
            "sessions": [umo],
            "steam_ids": [TEST_USER_STEAM_ID],
            "settings": self._default_group_settings(),
        }

    @filter.command("steam test status")
    async def steam_test_status(self, event: AstrMessageEvent):
        """测试状态变更：游戏中 -> 在线"""
        if not self.api_key:
            yield event.plain_result("未配置 Steam API Key，监控循环未启动，无法测试推送。")
            return
        await self._setup_test_user(event.unified_msg_origin)
        # 将模拟状态从“游戏中”变为“在线”
        self.test_user_mock_state = {
            "personaname": "测试ID",
            "personastate": 1,
            "gameid": None,
            "gameextrainfo": None,
        }
        yield event.plain_result(
            "测试命令已触发：测试用户状态已变为“在线”。请等待下一轮【状态检查】循环以查看推送效果。"
        )
        self.test_reset_task = asyncio.create_task(self._reset_test_user_delayed())

    @filter.command("steam test achievements")
    async def steam_test_achievements(self, event: AstrMessageEvent):
        """测试成就变更：随机增加一个成就"""
        if not self.api_key:
            yield event.plain_result("未配置 Steam API Key，监控循环未启动，无法测试推送。")
            return
        if self.achievement_poll_interval <= 0:
            yield event.plain_result("成就监控已关闭，请先将成就检查间隔设置为大于 0。")
            return
        await self._setup_test_user(event.unified_msg_origin)
        app_id = TEST_USER_INITIAL_STATE["gameid"]
        schema = await self.get_achievement_schema(app_id)
        if not schema:
            yield event.plain_result("无法获取测试游戏的成就纲要，测试失败。")
            return

        # 从主数据中获取上次的成就，以决定可以添加哪个新成就
        last_known_achs = set(
            self.last_achievements.get(TEST_USER_STEAM_ID, {}).get(app_id, [])
        )
        all_schema_achs = set(schema.keys())
        unlocked_achs = all_schema_achs - last_known_achs

        if not unlocked_achs:
            # 如果全成就了，为了能继续测试，就重置成就
            self.last_achievements[TEST_USER_STEAM_ID][app_id] = []
            self.test_user_mock_achievements[app_id] = []
            last_known_achs = set()
            unlocked_achs = all_schema_achs
            await event.send(
                event.plain_result("测试用户已全成就，现已重置其成就列表以便测试。")
            )

        new_ach_name = random.choice(list(unlocked_achs))

        # 更新模拟器的“下一次API返回”状态
        # 确保 self.test_user_mock_achievements 是基于 self.last_achievements 的状态来更新的
        new_ach_list = list(last_known_achs)
        new_ach_list.append(new_ach_name)
        self.test_user_mock_achievements[app_id] = new_ach_list

        yield event.plain_result(
            f"测试命令已触发：为测试用户在 Cyberpunk 2077 中添加了新成就“{schema[new_ach_name]['displayName']}”。请等待下一轮【成就检查】循环。"
        )
        self.test_reset_task = asyncio.create_task(
            self._reset_test_user_delayed(self.achievement_poll_interval + 5)
        )

    @filter.command("steam test offline")
    async def steam_test_offline(self, event: AstrMessageEvent):
        """测试状态变更：游戏中 -> 离线"""
        if not self.api_key:
            yield event.plain_result("未配置 Steam API Key，监控循环未启动，无法测试推送。")
            return
        await self._setup_test_user(event.unified_msg_origin)
        # 将模拟状态从“游戏中”变为“离线”
        self.test_user_mock_state = {
            "personaname": "测试ID",
            "personastate": 0,
            "gameid": None,
            "gameextrainfo": None,
        }
        yield event.plain_result(
            "测试命令已触发：测试用户状态已变为“离线”。请等待下一轮【状态检查】循环以查看推送效果。"
        )
        self.test_reset_task = asyncio.create_task(self._reset_test_user_delayed())

    async def _reset_test_user_delayed(self, delay_seconds: Optional[int] = None):
        """延迟重置测试用户状态"""
        # 等待足够长的时间，确保监控循环已经处理了模拟状态
        await asyncio.sleep(
            self.status_poll_interval + 5
            if delay_seconds is None
            else delay_seconds
        )

        # 重置下一次API调用将返回的模拟状态
        self.test_user_mock_state = TEST_USER_INITIAL_STATE.copy()
        self._test_monitor_group = None

        # 同时也重置主循环中的“上一次”状态记录，以防万一
        self.last_states[TEST_USER_STEAM_ID] = TEST_USER_INITIAL_STATE.copy()
        await self._save_data(self.last_states_path, self.last_states)

        self._log_warning("测试用户的模拟状态已自动恢复，临时监控已结束。")

    async def terminate(self):
        """插件终止时调用的清理函数"""
        # 方案二：优雅停机，先停止任务并等待结束
        tasks = []
        if self.status_monitor_task and not self.status_monitor_task.done():
            self.status_monitor_task.cancel()
            tasks.append(self.status_monitor_task)
        if self.achievement_monitor_task and not self.achievement_monitor_task.done():
            self.achievement_monitor_task.cancel()
            tasks.append(self.achievement_monitor_task)
        if self.test_reset_task and not self.test_reset_task.done():
            self.test_reset_task.cancel()
            tasks.append(self.test_reset_task)
        for game_task in self._pending_game_tasks.values():
            if not game_task.done():
                game_task.cancel()
                tasks.append(game_task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if hasattr(self, "http_client") and not self.http_client.is_closed:
            await self.http_client.aclose()

        # 保存所有数据
        await self._save_data(self.last_states_path, self.last_states)
        await self._save_data(self.last_achievements_path, self.last_achievements)
        await self._save_data(self.last_playtimes_path, self.last_playtimes)
        await self._save_data(self.game_cache_path, self.game_cache)
        await self._save_data(self.achievement_schema_path, self.achievement_schema)
        await self._save_data(self.player_profiles_path, self.player_profiles)
        await self._save_data(self.id_remarks_path, self.id_remarks)

        logger.info("Steam 监控插件已停止并保存了所有数据。")
