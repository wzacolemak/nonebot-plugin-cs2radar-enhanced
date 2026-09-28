from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from nonebot import require

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import html_to_pic

from .security import inject_csp, safe_image_url

TEMPLATE_PATH = Path(__file__).parent / "templates"
WMPVP_REFERER = "https://www.wmpvp.com/"
env = Environment(
    loader=FileSystemLoader(TEMPLATE_PATH),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
)
env.filters["safe_image_url"] = safe_image_url


def rating_color(value: Any) -> str:
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return "#ffffff"
    if v == 0:
        return "#ffffff"
    if v >= 1.1:
        return "#4ade80"
    if v <= 0.9:
        return "#f87171"
    return "#ffffff"


env.filters["rating_color"] = rating_color

def we_color(value: Any) -> str:
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return "#ffffff"
    if v == 0:
        return "#ffffff"
    if v > 8:
        return "#4ade80"
    if v < 8:
        return "#f87171"
    return "#ffffff"


env.filters["we_color"] = we_color


async def _secure_html_to_pic(html: str, *, width: int) -> bytes:
    return await html_to_pic(
        html=inject_csp(html),
        viewport={"width": width, "height": 10},
        extra_http_headers={"Referer": WMPVP_REFERER},
    )


def _nested_value(source: Any, path: str) -> Any:
    current = source
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _pick_int(sources: list[Any], *aliases: str) -> int:
    for alias in aliases:
        for source in sources:
            value = _nested_value(source, alias)
            if value not in (None, ""):
                try:
                    return int(float(value))
                except Exception:
                    continue
    return 0


def _pick_float(sources: list[Any], *aliases: str) -> float | None:
    for alias in aliases:
        for source in sources:
            value = _nested_value(source, alias)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    return None


def _build_highlight_summary(*sources: Any) -> dict[str, Any]:
    source_list = [source for source in sources if isinstance(source, dict)]
    kills_2 = _pick_int(source_list, "k2", "doubleKill", "double_kill", "twoKill", "two_kill", "multiKill2", "2k", "kill2", "2kill", "double_kill_total", "2_kill", "kill_2")
    kills_3 = _pick_int(source_list, "k3", "tripleKill", "threeKill", "three_kill", "multiKill3", "3k", "kill3", "3kill", "triple_kill_total", "3_kill", "kill_3")
    kills_4 = _pick_int(source_list, "k4", "quadraKill", "fourKill", "four_kill", "multiKill4", "4k", "kill4", "4kill", "quadra_kill_total", "4_kill", "kill_4")
    kills_5 = _pick_int(source_list, "k5", "pentaKill", "fiveKill", "five_kill", "ace", "multiKill5", "5k", "kill5", "5kill", "penta_kill_total", "5_kill", "kill_5")
    first_kills = _pick_int(source_list, "firstKill", "firstKills", "first_kill", "entryKill", "entryKills", "firstBlood", "first_kill_total")
    first_kill_ratio = _pick_float(source_list, "entryKillRatio", "firstKillRatio", "first_kill_ratio")
    if first_kills > 0:
        first_kill_label = "首杀"
        first_kill_value: int | str = first_kills
    elif first_kill_ratio is not None:
        ratio_percent = first_kill_ratio * 100 if first_kill_ratio <= 1 else first_kill_ratio
        first_kill_label = "首杀率"
        first_kill_value = f"{ratio_percent:.1f}%"
    else:
        first_kill_label = "首杀"
        first_kill_value = 0
    clutch_1v1 = _pick_int(source_list, "vs1", "clutch1", "clutch_1", "clutch1v1", "oneVOne", "v1_total", "1v1", "end_1v1")
    clutch_1v2 = _pick_int(source_list, "vs2", "clutch2", "clutch_2", "clutch1v2", "oneVTwo", "v2_total", "1v2", "end_1v2")
    clutch_1v3 = _pick_int(source_list, "vs3", "clutch3", "clutch_3", "clutch1v3", "oneVThree", "v3_total", "1v3", "end_1v3")
    clutch_1v4 = _pick_int(source_list, "vs4", "clutch4", "clutch_4", "clutch1v4", "oneVFour", "v4_total", "1v4", "end_1v4")
    clutch_1v5 = _pick_int(source_list, "vs5", "clutch5", "clutch_5", "clutch1v5", "oneVFive", "v5_total", "1v5", "end_1v5")
    multi_kills = _pick_int(source_list, "multiKill", "multi_kill", "multiKills", "manyKill", "multi_kill_total")
    if multi_kills <= 0:
        multi_kills = kills_2 + kills_3 + kills_4 + kills_5
    clutch_wins = _pick_int(source_list, "clutchWin", "clutchWins", "endGameWin", "1vN_win_total", "1vN_win", "end_total")
    if clutch_wins <= 0:
        clutch_wins = clutch_1v1 + clutch_1v2 + clutch_1v3 + clutch_1v4 + clutch_1v5
    return {
        "first_kills": first_kills,
        "multi_kills": multi_kills,
        "clutch_wins": clutch_wins,
        "summary_cards": [
            {"label": first_kill_label, "value": first_kill_value},
            {"label": "多杀", "value": multi_kills},
            {"label": "残局", "value": clutch_wins},
            {"label": "2K/3K/4K/5K", "value": f"{kills_2}/{kills_3}/{kills_4}/{kills_5}"},
        ],
        "clutch_cards": [
            {"label": "1v1", "value": clutch_1v1},
            {"label": "1v2", "value": clutch_1v2},
            {"label": "1v3", "value": clutch_1v3},
            {"label": "1v4", "value": clutch_1v4},
            {"label": "1v5", "value": clutch_1v5},
        ],
    }


async def render_events_card(events: list[dict[str, Any]]) -> bytes:
    template = env.get_template("events.html")
    processed_events = []
    for event in events:
        title = event.get("title", "未知赛事")
        if title == "进行中" and event.get("status"):
            title = event.get("status")
            status = "进行中"
        else:
            status = event.get("status", "未开始")

        processed_events.append(
            {
                "title": title,
                "level": event.get("level", "A级"),
                "status": status,
                "time": event.get("time", ""),
                "location": event.get("location", ""),
                "prize": event.get("prize", ""),
                "teams": event.get("teams", ""),
            }
        )

    html_content = template.render(
        events=processed_events,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return await _secure_html_to_pic(html_content, width=800)


async def render_matches_card(matches: list[dict[str, Any]]) -> bytes:
    template = env.get_template("matches.html")
    html_content = template.render(
        matches=matches[:15],
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return await _secure_html_to_pic(html_content, width=800)


async def render_results_card(results: list[dict[str, Any]]) -> bytes:
    template = env.get_template("match_results.html")
    html_content = template.render(
        results=results[:20],
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return await _secure_html_to_pic(html_content, width=800)


async def render_stats_card(data: dict) -> bytes:
    template = env.get_template("stats.html")
    stats = data.get("stats", {})
    combat = _build_highlight_summary(stats, stats.get("career", {}), stats.get("best_season", {}), stats.get("home", {}), (stats.get("home", {}) or {}).get("season_data", {}) or {})
    html_content = template.render(
        nickname=data.get("nickname", "Unknown"),
        avatar=data.get("avatar", ""),
        stats=stats,
        combat=combat,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return await _secure_html_to_pic(html_content, width=640)


async def render_player_detail(player_data: dict) -> bytes:
    template = env.get_template("player_detail.html")
    hltv_id = player_data.get("hltv_id")
    name = player_data.get("name", "player")
    hltv_link = f"https://www.hltv.org/player/{hltv_id}/{name}" if hltv_id else None

    html_content = template.render(
        player=player_data,
        hltv_link=hltv_link,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return await _secure_html_to_pic(html_content, width=800)


async def render_pw_stats_card(player_data: dict) -> bytes:
    template = env.get_template("pw_stats.html")
    combat = _build_highlight_summary(player_data.get("stats", {}))
    html_content = template.render(
        player=player_data,
        combat=combat,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return await _secure_html_to_pic(html_content, width=640)


def _official_history_to_pw_card_data(
    matches: list[Any], profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Aggregate recent official matches into the same view model used by /pw."""
    if not matches:
        raise ValueError("官匹历史战绩为空")

    profile = profile or {}
    profile_summary = profile.get("summary", {}) if isinstance(profile, dict) else {}
    players = [match.player for match in matches]
    count = len(matches)
    wins = sum(match.result_text == "胜利" for match in matches)
    draws = sum(match.result_text == "平局" for match in matches)
    losses = count - wins - draws
    total_kills = sum(player.kill for player in players)
    total_deaths = sum(player.death for player in players)

    def average(attribute: str) -> float:
        return sum(float(getattr(player, attribute, 0) or 0) for player in players) / count

    highlight_names = (
        "first_kills",
        "multi_kills",
        "clutch_wins",
        "kills_2",
        "kills_3",
        "kills_4",
        "kills_5",
        "clutch_1v1",
        "clutch_1v2",
        "clutch_1v3",
        "clutch_1v4",
        "clutch_1v5",
    )
    highlights = {
        name: sum(int(getattr(player.highlights, name, 0) or 0) for player in players)
        for name in highlight_names
    }

    map_stats: dict[str, dict[str, Any]] = {}
    for match in matches:
        item = map_stats.setdefault(
            match.map_name,
            {"mapName": match.map_name, "winCount": 0, "totalMatch": 0},
        )
        item["totalMatch"] += 1
        if match.result_text == "胜利":
            item["winCount"] += 1
    hot_maps = sorted(
        map_stats.values(),
        key=lambda item: (item["totalMatch"], item["winCount"]),
        reverse=True,
    )

    recent_matches = []
    for match in matches:
        if match.result_text == "胜利":
            team, win_team = 1, 1
        elif match.result_text == "失败":
            team, win_team = 1, 2
        else:
            team, win_team = 1, 0
        recent_matches.append(
            {
                "team": team,
                "winTeam": win_team,
                "score1": match.score_our,
                "score2": match.score_enemy,
                "mapName": match.map_name,
                "mode": match.match_type,
                "startTime": datetime.fromtimestamp(match.start_time).strftime("%Y-%m-%d %H:%M:%S"),
                "pwRating": match.player.rating,
                "rating": match.player.rating,
                "kill": match.player.kill,
                "death": match.player.death,
                "we": match.player.rws,
            }
        )

    average_rating = average("rating")
    average_adr = average("adr")
    average_rws = average("rws")
    steam_id = str(profile_summary.get("steamId") or players[0].uuid or "未知")
    nickname = str(profile_summary.get("nickname") or players[0].name or steam_id)
    return {
        "platform_label": "官匹",
        "summary": {
            "nickname": nickname,
            "avatarUrl": profile_summary.get("avatarUrl") or "",
            "steamId": steam_id,
            "description": f"最近 {count} 场：{wins} 胜 {draws} 平 {losses} 负",
            "rankItems": [
                {"label": "最近场次", "value": count},
                {"label": "胜 / 平 / 负", "value": f"{wins}/{draws}/{losses}"},
                {"label": "平均 Rating", "value": f"{average_rating:.2f}"},
            ],
            "statsDescription": (
                f"场均击杀 {total_kills / count:.1f} / 场均死亡 {total_deaths / count:.1f} / "
                f"平均 ADR {average_adr:.1f} / 平均 RWS {average_rws:.2f}"
            ),
            "recentTitle": f"最近 {count} 场官匹",
        },
        "stats": {
            "seasonId": "官匹历史",
            "pvpRank": 0,
            "pvpScore": 0,
            "winRate": wins / count,
            "pwRating": average_rating,
            "rating": average_rating,
            "adr": average_adr,
            "rws": average_rws,
            "mvpCount": 0,
            "kd": total_kills / total_deaths if total_deaths else total_kills,
            "headShotRatio": average("headshot_rate"),
            "cnt": count,
            "avgWe": average_rws,
            "kills": total_kills,
            "assists": 0,
            "deaths": total_deaths,
            "entryKillRatio": highlights["first_kills"] / max(total_kills, 1),
            "firstKill": highlights["first_kills"],
            "multiKill": highlights["multi_kills"],
            "clutchWin": highlights["clutch_wins"],
            "k2": highlights["kills_2"],
            "k3": highlights["kills_3"],
            "k4": highlights["kills_4"],
            "k5": highlights["kills_5"],
            "vs1": highlights["clutch_1v1"],
            "vs2": highlights["clutch_1v2"],
            "vs3": highlights["clutch_1v3"],
            "vs4": highlights["clutch_1v4"],
            "vs5": highlights["clutch_1v5"],
            "hotMaps": hot_maps,
            "hotWeapons": [],
            "hotWeapons2": [],
        },
        "recent_matches": recent_matches,
    }


async def render_official_history_card(
    matches: list[Any], profile: dict[str, Any] | None = None
) -> bytes:
    return await render_pw_stats_card(_official_history_to_pw_card_data(matches, profile))


async def render_match_detail_card(view_data: dict) -> bytes:
    template = env.get_template("match_detail.html")
    html_content = template.render(
        data=view_data,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    return await _secure_html_to_pic(html_content, width=960)


def _col_5e(m: dict, idx: int) -> dict:
    try:
        time_str = datetime.fromtimestamp(int(m.get("start_time") or 0)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        time_str = str(m.get("start_time") or "")
    map_name = str(m.get("map") or "").replace("de_", "").upper() or "未知地图"
    if m.get("is_win"):
        result, result_color, border_cls = "胜", "#4ade80", "win"
    elif m.get("is_tie"):
        result, result_color, border_cls = "平", "#fbbf24", "tie"
    else:
        result, result_color, border_cls = "负", "#f87171", "lose"
    rating_val = m.get("rating")
    try:
        rating_str = f"{float(rating_val):.2f}"
    except (TypeError, ValueError):
        rating_str = "--"
    try:
        elo_v = float(str(m.get("change_elo")))
        elo_line = f"ELO {elo_v:+.2f}"
    except (TypeError, ValueError):
        elo_line = None
    multi = "/".join(str(m.get(f"kill_{i}", 0) or 0) for i in (2, 3, 4, 5))
    minor = []
    if elo_line:
        minor.append(elo_line)
    minor.append(f"2K/3K/4K/5K {multi}")
    badges = [b for b, flag in (("MVP", m.get("is_mvp")), ("SVP", m.get("is_svp")),
                                ("首杀王", m.get("is_most_first_kill")), ("残局王", m.get("is_most_end"))) if flag]
    return {
        "map_name": map_name, "time_str": time_str,
        "result": result, "result_color": result_color, "border_cls": border_cls,
        "score": f"{m.get('group1_all_score', 0) or 0} : {m.get('group2_all_score', 0) or 0}",
        "rating_str": rating_str, "rating_color": rating_color(rating_val),
        "rows": [
            {"k": "K/D", "v": f"{m.get('kill', '?')} / {m.get('death', '?')}", "color": "#ffffff"},
            {"k": "ADR", "v": str(m.get("adr") or "--"), "color": "#ffffff"},
            {"k": "RWS", "v": str(m.get("rws") or "--"), "color": we_color(m.get("rws"))},
        ],
        "minor": minor, "badges": badges,
    }


def _col_pw(m: dict, idx: int) -> dict:
    st = m.get("startTime")
    try:
        time_str = datetime.fromtimestamp(int(st)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        time_str = str(st or "")
    map_name = str(m.get("mapName") or "").upper() or "未知地图"
    team, win_team = m.get("team"), m.get("winTeam")
    if win_team == 0:
        result, result_color, border_cls = "平", "#fbbf24", "tie"
    elif team == win_team:
        result, result_color, border_cls = "胜", "#4ade80", "win"
    else:
        result, result_color, border_cls = "负", "#f87171", "lose"
    rating_val = m.get("pwRating") if m.get("pwRating") is not None else m.get("rating")
    try:
        rating_str = f"{float(rating_val):.2f}"
    except (TypeError, ValueError):
        rating_str = "--"
    try:
        we_str = f"{float(m.get('we') or 0):.2f}"
    except (TypeError, ValueError):
        we_str = "--"
    try:
        delta = int(m.get("pvpScoreChange") or 0)
        delta_line = f"天命分 {delta:+d}"
    except (TypeError, ValueError):
        delta_line = None
    minor = []
    if delta_line:
        minor.append(delta_line)
    minor.append(f"4K {m.get('k4', 0) or 0} · 5K {m.get('k5', 0) or 0}")
    badges = [f"MVPx{m.get('mvp')}"] if m.get("mvp") else []
    return {
        "map_name": map_name, "time_str": time_str,
        "result": result, "result_color": result_color, "border_cls": border_cls,
        "score": f"{m.get('score1', 0) or 0} : {m.get('score2', 0) or 0}",
        "rating_str": rating_str, "rating_color": rating_color(rating_val),
        "rows": [
            {"k": "K/D", "v": f"{m.get('kill', 0) or 0} / {m.get('death', 0) or 0}", "color": "#ffffff"},
            {"k": "WE", "v": we_str, "color": we_color(m.get("we"))},
        ],
        "minor": minor, "badges": badges,
    }


async def render_match_columns(matches: list, platform: str, start_index: int, total: int | None = None) -> bytes:
    """渲染计分板: 每张图最多10场, 每场一列, 补充数据小字放列底部"""
    builder = _col_5e if platform == "5e" else _col_pw
    cols = [builder(m, i) for i, m in enumerate(matches, start_index)]
    template = env.get_template("match_columns.html")
    html_content = template.render(
        cols=cols,
        platform="5E" if platform == "5e" else "完美",
        start_index=start_index,
        end_index=start_index + len(cols) - 1,
        total=total if total is not None else end_index_of(cols, start_index),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return await _secure_html_to_pic(html_content, width=1280)


def end_index_of(cols: list, start_index: int) -> int:
    return start_index + len(cols) - 1
