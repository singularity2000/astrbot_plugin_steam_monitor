import asyncio
import json
import os
import random
from typing import Dict, List, Any, Optional, Set, Tuple

import httpx
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

# 测试用户的常量
TEST_USER_STEAM_ID = "70000000000000001"
TEST_USER_INITIAL_STATE = {
    "personaname": "测试ID",
    "personastate": 1,
    "gameid": "774241", # Cyberpunk 2077 App ID
    "gameextrainfo": "Cyberpunk 2077",
}

@register(
    "steam_monitor",
    "Singularity2000",
    "一个简单但强大的 Steam 游戏状态监控插件，用于推送游戏开始/结束和成就获得通知。",
    "1.0.0",
    "https://github.com/Singularity2000/astrbot_plugin_steam_monitor"
)
class SteamMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_steam_monitor")
        os.makedirs(self.data_dir, exist_ok=True)

        # 数据文件路径
        self.last_states_path = os.path.join(self.data_dir, "last_states.json")
        self.last_achievements_path = os.path.join(self.data_dir, "last_achievements.json")
        self.game_cache_path = os.path.join(self.data_dir, "game_cache.json")
        self.achievement_schema_path = os.path.join(self.data_dir, "achievement_schema.json")

        # 加载配置和数据
        self._load_config()
        bot_config = self.context.get_config()
        self.admins: List[str] = [str(admin) for admin in bot_config.get("admins_id", [])]
        self.last_states: Dict[str, Dict[str, Any]] = self._load_data(self.last_states_path)
        self.last_achievements: Dict[str, Dict[str, List[str]]] = self._load_data(self.last_achievements_path)
        self.game_cache: Dict[str, str] = self._load_data(self.game_cache_path)
        self.achievement_schema: Dict[str, Dict[str, Any]] = self._load_data(self.achievement_schema_path)

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
        self.achievement_semaphore = asyncio.Semaphore(4)

        if self.api_key:
            self.status_monitor_task = asyncio.create_task(self.status_monitoring_loop())
            # 错开启动，避免同时请求
            asyncio.create_task(self._start_achievement_loop_delayed())
        else:
            logger.warning("Steam API Key 未配置，插件不会启动。")

    def _load_config(self):
        """从配置对象加载或重载配置"""
        self.api_key: str = self.config.get("steam_api_key", "")
        self.admin_only_sensitive_operations: bool = self.config.get("admin_only_sensitive_operations", True)
        self.status_poll_interval: int = self.config.get("status_poll_interval", 60)
        self.achievement_poll_interval: int = self.config.get("achievement_poll_interval", 600)
        self.retry_times: int = self.config.get("retry_times", 3)
        self.detailed_log: bool = self.config.get("detailed_poll_log", False)
        
        # 全局通知设置
        self.global_status_notification: bool = self.config.get("status_notification", True)
        self.global_online_offline: bool = self.config.get("online_offline_notification", False)
        self.global_achievements: bool = self.config.get("achievements_notification", True)
        self.private_mode: bool = self.config.get("private_mode", False)
        self.private_name: str = self.config.get("private_name", "")
        
        try:
            self.monitored_targets: Dict[str, Dict] = json.loads(self.config.get("monitored_targets", "{}"))
        except json.JSONDecodeError:
            self.monitored_targets = {}
            logger.error("解析 'monitored_targets' 配置失败，请检查JSON格式。")

    def _load_data(self, path: str) -> Dict:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"加载数据失败 {path}: {e}")
        return {}

    def _save_data(self, path: str, data: Any):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存数据失败 {path}: {e}")

    def _get_all_steam_ids(self) -> Set[str]:
        """从监控目标中提取所有唯一的Steam ID"""
        all_ids = set()
        for target_info in self.monitored_targets.values():
            if isinstance(target_info, dict) and "steam_ids" in target_info:
                all_ids.update(target_info["steam_ids"])
        return all_ids

    def _get_umo_settings(self, umo: str) -> Tuple[bool, bool, bool, bool]:
        """获取指定会话的通知设置，如果未指定则回退到全局设置"""
        target_info = self.monitored_targets.get(umo, {})
        settings = target_info.get("settings") if isinstance(target_info, dict) else None

        if settings and isinstance(settings, dict):
            status_enabled = settings.get("status_notification", self.global_status_notification)
            online_offline_enabled = status_enabled and settings.get("online_offline_notification", self.global_online_offline)
            achievement_enabled = settings.get("achievements_notification", self.global_achievements)
            private_mode_enabled = settings.get("private_mode", self.private_mode)
            return status_enabled, online_offline_enabled, achievement_enabled, private_mode_enabled
        
        # 回退到全局
        status_enabled = self.global_status_notification
        online_offline_enabled = status_enabled and self.global_online_offline
        achievement_enabled = self.global_achievements
        private_mode_enabled = self.private_mode
        return status_enabled, online_offline_enabled, achievement_enabled, private_mode_enabled

    async def _make_request(self, url: str, ignore_errors: bool = False) -> Optional[Dict]:
        """发起HTTP请求，支持重试"""
        for attempt in range(self.retry_times):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if not ignore_errors:
                    logger.warning(f"请求失败 (第 {attempt + 1} 次): {e}")
                if attempt < self.retry_times - 1:
                    await asyncio.sleep(2 ** attempt)
        if not ignore_errors:
            logger.error(f"请求失败，已达最大重试次数: {url}")
        return None

    # --- API 调用封装 ---
    async def get_player_summaries(self, steam_ids: List[str]) -> Optional[List[Dict]]:
        if not steam_ids: return None

        mock_player_data = []
        real_steam_ids = list(steam_ids)

        # --- 模拟测试用户 ---
        if TEST_USER_STEAM_ID in real_steam_ids:
            real_steam_ids.remove(TEST_USER_STEAM_ID)
            # 从专用的模拟状态变量中读取
            mock_player = {
                "steamid": TEST_USER_STEAM_ID,
                "personaname": self.test_user_mock_state.get("personaname", "测试ID"),
                "personastate": self.test_user_mock_state.get("personastate", 1),
                "gameid": self.test_user_mock_state.get("gameid"),
                "gameextrainfo": self.test_user_mock_state.get("gameextrainfo"),
            }
            mock_player_data.append(mock_player)
        # --- 模拟结束 ---

        if not real_steam_ids:
            return mock_player_data

        ids_str = ",".join(real_steam_ids)
        url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={self.api_key}&steamids={ids_str}"
        data = await self._make_request(url)
        
        real_players = data.get("response", {}).get("players") if data else []
        
        return real_players + mock_player_data if real_players else mock_player_data

    async def get_game_name(self, app_id: str) -> str:
        if app_id in self.game_cache:
            return self.game_cache[app_id]
        
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&l=schinese"
        data = await self._make_request(url, ignore_errors=True)
        if data and str(app_id) in data and data[str(app_id)].get("success"):
            name = data[str(app_id)]["data"]["name"]
            self.game_cache[app_id] = name
            self._save_data(self.game_cache_path, self.game_cache)
            return name
        return f"未知游戏({app_id})"

    async def get_recently_played_games(self, steam_id: str) -> Optional[List[Dict]]:
        url = f"https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/?key={self.api_key}&steamid={steam_id}&count=2"
        data = await self._make_request(url, ignore_errors=True)
        return data.get("response", {}).get("games") if data and data.get("response", {}).get("total_count", 0) > 0 else None

    async def get_player_achievements(self, steam_id: str, app_id: str) -> Optional[List[Dict]]:
        # --- 模拟测试用户 ---
        if steam_id == TEST_USER_STEAM_ID:
            # 从专用的模拟成就变量中读取
            achieved_apis = self.test_user_mock_achievements.get(app_id, [])
            return [{"apiname": api_name, "achieved": 1} for api_name in achieved_apis]
        # --- 模拟结束 ---

        url = f"https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/?key={self.api_key}&steamid={steam_id}&appid={app_id}&l=schinese"
        data = await self._make_request(url, ignore_errors=True)
        if data and data.get("playerstats", {}).get("success"):
            return data["playerstats"].get("achievements", [])
        return None

    async def get_achievement_schema(self, app_id: str) -> Optional[Dict[str, Any]]:
        if app_id in self.achievement_schema:
            return self.achievement_schema[app_id]
        
        url = f"https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/?key={self.api_key}&appid={app_id}&l=schinese"
        data = await self._make_request(url, ignore_errors=True)
        if data and "game" in data and "availableGameStats" in data["game"]:
            schema = {ach["name"]: ach for ach in data["game"]["availableGameStats"]["achievements"]}
            self.achievement_schema[app_id] = schema
            self._save_data(self.achievement_schema_path, self.achievement_schema)
            return schema
        return None

    # --- 监控循环 ---
    async def _start_achievement_loop_delayed(self):
        await asyncio.sleep(15) # 错开15秒
        self.achievement_monitor_task = asyncio.create_task(self.achievement_monitoring_loop())

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
                
                if not players:
                    logger.warning("无法从Steam API获取玩家信息，跳过本轮状态检查。")
                    await asyncio.sleep(self.status_poll_interval)
                    continue

                current_states = {}
                for player in players:
                    steam_id = player["steamid"]
                    player_name = player.get("personaname", "未知玩家")
                    current_state = {
                        "personaname": player_name,
                        "personastate": player.get("personastate", 0),
                        "gameid": player.get("gameid"),
                        "gameextrainfo": player.get("gameextrainfo"),
                    }
                    last_state = self.last_states.get(steam_id, {})
                    
                    if self.detailed_log:
                        log_msg = (
                            f"{player_name}({steam_id}): "
                            f"当前状态 {current_state['personastate']}, 游戏ID {current_state.get('gameid')} | "
                            f"上次状态 {last_state.get('personastate')}, 游戏ID {last_state.get('gameid')}"
                        )
                        logger.info(log_msg)

                    # 首次运行不推送
                    if not self.is_first_status_run and last_state:
                        await self._check_and_notify_status_change(steam_id, last_state, current_state, steam_id_to_umos)
                    
                    current_states[steam_id] = current_state
                
                # 更新测试用户状态
                current_states[TEST_USER_STEAM_ID] = self.last_states.get(TEST_USER_STEAM_ID, TEST_USER_INITIAL_STATE.copy())

                self.last_states = current_states
                self._save_data(self.last_states_path, self.last_states)

                if self.is_first_status_run:
                    self.is_first_status_run = False
                
                logger.info(f"本轮在线、游戏状态检查成功，等待 {self.status_poll_interval} 秒。")

            except Exception as e:
                logger.error(f"状态监控循环发生未捕获的异常: {e}", exc_info=True)
            
            await asyncio.sleep(self.status_poll_interval)

    async def achievement_monitoring_loop(self):
        """独立的成就监控循环（并行优化版）"""
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
                for result_data in results:
                    if result_data:
                        steam_id, user_achievements = result_data
                        if steam_id not in self.last_achievements:
                            self.last_achievements[steam_id] = {}
                        self.last_achievements[steam_id].update(user_achievements)

                self._save_data(self.last_achievements_path, self.last_achievements)

                if self.is_first_achievement_run:
                    self.is_first_achievement_run = False

                logger.info(f"本轮成就检查成功，等待 {self.achievement_poll_interval} 秒。")

            except Exception as e:
                logger.error(f"成就监控循环发生未捕获的异常: {e}", exc_info=True)

            await asyncio.sleep(self.achievement_poll_interval)

    async def _check_achievements_for_user(
        self, steam_id: str, steam_id_to_umos: Dict[str, List[str]]
    ) -> Optional[Tuple[str, Dict[str, List[str]]]]:
        """获取并比对单个用户的成就，返回需要更新的数据"""
        async with self.achievement_semaphore:
            try:
                app_ids_to_check = set()
                # 策略1: 获取最近玩过的游戏
                recent_games = await self.get_recently_played_games(steam_id)
                if recent_games:
                    for game in recent_games:
                        app_ids_to_check.add(str(game["appid"]))

                # 策略2: 获取当前正在玩的游戏
                player_state = self.last_states.get(steam_id)
                if player_state and player_state.get("gameid"):
                    app_ids_to_check.add(player_state["gameid"])

                if not app_ids_to_check:
                    return None

                user_achievements_update: Dict[str, List[str]] = {}

                for app_id in app_ids_to_check:
                    player_achievements = await self.get_player_achievements(steam_id, app_id)
                    if player_achievements is None:  # 隐私或API错误
                        continue

                    achieved_list = sorted([ach["apiname"] for ach in player_achievements if ach["achieved"] == 1])
                    last_achieved_list = self.last_achievements.get(steam_id, {}).get(app_id, [])

                    if self.detailed_log:
                        player_name = self.last_states.get(steam_id, {}).get("personaname", steam_id)
                        log_msg = (
                            f"{player_name}({steam_id}) 游戏({app_id}): "
                            f"成就共{len(achieved_list)}个 | 上次成就共{len(last_achieved_list)}个"
                        )
                        logger.info(log_msg)

                    # 检查是否有新成就
                    new_achievements_names = set(achieved_list) - set(last_achieved_list)

                    # 首次运行不推送，且仅当游戏已有记录时才推送，避免新记录的游戏推送全部历史成就
                    has_prior_record = steam_id in self.last_achievements and app_id in self.last_achievements[steam_id]
                    if not self.is_first_achievement_run and has_prior_record and new_achievements_names:
                        await self._notify_new_achievements(
                            steam_id, app_id, new_achievements_names, len(achieved_list), steam_id_to_umos
                        )

                    # 记录需要更新的成就数据
                    user_achievements_update[app_id] = achieved_list
                
                return steam_id, user_achievements_update

            except Exception as e:
                logger.error(f"检查用户 {steam_id} 的成就时出错: {e}", exc_info=True)
                return None


    def _build_reverse_map(self) -> Dict[str, List[str]]:
        """构建 steam_id -> [umos] 的反向映射"""
        steam_id_to_umos: Dict[str, List[str]] = {}
        for umo, target_info in self.monitored_targets.items():
            if isinstance(target_info, dict) and "steam_ids" in target_info:
                for steam_id in target_info["steam_ids"]:
                    if steam_id not in steam_id_to_umos:
                        steam_id_to_umos[steam_id] = []
                    steam_id_to_umos[steam_id].append(umo)
        return steam_id_to_umos

    # --- 消息通知 ---
    async def _check_and_notify_status_change(self, steam_id: str, last_state: Dict, current_state: Dict, steam_id_to_umos: Dict):
        player_name = current_state.get("personaname", "未知玩家")
        last_status = last_state.get('personastate', 0)
        current_status = current_state['personastate']
        last_game_id = last_state.get('gameid')
        current_game_id = current_state.get('gameid')
        
        messages_to_send = []

        # 游戏状态变更
        if last_game_id != current_game_id:
            if current_game_id: # 开始玩新游戏
                game_name = current_state.get("gameextrainfo") or await self.get_game_name(current_game_id)
                current_state["gameextrainfo"] = game_name
                messages_to_send.append((f"【{player_name}】开始玩【{game_name}】了", "status"))
            else: # 退出游戏
                last_game_name = last_state.get("gameextrainfo") or await self.get_game_name(last_game_id)
                if current_status == 0: # 游戏中 -> 离线
                    # 使用一个特殊的元组来延迟决定消息内容
                    messages_to_send.append(((player_name, last_game_name), "game_to_offline"))
                else: # 游戏中 -> 在线
                    messages_to_send.append((f"【{player_name}】退出了游戏【{last_game_name}】", "status"))
        # 在线/离线状态变更 (仅当游戏状态未变时)
        elif last_status != current_status and not current_game_id:
            if last_status == 0 and current_status > 0: # 上线
                messages_to_send.append((f"【{player_name}】上线了", "online_offline"))
            elif last_status > 0 and current_status == 0: # 下线
                messages_to_send.append((f"【{player_name}】下线了", "online_offline"))

        if not messages_to_send:
            return

        umos = steam_id_to_umos.get(steam_id, [])
        for msg_content, msg_type in messages_to_send:
            for umo in umos:
                status_ok, online_offline_ok, _, private_mode_enabled = self._get_umo_settings(umo)
                
                display_name = self.private_name or "有人" if private_mode_enabled else player_name

                final_msg = None
                if msg_type == "game_to_offline":
                    _, l_game_name = msg_content
                    # 根据接收方的配置决定发送哪条消息
                    if online_offline_ok:
                        final_msg = f"【{display_name}】下线了"
                    elif status_ok:
                        final_msg = f"【{display_name}】退出了游戏【{l_game_name}】"
                else:
                    # 原始逻辑
                    should_send = (msg_type == "status" and status_ok) or \
                                  (msg_type == "online_offline" and online_offline_ok)
                    if should_send:
                        # 重新格式化消息以使用 display_name
                        if msg_type == "status":
                            if "开始玩" in msg_content:
                                game_name = current_state.get("gameextrainfo")
                                final_msg = f"【{display_name}】开始玩【{game_name}】了"
                            elif "退出了游戏" in msg_content:
                                last_game_name = last_state.get("gameextrainfo")
                                final_msg = f"【{display_name}】退出了游戏【{last_game_name}】"
                        elif msg_type == "online_offline":
                            if "上线了" in msg_content:
                                final_msg = f"【{display_name}】上线了"
                            elif "下线了" in msg_content:
                                final_msg = f"【{display_name}】下线了"

                if final_msg:
                    await self.context.send_message(umo, MessageChain().message(final_msg))
                    logger.info(f"推送消息到 {umo}: {final_msg}")

    async def _notify_new_achievements(self, steam_id: str, app_id: str, new_ach_names: Set[str], total_achieved: int, steam_id_to_umos: Dict):
        player_name = self.last_states.get(steam_id, {}).get("personaname", steam_id)
        game_name = await self.get_game_name(app_id)
        schema = await self.get_achievement_schema(app_id)
        
        if not schema: return

        total_schema_count = len(schema)
        ach_details = []
        for name in new_ach_names:
            ach_info = schema.get(name)
            if ach_info:
                ach_details.append(f"  - {ach_info.get('displayName', name)}")

        if not ach_details: return

        umos = steam_id_to_umos.get(steam_id, [])
        for umo in umos:
            _, _, achievement_ok, private_mode_enabled = self._get_umo_settings(umo)
            if achievement_ok:
                display_name = self.private_name or "有人" if private_mode_enabled else player_name
                msg_body = "\n".join(ach_details)
                msg = (
                    f"【{display_name}】在【{game_name}】中获得了新成就：\n{msg_body}\n"
                    f"（已获得{total_achieved}个/共{total_schema_count}个）"
                )
                await self.context.send_message(umo, MessageChain().message(msg))
                logger.info(f"推送成就消息到 {umo}: {msg}")

    # --- 命令实现 ---
    async def _get_formatted_status(self, steam_id: str) -> str:
        """获取单个玩家的格式化状态字符串"""
        # --- 模拟测试用户 ---
        if steam_id == TEST_USER_STEAM_ID:
            game_name = self.test_user_mock_state.get("gameextrainfo", "Cyberpunk 2077")
            if self.test_user_mock_state.get("gameid"):
                return f"【{self.test_user_mock_state.get('personaname', '测试ID')}】正在玩【{game_name}】"
            state_map = {0: "离线", 1: "在线", 2: "忙碌", 3: "离开", 4: "打盹", 5: "想交易", 6: "想玩游戏"}
            return f"【{self.test_user_mock_state.get('personaname', '测试ID')}】{state_map.get(self.test_user_mock_state.get('personastate', 0), '未知状态')}"
        # --- 模拟结束 ---

        players = await self.get_player_summaries([steam_id])
        if not players:
            return f"【{steam_id}】查询失败"

        player = players[0]
        name = player.get("personaname", "未知玩家")
        game_id = player.get("gameid")
        persona_state = player.get("personastate", 0)

        if game_id:
            game_name = player.get("gameextrainfo") or await self.get_game_name(game_id)
            return f"【{name}】正在玩【{game_name}】"
        
        state_map = {0: "离线", 1: "在线", 2: "忙碌", 3: "离开", 4: "打盹", 5: "想交易", 6: "想玩游戏"}
        return f"【{name}】{state_map.get(persona_state, '未知状态')}"

    @filter.command("steam list")
    async def steam_list(self, event: AstrMessageEvent):
        """获取当前会话监控的所有玩家的游戏状态。"""
        umo = event.unified_msg_origin
        target_info = self.monitored_targets.get(umo)
        if not target_info or not target_info.get("steam_ids"):
            yield event.plain_result("当前会话未配置监控列表。" )
            return

        steam_ids = target_info["steam_ids"]
        tasks = [self._get_formatted_status(sid) for sid in steam_ids]
        results = await asyncio.gather(*tasks)
        yield event.plain_result("\n".join(results))

    @filter.command("steam alllist")
    async def steam_alllist(self, event: AstrMessageEvent):
        """获取所有会话监控的所有玩家的游戏状态。"""
        if self.admin_only_sensitive_operations and str(event.get_sender_id()) not in self.admins:
            yield event.plain_result("此命令仅限管理员使用。")
            return

        if not self.monitored_targets:
            yield event.plain_result("没有任何监控配置。" )
            return
            
        final_reply_parts = []
        for umo, target_info in self.monitored_targets.items():
            steam_ids = target_info.get("steam_ids")
            if not steam_ids: continue
            
            final_reply_parts.append(f"--- {umo} ---")
            tasks = [self._get_formatted_status(sid) for sid in steam_ids]
            results = await asyncio.gather(*tasks)
            final_reply_parts.extend(results)
            final_reply_parts.append("") 
        
        if final_reply_parts: final_reply_parts.pop()
        yield event.plain_result("\n".join(final_reply_parts))

    @filter.command("steam add")
    async def steam_add(self, event: AstrMessageEvent, steam_id: str):
        """在当前会话添加一个监控Steam ID。"""
        if self.admin_only_sensitive_operations and str(event.get_sender_id()) not in self.admins:
            yield event.plain_result("此命令仅限管理员使用。")
            return

        umo = event.unified_msg_origin
        if not steam_id.isdigit() or len(steam_id) != 17:
            yield event.plain_result("请输入一个有效的17位Steam ID。" )
            return
        
        if steam_id == TEST_USER_STEAM_ID:
            yield event.plain_result(f"测试用户ID {TEST_USER_STEAM_ID} 为内置ID，无法手动添加。" )
            return

        if umo not in self.monitored_targets:
            self.monitored_targets[umo] = {"steam_ids": [], "settings": None}
        
        if "steam_ids" not in self.monitored_targets[umo]:
             self.monitored_targets[umo]["steam_ids"] = []

        if steam_id not in self.monitored_targets[umo]["steam_ids"]:
            self.monitored_targets[umo]["steam_ids"].append(steam_id)
            self.config["monitored_targets"] = json.dumps(self.monitored_targets, indent=2, ensure_ascii=False)
            self.config.save_config()
            yield event.plain_result(f"已将 {steam_id} 添加到当前会话的监控列表。" )
        else:
            yield event.plain_result(f"{steam_id} 已在监控列表中。" )

    @filter.command("steam remove")
    async def steam_remove(self, event: AstrMessageEvent, steam_id: str):
        """在当前会话移除一个监控Steam ID。"""
        if self.admin_only_sensitive_operations and str(event.get_sender_id()) not in self.admins:
            yield event.plain_result("此命令仅限管理员使用。")
            return

        umo = event.unified_msg_origin
        if steam_id == TEST_USER_STEAM_ID:
            yield event.plain_result(f"测试用户ID {TEST_USER_STEAM_ID} 为内置ID，无法移除。" )
            return

        if umo in self.monitored_targets and steam_id in self.monitored_targets[umo].get("steam_ids", []):
            self.monitored_targets[umo]["steam_ids"].remove(steam_id)
            self.config["monitored_targets"] = json.dumps(self.monitored_targets, indent=2, ensure_ascii=False)
            self.config.save_config()
            yield event.plain_result(f"已将 {steam_id} 从当前会话的监控列表移除。" )
        else:
            yield event.plain_result(f"当前会话的监控列表中没有找到 {steam_id}。" )

    async def _setup_test_user(self, event: AstrMessageEvent):
        """确保测试用户在当前会话的监控列表中"""
        umo = event.unified_msg_origin
        if umo not in self.monitored_targets:
            self.monitored_targets[umo] = {"steam_ids": [], "settings": None}
        
        if TEST_USER_STEAM_ID not in self.monitored_targets[umo].get("steam_ids", []):
            self.monitored_targets[umo].setdefault("steam_ids", []).append(TEST_USER_STEAM_ID)
            self.config["monitored_targets"] = json.dumps(self.monitored_targets, indent=2, ensure_ascii=False)
            self.config.save_config()
            await event.send(event.plain_result(f"已临时将测试用户加入当前会话监控列表。" ))

    @filter.command("steam test status")
    async def steam_test_status(self, event: AstrMessageEvent):
        """测试状态变更：游戏中 -> 在线"""
        await self._setup_test_user(event)
        # 将模拟状态从“游戏中”变为“在线”
        self.test_user_mock_state = {
            "personaname": "测试ID", "personastate": 1, "gameid": None, "gameextrainfo": None
        }
        yield event.plain_result("测试命令已触发：测试用户状态已变为“在线”。请等待下一轮【状态检查】循环以查看推送效果。" )
        asyncio.create_task(self._reset_test_user_delayed())

    @filter.command("steam test achievements")
    async def steam_test_achievements(self, event: AstrMessageEvent):
        """测试成就变更：随机增加一个成就"""
        await self._setup_test_user(event)
        app_id = TEST_USER_INITIAL_STATE["gameid"]
        schema = await self.get_achievement_schema(app_id)
        if not schema:
            yield event.plain_result("无法获取测试游戏的成就纲要，测试失败。" )
            return

        # 从主数据中获取上次的成就，以决定可以添加哪个新成就
        last_known_achs = set(self.last_achievements.get(TEST_USER_STEAM_ID, {}).get(app_id, []))
        all_schema_achs = set(schema.keys())
        unlocked_achs = all_schema_achs - last_known_achs
        
        if not unlocked_achs:
            # 如果全成就了，为了能继续测试，就重置成就
            self.last_achievements[TEST_USER_STEAM_ID][app_id] = []
            self.test_user_mock_achievements[app_id] = []
            last_known_achs = set()
            unlocked_achs = all_schema_achs
            await event.send(event.plain_result("测试用户已全成就，现已重置其成就列表以便测试。" ))

        new_ach_name = random.choice(list(unlocked_achs))
        
        # 更新模拟器的“下一次API返回”状态
        # 确保 self.test_user_mock_achievements 是基于 self.last_achievements 的状态来更新的
        new_ach_list = list(last_known_achs)
        new_ach_list.append(new_ach_name)
        self.test_user_mock_achievements[app_id] = new_ach_list
        
        yield event.plain_result(f"测试命令已触发：为测试用户在 Cyberpunk 2077 中添加了新成就“{schema[new_ach_name]['displayName']}”。请等待下一轮【成就检查】循环。" )

    @filter.command("steam test offline")
    async def steam_test_offline(self, event: AstrMessageEvent):
        """测试状态变更：游戏中 -> 离线"""
        await self._setup_test_user(event)
        # 将模拟状态从“游戏中”变为“离线”
        self.test_user_mock_state = {
            "personaname": "测试ID", "personastate": 0, "gameid": None, "gameextrainfo": None
        }
        yield event.plain_result("测试命令已触发：测试用户状态已变为“离线”。请等待下一轮【状态检查】循环以查看推送效果。" )
        asyncio.create_task(self._reset_test_user_delayed())

    async def _reset_test_user_delayed(self):
        """延迟重置测试用户状态"""
        # 等待足够长的时间，确保监控循环已经处理了模拟状态
        await asyncio.sleep(self.status_poll_interval + 5)
        
        # 重置下一次API调用将返回的模拟状态
        self.test_user_mock_state = TEST_USER_INITIAL_STATE.copy()
        
        # 同时也重置主循环中的“上一次”状态记录，以防万一
        self.last_states[TEST_USER_STEAM_ID] = TEST_USER_INITIAL_STATE.copy()
        
        logger.info(f"测试用户 {TEST_USER_STEAM_ID} 的模拟状态已自动恢复。" )

    async def terminate(self):
        """插件终止时调用的清理函数"""
        if self.status_monitor_task:
            self.status_monitor_task.cancel()
        if self.achievement_monitor_task:
            self.achievement_monitor_task.cancel()
        
        # 保存所有数据
        self._save_data(self.last_states_path, self.last_states)
        self._save_data(self.last_achievements_path, self.last_achievements)
        self._save_data(self.game_cache_path, self.game_cache)
        self._save_data(self.achievement_schema_path, self.achievement_schema)
        
        logger.info("Steam 监控插件已停止并保存了所有数据。" )