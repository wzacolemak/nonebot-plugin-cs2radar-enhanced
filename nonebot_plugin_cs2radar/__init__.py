import asyncio
import html
import re
from datetime import datetime
from functools import wraps
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from nonebot import get_plugin_config, logger, on_command, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.exception import FinishedException, MatcherException
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")

from .binding_store import BindingStore
from .config import Config
from .crawler import FiveECrawler, FiveEEventCrawler, PWCrawler
from .llm import LLMEvaluator
from .match_service import MatchService, parse_bind_args, parse_match_args
from .renderer import (
    render_events_card,
    render_match_detail_card,
    render_matches_card,
    render_official_history_card,
    render_player_detail,
    render_pw_stats_card,
    render_results_card,
    render_stats_card,
)
from .security import CommandGuard, GuardRejected
from .storage import get_bind_db_path

__version__ = "0.1.3"

plugin_config = get_plugin_config(Config)

for legacy_name in (
    "cs_pro_priority",
    "cs_pro_bind_db_path",
    "cs_pro_http_timeout",
    "cs_pro_llm_enabled",
    "cs_pro_llm_api_type",
    "cs_pro_llm_api_url",
    "cs_pro_llm_api_key",
    "cs_pro_llm_model",
    "cs_pro_llm_backup_enabled",
    "cs_pro_llm_backup_api_type",
    "cs_pro_llm_backup_api_url",
    "cs_pro_llm_backup_api_key",
    "cs_pro_llm_backup_model",
    "cs_pro_llm_timeout",
    "cs_pro_llm_system_prompt",
):
    if getattr(plugin_config, legacy_name, None) is not None:
        status = "enabled by explicit opt-in" if plugin_config.cs2radar_allow_legacy_config else "ignored by default"
        logger.warning(
            f"[nonebot_plugin_cs2radar] `{legacy_name}` is deprecated and {status}; "
            "migrate to `cs2radar_*` config names."
        )

__plugin_meta__ = PluginMetadata(
    name="CS2 Radar",
    description="CS2 赛事、选手、5E/完美/官匹战绩查询与详细对局分析",
    usage=(
        "cs查询 [选手]\n"
        "cs赛事\n"
        "赛果\n"
        "5e [ID/昵称]\n"
        "pw [ID/昵称/Steam自定义ID] [场数，默认5]\n"
        "pwlogin [手机号] [验证码]\n"
        "官匹 [SteamID64/自定义ID] [场数，默认5]\n"
        "bind [platform] [name]\n"
        "match [platform] [@群友] [round]"
    ),
    type="application",
    homepage="https://github.com/wzacolemak/nonebot-plugin-cs2radar-enhanced",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={
        "author": "luojisama / wzacolemak",
        "version": __version__,
        "pypi": "nonebot-plugin-cs2radar",
    },
)

# Commands
cs_search = on_command("cs查询", aliases={"cs选手", "csplayer"}, priority=plugin_config.priority, block=True)
game_search = on_command("cs赛事", aliases={"赛事", "csgo赛事", "cs2赛事"}, priority=plugin_config.priority, block=True)
result_search = on_command("赛果", aliases={"cs赛果", "赛事赛果"}, priority=plugin_config.priority, block=True)
five_e_stats = on_command("5e", aliases={"5e战绩", "5e查询", "cs战绩"}, priority=plugin_config.priority, block=True)
pw_stats = on_command("pw", aliases={"pw战绩", "pw查询", "完美战绩"}, priority=plugin_config.priority, block=True)
pw_login = on_command(
    "pwlogin",
    aliases={"完美登录"},
    priority=plugin_config.priority,
    block=True,
    permission=SUPERUSER,
)
official_match = on_command(
    "官匹",
    aliases={"官匹查询", "mm"},
    priority=plugin_config.priority,
    block=True,
)
bind_cmd = on_command("bind", aliases={"绑定", "添加", "绑定用户", "添加用户"}, priority=plugin_config.priority, block=True)
match_cmd = on_command("match", aliases={"战绩", "查询战绩"}, priority=plugin_config.priority, block=True)

# Shared services
store = BindingStore(str(get_bind_db_path(plugin_config.bind_db_path)))
event_crawler = FiveEEventCrawler()
five_e_crawler = FiveECrawler()
pw_crawler = PWCrawler(
    token=plugin_config.cs2radar_pw_token or "",
    steam_id=plugin_config.cs2radar_pw_steam_id or 0,
    persist_session=plugin_config.cs2radar_pw_session_persist,
)
match_service = MatchService(
    timeout=plugin_config.http_timeout,
    pw_session_provider=pw_crawler.get_session,
)
command_guard = CommandGuard(
    max_concurrency=plugin_config.cs2radar_max_concurrency,
    cooldown_seconds=plugin_config.cs2radar_cooldown_seconds,
)
_llm_api_key = (plugin_config.llm_api_key or "").strip()
_llm_api_type = plugin_config.llm_api_type
_llm_api_url = plugin_config.llm_api_url
_llm_model = plugin_config.llm_model
_llm_backup_enabled = plugin_config.llm_backup_enabled
_llm_backup_api_key = (plugin_config.llm_backup_api_key or "").strip()
_llm_backup_api_type = plugin_config.llm_backup_api_type
_llm_backup_api_url = plugin_config.llm_backup_api_url
_llm_backup_model = plugin_config.llm_backup_model
llm = LLMEvaluator(
    enabled=plugin_config.llm_enabled,
    api_type=_llm_api_type,
    api_url=_llm_api_url,
    api_key=_llm_api_key,
    model=_llm_model,
    backup_enabled=_llm_backup_enabled,
    backup_api_type=_llm_backup_api_type,
    backup_api_url=_llm_backup_api_url,
    backup_api_key=_llm_backup_api_key,
    backup_model=_llm_backup_model,
    timeout=plugin_config.llm_timeout,
    system_prompt=plugin_config.llm_system_prompt,
)


def _guarded(matcher: Any, command_name: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            event = kwargs.get("event")
            if event is None:
                event = next((item for item in args if isinstance(item, MessageEvent)), None)
            if event is None:
                logger.error(f"[nonebot_plugin_cs2radar] missing MessageEvent for guarded command {command_name}")
                await matcher.finish("命令上下文异常，请联系管理员。")
                return None
            try:
                async with command_guard.acquire(str(event.user_id), command_name):
                    return await func(*args, **kwargs)
            except GuardRejected as exc:
                await matcher.finish(str(exc))

        return wrapper

    return decorator


def _extract_target_qq(bot: Bot, event: MessageEvent) -> str:
    target = str(event.user_id)
    for seg in event.message:
        if seg.type == "at":
            qq = str(seg.data.get("qq") or "")
            if qq and qq != str(bot.self_id):
                target = qq
                break
    return target


def _platform_theme(platform: str) -> tuple[str, str, str]:
    if platform == "5e":
        return "5E平台", "#f74d4d", "#f78c00"
    if platform == "mm":
        return "官匹", "#2db3ff", "#3b82f6"
    return "完美平台", "#2db3ff", "#06b6d4"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


async def _resolve_steam_id64(raw: str) -> str:
    value = str(raw or "").strip()
    if re.fullmatch(r"7656119\d{10}", value):
        return value

    identifier = value
    if value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}:
            raise ValueError("仅支持 Steam Community 个人主页链接。")
        profile_match = re.fullmatch(r"/profiles/(7656119\d{10})/?", parsed.path)
        if profile_match:
            return profile_match.group(1)
        vanity_match = re.fullmatch(r"/id/([^/]+)/?", parsed.path)
        if not vanity_match:
            raise ValueError("Steam 个人主页链接格式不正确。")
        identifier = vanity_match.group(1)

    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", identifier):
        raise ValueError("请输入 SteamID64、自定义ID或 Steam 个人主页链接。")

    url = f"https://steamid.io/lookup/{quote(identifier, safe='')}"
    async with httpx.AsyncClient(timeout=plugin_config.http_timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    match = re.search(r"7656119\d{10}", response.text)
    if not match:
        raise ValueError(f"无法解析 Steam 自定义ID：{identifier}")
    return match.group(0)


def _build_match_view_data(match_data, llm_title: str, llm_detail: str) -> dict:
    platform_label, color_a, color_b = _platform_theme(match_data.platform)

    def _highlight_view(highlights):
        return {
            "first_kills": highlights.first_kills,
            "multi_kills": highlights.multi_kills,
            "clutch_wins": highlights.clutch_wins,
            "summary_cards": [
                {"label": "首杀", "value": highlights.first_kills},
                {"label": "多杀", "value": highlights.multi_kills},
                {"label": "残局", "value": highlights.clutch_wins},
                {"label": "2K/3K/4K/5K", "value": f"{highlights.kills_2}/{highlights.kills_3}/{highlights.kills_4}/{highlights.kills_5}"},
            ],
            "clutch_cards": [
                {"label": "1v1", "value": highlights.clutch_1v1},
                {"label": "1v2", "value": highlights.clutch_1v2},
                {"label": "1v3", "value": highlights.clutch_1v3},
                {"label": "1v4", "value": highlights.clutch_1v4},
                {"label": "1v5", "value": highlights.clutch_1v5},
            ],
        }

    def _p(p):
        return {
            "name": p.name,
            "rating": f"{p.rating:.2f}",
            "adr": f"{p.adr:.1f}",
            "kill": p.kill,
            "death": p.death,
            "hs": _fmt_pct(p.headshot_rate),
            "elo": f"{p.elo_change:+.1f}",
            "rws": f"{p.rws:.2f}",
            "uuid": p.uuid,
            "highlights": _highlight_view(p.highlights),
        }

    def _round_view(item):
        result_map = {"W": ("胜", "win"), "L": ("负", "loss")}
        label, css = result_map.get(item.result, ("?", "unknown"))
        return {
            "no": item.round_no,
            "result": item.result,
            "result_label": label,
            "result_class": css,
            "side": item.side or "",
            "score_after": item.score_after or "",
        }

    def _segment_view(segment):
        return {
            "key": segment.key,
            "label": segment.label,
            "our_score": segment.our_score,
            "enemy_score": segment.enemy_score,
            "score_text": f"{segment.our_score}:{segment.enemy_score}",
            "rounds": [_round_view(x) for x in segment.rounds],
        }

    start_at = datetime.fromtimestamp(match_data.start_time).strftime("%Y-%m-%d %H:%M:%S")
    all_teammates = [match_data.player] + match_data.teammates
    all_teammates.sort(key=lambda x: x.rating, reverse=True)
    halves = [_segment_view(x) for x in match_data.halves]
    half_summary = " / ".join(f"{item['label']} {item['score_text']}" for item in halves) or "暂无"
    player_view = _p(match_data.player)

    return {
        "platform_label": platform_label,
        "theme_a": color_a,
        "theme_b": color_b,
        "map_name": match_data.map_name,
        "match_type": match_data.match_type or "未知模式",
        "start_at": start_at,
        "duration_min": match_data.duration_min,
        "result_text": match_data.result_text,
        "result_class": "good" if match_data.result_text == "胜利" else ("draw" if match_data.result_text == "平局" else "bad"),
        "match_id": match_data.match_id,
        "score_text": f"{match_data.score_our}:{match_data.score_enemy}",
        "half_summary": half_summary,
        "halves": halves,
        "has_rounds": any(item["rounds"] for item in halves),
        "has_overtime": match_data.has_overtime,
        "player": player_view,
        "player_highlights": player_view["highlights"],
        "teammates": [_p(x) for x in all_teammates],
        "opponents": [_p(x) for x in match_data.opponents],
        "llm_title": llm_title,
        "llm_detail": llm_detail,
    }


async def _render_match_image(match_data) -> bytes:
    llm_title = "评价暂不可用"
    llm_detail = "未配置或调用失败，本次仅展示战绩数据。"
    try:
        result = await llm.evaluate(match_data.llm_context())
        if result:
            llm_title = result.title
            llm_detail = result.detail
    except Exception as e:
        logger.warning(f"[cs_pro] llm evaluate failed: {e}")

    view_data = _build_match_view_data(match_data, llm_title, llm_detail)
    return await render_match_detail_card(view_data)


@bind_cmd.handle()
@_guarded(bind_cmd, "bind")
async def handle_bind(event: MessageEvent, args: Message = CommandArg()):
    raw = args.extract_plain_text().strip()
    if not raw:
        await bind_cmd.finish("用法: /bind [5e|pw] [玩家名]")
    if len(raw) > 128:
        await bind_cmd.finish("绑定参数过长。")

    try:
        default_platform = store.get_default_platform(str(event.user_id))
        platform, name = parse_bind_args(raw, default_platform=default_platform)

        # 5E绑定复用 /5e 查询规则：ID/域名直接绑定，昵称走 search_player 首条匹配。
        if platform == "5e":
            is_id = re.match(r"^\d+s\w+$|^\d+$|^[0-9a-f-]{36}$", name)
            if is_id:
                bound = await match_service.bind_5e_domain(store, str(event.user_id), name, canonical_name=name)
            else:
                candidates = await five_e_crawler.search_player(name)
                if not candidates:
                    await bind_cmd.finish(f"绑定失败: 未找到5E玩家 {name}")
                first = candidates[0]
                domain = str(first.get("domain") or "").strip()
                if not domain:
                    await bind_cmd.finish("绑定失败: 5E搜索结果缺少domain")
                canonical = str(first.get("name") or name)
                bound = await match_service.bind_5e_domain(store, str(event.user_id), domain, canonical_name=canonical)
        else:
            bound = await match_service.bind_player(store, str(event.user_id), platform, name)
    except Exception as e:
        logger.exception(f"[nonebot_plugin_cs2radar] bind failed: {e}")
        await bind_cmd.finish("绑定失败，请稍后重试。")

    if bound.platform == "pw" and (not bound.domain or not bound.uuid):
        await bind_cmd.finish(
            f"绑定成功\n平台: {bound.platform}\n玩家: {bound.player_name}\n"
            "已按用户名绑定，将在首次查询官匹/完美战绩时自动补全平台ID与SteamID。"
        )
    await bind_cmd.finish(
        f"绑定成功\n平台: {bound.platform}\n玩家: {bound.player_name}\n平台ID: {bound.domain}\nSteamID: {bound.uuid}"
    )


@match_cmd.handle()
@_guarded(match_cmd, "match")
async def handle_match(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    raw = args.extract_plain_text().strip()
    if len(raw) > 128:
        await match_cmd.finish("查询参数过长。")
    platform, round_index = parse_match_args(raw)
    target_qq = _extract_target_qq(bot, event)
    if target_qq != str(event.user_id) and not plugin_config.cs2radar_allow_query_others:
        await match_cmd.finish("出于隐私保护，当前不允许查询其他群成员的绑定战绩。")

    await match_cmd.send("正在查询详细战绩并生成评价...")
    try:
        match_data = await match_service.fetch_match(store, target_qq, platform, round_index)
    except Exception as e:
        logger.exception(f"[nonebot_plugin_cs2radar] match query failed: {e}")
        await match_cmd.finish("查询失败，请稍后重试。")

    image_bytes = await _render_match_image(match_data)
    await match_cmd.finish(MessageSegment.image(image_bytes))


@official_match.handle()
@_guarded(official_match, "official_match")
async def handle_official_match(event: MessageEvent, args: Message = CommandArg()):
    tokens = args.extract_plain_text().strip().split()
    if not tokens or len(tokens) > 2:
        await official_match.finish(
            "用法: /官匹 <SteamID64/自定义ID> [场数]，例如: /官匹 colemakQWQ"
        )

    try:
        steam_id = await _resolve_steam_id64(tokens[0])
    except Exception as e:
        await official_match.finish(f"Steam 账号解析失败: {e}")

    match_count = 5
    if len(tokens) == 2:
        if not tokens[1].isdigit() or not 1 <= int(tokens[1]) <= 10:
            await official_match.finish("查询场数应在 1 到 10 之间。")
        match_count = int(tokens[1])

    await official_match.send(f"正在查询该玩家最近 {match_count} 场官匹并生成战绩图...")
    try:
        matches, profile = await asyncio.gather(
            match_service.fetch_official_recent_matches(steam_id, match_count),
            pw_crawler.get_player_data(steam_id, include_recent_matches=False),
        )
        if not isinstance(profile, dict) or "error" in profile:
            profile = {}
        image_bytes = await render_official_history_card(matches, profile)
    except ValueError as e:
        await official_match.finish(f"官匹查询失败: {e}")
    except Exception as e:
        logger.exception(f"[nonebot_plugin_cs2radar] official match query failed: {e}")
        await official_match.finish("官匹查询失败，请稍后重试。")

    await official_match.finish(MessageSegment.image(image_bytes))


@cs_search.handle()
@_guarded(cs_search, "cs_search")
async def handle_cs_search(event: MessageEvent, args: Message = CommandArg()):
    query = args.extract_plain_text().strip()
    if not query:
        await cs_search.finish("请输入选手名称，例如: cs查询 sh1ro")
    if len(query) > 64:
        await cs_search.finish("选手名称过长。")

    search_api = "https://api.viki.moe/pw-cs/search"
    async with httpx.AsyncClient(timeout=plugin_config.http_timeout) as client:
        try:
            resp = await client.get(search_api, params={"type": "player", "s": query})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.exception(f"[nonebot_plugin_cs2radar] player search failed: {e}")
            await cs_search.finish("查询出错，请稍后重试。")

    if not isinstance(data, list):
        logger.warning(f"[nonebot_plugin_cs2radar] unexpected player search response: {type(data).__name__}")
        await cs_search.finish("查询出错，请稍后重试。")
    if not data:
        await cs_search.finish("未找到相关选手，请检查名称")

    player_brief = data[0]
    hltv_id = player_brief.get("hltv_id")
    if not hltv_id:
        await cs_search.finish("未找到选手HLTV ID")

    detail_api = f"https://api.viki.moe/pw-cs/player/{hltv_id}"
    async with httpx.AsyncClient(timeout=plugin_config.http_timeout) as client:
        try:
            resp = await client.get(detail_api)
            resp.raise_for_status()
            player = resp.json()
        except Exception as e:
            logger.exception(f"[nonebot_plugin_cs2radar] player detail failed: {e}")
            await cs_search.finish("获取选手详情出错，请稍后重试。")

    try:
        image_bytes = await render_player_detail(player)
    except Exception as e:
        logger.error(f"Error rendering player detail: {e}")
        name = player.get("name", "未知")
        team_name = player.get("team", {}).get("name", "无战队")
        await cs_search.finish(f"选手: {name}\n战队: {team_name}\n(图片渲染失败)")

    await cs_search.finish(MessageSegment.image(image_bytes))


@game_search.handle()
@_guarded(game_search, "events")
async def handle_game_search(event: MessageEvent):
    await game_search.send("正在获取实时赛程与赛事信息...")
    try:
        matches = await event_crawler.get_matches()
        if matches:
            image_bytes = await render_matches_card(matches)
            await game_search.finish(MessageSegment.image(image_bytes))

        events = await event_crawler.get_events()
        if events:
            image_bytes = await render_events_card(events)
            await game_search.finish(MessageSegment.image(image_bytes))

        await game_search.finish("暂无实时赛程数据。")
    except (FinishedException, MatcherException):
        raise
    except Exception as e:
        logger.error(f"Error in game_search: {e}")
        await game_search.finish("查询赛事失败，请稍后重试。")


@result_search.handle()
@_guarded(result_search, "results")
async def handle_result_search(event: MessageEvent):
    await result_search.send("正在获取赛果数据...")
    try:
        results = await event_crawler.get_results()
        if not results:
            await result_search.finish("暂无赛果数据。")
        image_bytes = await render_results_card(results)
        await result_search.finish(MessageSegment.image(image_bytes))
    except (FinishedException, MatcherException):
        raise
    except Exception as e:
        logger.error(f"Error in result_search: {e}")
        await result_search.finish("查询赛果失败，请稍后重试。")


@five_e_stats.handle()
@_guarded(five_e_stats, "five_e")
async def handle_five_e_stats(event: MessageEvent, arg: Message = CommandArg()):
    input_str = arg.extract_plain_text().strip()
    if not input_str:
        await five_e_stats.finish("请输入5E玩家域名、ID或昵称，例如: /5e 15429443s91f72")
    match_count = 5
    input_parts = input_str.rsplit(maxsplit=1)
    if len(input_parts) == 2 and input_parts[1].isdigit():
        if not 1 <= int(input_parts[1]) <= 10:
            await five_e_stats.finish("查询场数应在 1 到 10 之间。")
        input_str = input_parts[0].strip()
        match_count = int(input_parts[1])
    if len(input_str) > 64:
        await five_e_stats.finish("玩家标识过长。")

    await five_e_stats.send(f"正在查询 5E 玩家 {input_str} 的最近 {match_count} 场...")
    domain = input_str

    try:
        is_id = re.match(r"^\d+s\w+$|^\d+$|^[0-9a-f-]{36}$", input_str)
        search_info = {}
        if not is_id:
            search_results = await five_e_crawler.search_player(input_str)
            if not search_results:
                await five_e_stats.finish(f"未找到昵称为 {input_str} 的玩家。")
            search_info = search_results[0]
            domain = search_info["domain"]
            await five_e_stats.send(f"匹配到玩家: {search_info['name']} ({domain})，正在获取详细战绩...")

        data = await five_e_crawler.get_player_data(domain, recent_match_count=match_count)
        if (not data.get("nickname") or data["nickname"] == "Unknown") and search_info.get("name"):
            data["nickname"] = search_info["name"]
        if (not data.get("avatar")) and search_info.get("avatar"):
            data["avatar"] = search_info["avatar"]

        if not data.get("stats") or not data["stats"].get("career"):
            await five_e_stats.finish(f"未找到玩家 {domain} 的有效战绩数据。")

        image_bytes = await render_stats_card(data)
        await five_e_stats.finish(MessageSegment.image(image_bytes))
    except (FinishedException, MatcherException):
        raise
    except Exception as e:
        logger.error(f"Error in five_e_stats: {e}")
        await five_e_stats.finish("5E 查询失败，请稍后重试。")


@pw_login.handle()
@_guarded(pw_login, "pw_login")
async def handle_pw_login(event: MessageEvent, arg: Message = CommandArg()):
    if not plugin_config.cs2radar_pw_login_enabled:
        await pw_login.finish("QQ 内登录默认关闭。请优先通过服务器环境变量配置完美平台 Session。")
    if getattr(event, "message_type", "") != "private":
        await pw_login.finish("安全原因，完美平台登录仅允许超级用户在私聊中执行。")
    args = arg.extract_plain_text().strip().split()
    if len(args) != 2:
        await pw_login.finish("请输入手机号和验证码，例如: /pwlogin 13800138000 123456")

    mobile, code = args
    if not re.fullmatch(r"1\d{10}", mobile) or not re.fullmatch(r"\d{4,8}", code):
        await pw_login.finish("手机号或验证码格式不正确。")
    await pw_login.send("正在尝试登录完美平台...")

    result = await pw_crawler.login(mobile, code)
    if "error" in result:
        await pw_login.finish(f"登录失败: {result['error']}")

    nickname = result.get("nickname", "未知")
    await pw_login.finish(f"登录成功，欢迎回来，{nickname}。Session 已更新。")


@pw_stats.handle()
@_guarded(pw_stats, "pw_stats")
async def handle_pw_stats(event: MessageEvent, arg: Message = CommandArg()):
    raw_input = arg.extract_plain_text().strip()
    if not raw_input:
        await pw_stats.finish("请输入完美平台玩家昵称或 SteamId，例如: /pw sh1ro")
    input_str = raw_input
    match_count = 5
    input_parts = raw_input.rsplit(maxsplit=1)
    if len(input_parts) == 2 and input_parts[1].isdigit():
        if not 1 <= int(input_parts[1]) <= 10:
            await pw_stats.finish("查询场数应在 1 到 10 之间。")
        input_str = input_parts[0].strip()
        match_count = int(input_parts[1])
    if len(input_str) > 64:
        await pw_stats.finish("玩家标识过长。")
    if not pw_crawler.has_session():
        await pw_stats.finish("请先使用 /pwlogin <手机号> <验证码> 登录完美平台后再查询。")

    await pw_stats.send(f"正在查询完美玩家 {input_str} 的赛季数据和最近 {match_count} 场...")

    try:
        is_steam_id = input_str.isdigit() and len(input_str) > 10
        target_steam_id = input_str
        search_info = {}

        if not is_steam_id:
            search_results = await pw_crawler.search_player(input_str)
            input_key = html.unescape(input_str).strip().casefold()
            exact_match = next(
                (
                    item
                    for item in search_results
                    if html.unescape(str(item.get("pvpNickName") or "")).strip().casefold() == input_key
                ),
                None,
            )
            if exact_match:
                search_info = exact_match
                target_steam_id = str(search_info["steamId"])
            else:
                try:
                    target_steam_id = await _resolve_steam_id64(input_str)
                except Exception:
                    if not search_results:
                        await pw_stats.finish(f"未找到昵称或 Steam 自定义ID为 {input_str} 的玩家。")
                    search_info = search_results[0]
                    target_steam_id = str(search_info["steamId"])

            if search_info:
                matched_name = html.unescape(str(search_info.get("pvpNickName") or "未知"))
                await pw_stats.send(f"匹配到玩家: {matched_name}，正在获取详细战绩...")
            else:
                await pw_stats.send(f"已解析 Steam 自定义ID: {target_steam_id}，正在获取详细战绩...")

        data = await pw_crawler.get_player_data(
            target_steam_id,
            recent_match_count=match_count,
        )
        if "error" in data:
            logger.warning(f"[nonebot_plugin_cs2radar] PW API returned an error: {data['error']}")
            await pw_stats.finish("查询完美战绩失败，请稍后重试。")
        if not data or not data.get("stats"):
            await pw_stats.finish(f"未找到玩家 {target_steam_id} 的有效战绩数据。")

        if not data.get("summary", {}).get("nickname"):
            data["summary"]["nickname"] = html.unescape(
                str(search_info.get("pvpNickName") or "Unknown")
            )
        else:
            data["summary"]["nickname"] = html.unescape(str(data["summary"]["nickname"]))
        if not data.get("summary", {}).get("avatarUrl"):
            data["summary"]["avatarUrl"] = search_info.get("pvpAvatar")
        actual_count = len(data.get("recent_matches") or [])
        data["summary"]["recentTitle"] = f"最近 {actual_count} 场比赛"

        image_bytes = await render_pw_stats_card(data)
        await pw_stats.finish(MessageSegment.image(image_bytes))
    except (FinishedException, MatcherException):
        raise
    except Exception as e:
        logger.error(f"Error in pw_stats: {e}")
        await pw_stats.finish("完美战绩查询失败，请稍后重试。")
