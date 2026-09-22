import asyncio
import json
import threading
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

import httpx
from nonebot import logger
from playwright.async_api import async_playwright

from .security import ensure_private_file, secure_write_json
from .storage import get_pw_session_path, migrate_legacy_file

FIVE_E_NETWORK_HOST_SUFFIXES = ("5eplay.com", "5eplaycdn.com")


def _is_allowed_5e_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in FIVE_E_NETWORK_HOST_SUFFIXES
    )


async def _route_5e_requests(route) -> None:
    parsed = urllib.parse.urlsplit(route.request.url)
    if parsed.scheme in {"data", "blob", "about"}:
        await route.continue_()
        return
    if parsed.scheme in {"https", "wss"} and parsed.hostname and _is_allowed_5e_host(parsed.hostname):
        await route.continue_()
        return
    await route.abort("blockedbyclient")


def _safe_score(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return int(text)

class FiveEEventCrawler:
    def __init__(self):
        self.events_url = "https://event.5eplay.com/csgo/events"
        self.matches_url = "https://event.5eplay.com/csgo/matches?grade=1%2C7%2C2%2C3%2C8%2C9"
        self.results_url = "https://event.5eplay.com/csgo/matches?grade=1%2C7%2C2%2C3%2C8%2C9&status=2"

    async def get_matches(self, click_results: bool = False) -> list[dict[str, Any]]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.route("**/*", _route_5e_requests)
            
            try:
                await page.goto(self.matches_url, wait_until="networkidle", timeout=30000)
                
                # If we need to get results, click the '赛果' tab and wait for data to load
                if click_results:
                    # Look for the exact text '赛果' and click it
                    await page.get_by_text("赛果", exact=True).click()
                    # Wait for network to settle after clicking
                    await page.wait_for_timeout(2000)
                    await page.wait_for_load_state("networkidle", timeout=10000)

                matches = await page.evaluate(r"""() => {
                    const results = [];
                    const matchBlocks = document.querySelectorAll('.match-item');
                    
                    matchBlocks.forEach(block => {
                        const date = block.querySelector('.match-time-title')?.innerText.trim() || "";
                        const rows = block.querySelectorAll('.match-item-row');
                        
                        rows.forEach(row => {
                            const time = row.querySelector('.match-time-star div')?.innerText.trim() || "";
                            const format = row.querySelector('.match-rule')?.innerText.trim() || "BO3";
                            
                            const teamDivs = row.querySelectorAll('.match-team .cp');
                            const team1 = {
                                name: teamDivs[0]?.querySelector('p')?.innerText.trim() || "TBD",
                                logo: teamDivs[0]?.querySelector('img')?.src || ""
                            };
                            const team2 = {
                                name: teamDivs[1]?.querySelector('p')?.innerText.trim() || "TBD",
                                logo: teamDivs[1]?.querySelector('img')?.src || ""
                            };
                            
                            const scoreDivs = row.querySelectorAll('.all-score-box .all-score div');
                            const score1 = scoreDivs[0]?.innerText.trim() || "--";
                            const score2 = scoreDivs[1]?.innerText.trim() || "--";
                            
                            const status = row.querySelector('.match-btn')?.innerText.trim() || "未开始";
                            const tournament = row.querySelector('.match-system .tleft .ellip')?.innerText.trim() || "";
                            const tournament_icon = row.querySelector('.match-system .tleft .ellip img')?.src || "";
                            
                            results.push({
                                date, time, format,
                                team1, team2,
                                score1, score2,
                                status, tournament, tournament_icon
                            });
                        });
                    });
                    return results;
                }""")
                return matches
            except Exception as e:
                logger.error(f"Error crawling 5E matches: {e}")
                return []
            finally:
                await browser.close()

    async def get_results(self) -> list[dict[str, Any]]:
        # Use click_results=True to simulate clicking the '赛果' tab
        matches = await self.get_matches(click_results=True)
        results: list[dict[str, Any]] = []
        today = datetime.now().date()
        cutoff_date = today - timedelta(days=5)

        for match in matches:
            status = str(match.get("status") or "").replace("赛前分析", "").strip()
            score1 = _safe_score(match.get("score1"))
            score2 = _safe_score(match.get("score2"))
            date_str = str(match.get("date") or "").strip()
            
            # Check date range (last 5 days)
            try:
                # Assuming date format is YYYY-MM-DD based on website example "2026-03-18"
                # Some dates might have suffix like "(今天)", need to strip
                clean_date = date_str.split('(')[0].strip()
                match_date = datetime.strptime(clean_date, "%Y-%m-%d").date()
                if match_date < cutoff_date:
                    continue
            except ValueError:
                # If date parsing fails, we might want to keep it or log it
                # For safety in strict filtering, let's skip or keep based on policy
                # Here we keep it if it looks like a date we can't parse, or skip?
                # Given strict "last 5 days" requirement, skipping invalid dates is safer
                pass

            if score1 is None or score2 is None:
                continue
            if any(key in status for key in ("进行中", "未开始", "即将", "直播中")):
                continue
            winner_side = 0
            winner_name = "平局"
            if score1 > score2:
                winner_side = 1
                winner_name = str(match.get("team1", {}).get("name") or "")
            elif score2 > score1:
                winner_side = 2
                winner_name = str(match.get("team2", {}).get("name") or "")
            results.append(
                {
                    **match,
                    "status": status or "已结束",
                    "score1": str(score1),
                    "score2": str(score2),
                    "winner_side": winner_side,
                    "winner_name": winner_name,
                }
            )
        return results

    async def get_events(self) -> list[dict[str, Any]]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.route("**/*", _route_5e_requests)
            
            try:
                await page.goto(self.events_url, wait_until="networkidle", timeout=30000)
                
                # Extract events using a more precise script
                events = await page.evaluate(r"""() => {
                    const results = [];
                    
                    // 5EPlay event items are usually in .tournament-item or similar
                    // Let's find all blocks that contain '级赛事'
                    const allDivs = Array.from(document.querySelectorAll('div, section, article'));
                    const eventBlocks = allDivs.filter(el => {
                        const text = el.innerText || "";
                        return (text.includes('S级赛事') || text.includes('A级赛事')) && 
                               el.children.length > 5 && el.children.length < 20;
                    });
                    
                    eventBlocks.forEach(block => {
                        const text = block.innerText || "";
                        const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 0);
                        
                        const levelIdx = lines.findIndex(l => l.includes('级赛事'));
                        if (levelIdx !== -1) {
                            const level = lines[levelIdx];
                            const title = lines[levelIdx - 1] || "未知赛事";
                            
                            // Status is often '进行中', '未开始', '已结束'
                            // In some cases, it might be shifted. Let's look for known statuses.
                            let status = "未知";
                            const knownStatuses = ['进行中', '未开始', '已结束', '报名中'];
                            const foundStatus = lines.find(l => knownStatuses.includes(l));
                            if (foundStatus) {
                                status = foundStatus;
                            } else if (lines[levelIdx + 1] && !lines[levelIdx + 1].includes('-')) {
                                // If not a date range, assume it's status
                                status = lines[levelIdx + 1];
                            }
                            
                            // Time usually matches \d{2}-\d{2} - \d{2}-\d{2}
                            const time = lines.find(l => /\d{2}-\d{2}/.test(l)) || "";
                            
                            let location = "";
                            const locationIdx = lines.indexOf('地点');
                            if (locationIdx !== -1 && lines[locationIdx - 1]) {
                                location = lines[locationIdx - 1];
                            } else {
                                // Fallback: look for lines that look like locations (often contain countries or cities)
                                const possibleLocation = lines.find(l => l.includes('，') || l.includes(',') || l.includes('线上'));
                                if (possibleLocation && possibleLocation !== title && possibleLocation !== status && !possibleLocation.includes('级赛事')) {
                                    location = possibleLocation;
                                }
                            }
                            
                            let prize = "";
                            const prizeIdx = lines.indexOf('奖金');
                            if (prizeIdx !== -1 && lines[prizeIdx - 1]) {
                                prize = lines[prizeIdx - 1];
                            } else {
                                // Fallback: look for $ or '晋级'
                                const possiblePrize = lines.find(l => l.includes('$') || l.includes('晋级') || l.includes('¥'));
                                if (possiblePrize && possiblePrize !== title && possiblePrize !== status && !possiblePrize.includes('级赛事')) {
                                    prize = possiblePrize;
                                }
                            }

                            results.push({ title, level, status, time, location, prize });
                        }
                    });

                    return results;
                }""")
                
                # Filter for unique events and S/A level
                unique_events = []
                seen_titles = set()
                for e in events:
                    if e['title'] not in seen_titles:
                        unique_events.append(e)
                        seen_titles.add(e['title'])
                
                return unique_events
                
            except Exception as e:
                logger.error(f"Error crawling 5E events: {e}")
                return []
            finally:
                await browser.close()

class FiveECrawler:
    def __init__(self):
        self.base_url = "https://arena-next.5eplaycdn.com/home/personalInfo?domain={domain}&uuid=null"
        self.search_url = "https://arena.5eplay.com/search?keywords={keywords}"
        
    async def search_player(self, keywords: str):
        """
        Search for players by keywords and return a list of potential domains.
        """
        async with async_playwright() as p, await p.chromium.launch(headless=True) as browser:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.route("**/*", _route_5e_requests)
            
            url = self.search_url.format(keywords=urllib.parse.quote(keywords))
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            
            users = await page.evaluate("""() => {
                const results = [];
                const items = document.querySelectorAll('div[class*="userItem"], a[href*="/data/player/"]');
                for (const item of items) {
                    let link, name, avatar;
                    if (item.tagName === 'A') {
                        link = item;
                        name = item.innerText.trim();
                    } else {
                        link = item.querySelector('a[href*="/data/player/"]');
                        const text = item.innerText || "";
                        name = text.trim().split('\\n')[0];
                        const img = item.querySelector('img');
                        if (img) avatar = img.src;
                    }
                    
                    if (link) {
                        const href = link.getAttribute('href');
                        if (!href) continue;
                        const parts = href.split('/');
                        const domain = parts[parts.length - 1];
                        if (domain && name && !results.find(r => r.domain === domain)) {
                            results.push({name, domain, avatar});
                        }
                    }
                }
                return results;
            }""")
            
            return users

    async def get_player_data(self, domain: str, recent_match_count: int = 5):
        async with async_playwright() as p, await p.chromium.launch(headless=True) as browser:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.route("**/*", _route_5e_requests)
            
            player_data = {
                "nickname": "Unknown",
                "avatar": "",
                "stats": {}
            }
            
            # Listen for API responses
            async def handle_response(response):
                # logger.debug(f"Response: {response.url}")
                if "player_career" in response.url:
                    try:
                        data = await response.json()
                        career_data = data.get("data", {}).get("career_data", {})
                        player_data["stats"]["career"] = career_data
                        
                        # Fallback for role data if present in career
                        if not player_data["stats"].get("role") and data.get("data", {}).get("role"):
                            role_data = data.get("data", {}).get("role")
                            if role_data.get("role_name") or role_data.get("name"):
                                player_data["stats"]["role"] = {
                                    "role_name": role_data.get("role_name") or role_data.get("name"),
                                    "role_icon": role_data.get("role_icon") or role_data.get("icon"),
                                    "role_desc": role_data.get("role_desc") or role_data.get("description"),
                                    "role_tags": role_data.get("role_tags") or role_data.get("tags") or [],
                                    "player_template_name": role_data.get("player_template_name") or role_data.get("tpl_name"),
                                    "score_level": role_data.get("score_level") or role_data.get("level_name"),
                                    "score": role_data.get("score"),
                                    "rarity": role_data.get("rarity")
                                }
                    except Exception as exc:
                        logger.debug(f"Failed to parse 5E career response: {exc}")
                elif "player/best_season" in response.url:
                    try:
                        data = await response.json()
                        player_data["stats"]["best_season"] = data.get("data", {})
                    except Exception as exc:
                        logger.debug(f"Failed to parse 5E season response: {exc}")
                elif "player/home" in response.url:
                    try:
                        data = await response.json()
                        home_data = data.get("data", {})
                        player_data["stats"]["home"] = home_data
                        
                        # Fallback for role data if present in home info
                        if not player_data["stats"].get("role") and home_data.get("role"):
                            role_data = home_data.get("role")
                            if role_data.get("role_name") or role_data.get("name"):
                                player_data["stats"]["role"] = {
                                    "role_name": role_data.get("role_name") or role_data.get("name"),
                                    "role_icon": role_data.get("role_icon") or role_data.get("icon"),
                                    "role_desc": role_data.get("role_desc") or role_data.get("description"),
                                    "role_tags": role_data.get("role_tags") or role_data.get("tags") or [],
                                    "player_template_name": role_data.get("player_template_name") or role_data.get("tpl_name"),
                                    "score_level": role_data.get("score_level") or role_data.get("level_name"),
                                    "score": role_data.get("score"),
                                    "rarity": role_data.get("rarity")
                                }
                    except Exception as exc:
                        logger.debug(f"Failed to parse 5E home response: {exc}")
                elif "role_position" in response.url:
                    try:
                        resp_data = await response.json()
                        role_data = resp_data.get("data")
                        if role_data and isinstance(role_data, dict):
                            # Map API keys to template keys if necessary
                            mapped_role = {
                                "role_name": role_data.get("role_name") or role_data.get("name"),
                                "role_icon": role_data.get("role_icon") or role_data.get("icon"),
                                "role_desc": role_data.get("role_desc") or role_data.get("description"),
                                "role_tags": role_data.get("role_tags") or role_data.get("tags") or [],
                                "player_template_name": role_data.get("player_template_name") or role_data.get("tpl_name") or role_data.get("template_name"),
                                "score_level": role_data.get("score_level") or role_data.get("level_name") or role_data.get("level"),
                                "score": role_data.get("score"),
                                "rarity": role_data.get("rarity")
                            }
                            # Only set if we have a role name
                            if mapped_role["role_name"]:
                                player_data["stats"]["role"] = mapped_role
                                logger.info(f"Captured role data: {mapped_role['role_name']}")
                    except Exception as e:
                        logger.error(f"Error parsing role_position: {e}")
                elif "player_match" in response.url:
                    try:
                        data = await response.json()
                        matches = data.get("data", {}).get("match_data", [])
                        player_data["stats"]["recent_matches"] = matches[:recent_match_count]
                        
                        # Extract nickname and avatar from inferred_info if available
                        inferred = data.get("data", {}).get("inferred_info", {})
                        if inferred:
                            if inferred.get("nickname"):
                                player_data["nickname"] = inferred["nickname"]
                            if inferred.get("avatar"):
                                avatar = inferred["avatar"]
                                if avatar.startswith('//'):
                                    avatar = 'https:' + avatar
                                player_data["avatar"] = avatar
                    except Exception as exc:
                        logger.debug(f"Failed to parse 5E match response: {exc}")

            page.on("response", handle_response)
            
            url = self.base_url.format(domain=domain)
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            await asyncio.sleep(3) # Increased wait for dynamic data
            
            # Try to get nickname and avatar from the page with more flexible selectors
            try:
                # Look for elements that might contain the nickname
                nickname_el = await page.query_selector('[class*="name_box"]') or \
                              await page.query_selector('[class*="nickname"]') or \
                              await page.query_selector('[class*="player_name"]') or \
                              await page.query_selector('h1')
                
                if nickname_el:
                    text = await nickname_el.inner_text()
                    # If there's multiple lines, the first one is usually the nickname
                    player_data["nickname"] = text.split('\n')[0].strip()
                
                # Look for elements that might be the avatar
                avatar_el = await page.query_selector('[class*="avatar"] img') or \
                            await page.query_selector('img[src*="avatar"]') or \
                            await page.query_selector('img[src*="disguise"]')
                
                if avatar_el:
                    src = await avatar_el.get_attribute("src")
                    if src:
                        # Ensure it's an absolute URL
                        if src.startswith('//'):
                            src = 'https:' + src
                        elif src.startswith('/'):
                            src = 'https://arena-next.5eplay.com' + src
                        player_data["avatar"] = src
            except Exception as exc:
                logger.debug(f"Failed to extract 5E profile header: {exc}")
                
            return player_data

class PWCrawler:
    def __init__(
        self,
        *,
        token: str = "",
        steam_id: int = 0,
        persist_session: bool = False,
    ):
        self.base_url = "https://api.wmpvp.com/api"
        self.passport_url = "https://passport.pwesports.cn"
        self.app_engine_url = "https://appengine.wmpvp.com"
        self.appversion = "3.5.4.172"
        self.token = str(token or "").strip()
        self.my_steam_id = int(steam_id or 0)
        self.persist_session = bool(persist_session)
        self._session_lock = threading.RLock()
        self.session_file = get_pw_session_path()
        if self.persist_session:
            self.session_file = migrate_legacy_file("pw_session.json", self.session_file)
        if self.persist_session and not self.has_session():
            self._load_session()

    def has_session(self) -> bool:
        return bool(self.token and self.my_steam_id)

    def _load_session(self):
        """从文件加载 Session"""
        if self.persist_session and self.session_file.exists():
            try:
                ensure_private_file(self.session_file)
                with self._session_lock:
                    data = json.loads(self.session_file.read_text(encoding="utf-8"))
                    self.token = str(data.get("token") or "").strip()
                    self.my_steam_id = int(data.get("steam_id") or 0)
                if self.has_session():
                    logger.info("Loaded PW session from protected file.")
            except Exception as e:
                logger.error(f"Error loading PW session: {e}")

    def _save_session(self):
        """保存 Session 到文件"""
        if not self.persist_session:
            return
        try:
            with self._session_lock:
                secure_write_json(
                    self.session_file,
                    {"token": self.token, "steam_id": self.my_steam_id},
                )
            logger.info("Saved PW session to protected file.")
        except Exception as e:
            logger.error(f"Error saving PW session: {e}")

    def set_session(self, token: str, steam_id: int):
        with self._session_lock:
            self.token = str(token or "").strip()
            self.my_steam_id = int(steam_id or 0)
            self._save_session()

    def get_session(self) -> dict[str, Any]:
        with self._session_lock:
            return {
                "token": self.token,
                "my_steam_id": self.my_steam_id,
                "appversion": self.appversion,
            }

    def _require_session(self) -> bool:
        if self.has_session():
            return True
        logger.warning("PW session is missing. Run `pwlogin <手机号> <验证码>` first.")
        return False

    async def login(self, mobile: str, code: str) -> dict[str, Any]:
        """登录完美平台获取 token"""
        url = f"{self.passport_url}/account/login"
        payload = {
            "appId": 2,
            "mobilePhone": mobile,
            "securityCode": code
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 0:
                    acc_info = data["result"]["loginResult"]["accountInfo"]
                    self.set_session(acc_info["token"], acc_info["steamId"])
                    return acc_info
                return {"error": data.get("description", "登录失败")}
            except Exception as e:
                logger.error(f"PW Login Error: {e}")
                return {"error": "登录请求失败，请检查网络后重试"}

    async def search_player(self, keyword: str) -> list[dict[str, Any]]:
        """搜索完美世界平台玩家"""
        if not self._require_session():
            return []
        url = f"{self.app_engine_url}/steamcn/app/search/user"
        headers = {
            "appversion": self.appversion,
            "token": self.token
        }
        payload = {
            "keyword": keyword,
            "page": 1
        }
        async with httpx.AsyncClient(headers=headers) as client:
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 1:
                    return data.get("result") or []
                return []
            except Exception as e:
                logger.error(f"Error searching PW player: {e}")
                return []

    async def get_player_data(
        self,
        target_steam_id: str,
        *,
        include_recent_matches: bool = True,
        recent_match_count: int = 5,
    ) -> dict[str, Any]:
        """获取玩家详细战绩"""
        if not self._require_session():
            return {"error": "请先使用 pwlogin 登录完美平台后再查询。"}
        url = f"{self.base_url}/csgo/home/pvp/detailStats"
        headers = {
            "appversion": self.appversion,
            "token": self.token,
            "platform": "android",
            "Content-Type": "application/json"
        }
        payload = {
            "mySteamId": int(self.my_steam_id),
            "toSteamId": int(target_steam_id),
            "accessToken": "",
            "csgoSeasonId": ""
        }
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            try:
                # Fetch detailed stats
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                
                if data.get("statusCode") == 0:
                    pw_data = data.get("data", {})
                    if not pw_data:
                        logger.warning(f"PW API returned success but empty data for {target_steam_id}")
                        return {"error": "API 返回数据为空，请检查玩家是否在该赛季有战绩"}
                        
                    # Fetch recent matches
                    recent_matches = (
                        await self.get_recent_matches(target_steam_id, recent_match_count)
                        if include_recent_matches
                        else []
                    )
                    
                    return {
                        "summary": {
                            "nickname": pw_data.get("name"),
                            "avatarUrl": pw_data.get("avatar"),
                            "steamId": pw_data.get("steamId"),
                            "description": pw_data.get("summary")
                        },
                        "stats": pw_data,
                        "recent_matches": recent_matches
                    }
                else:
                    error_msg = data.get("errorMessage") or f"Status Code: {data.get('statusCode')}"
                    logger.error(f"PW API Error for {target_steam_id}: {error_msg}")
                    return {"error": error_msg}
            except Exception as e:
                logger.error(f"Error getting PW player data: {e}")
                return {"error": f"网络请求失败: {e!s}"}

    async def get_recent_matches(self, target_steam_id: str, count: int = 5) -> list[dict[str, Any]]:
        """获取玩家最近比赛记录"""
        if not self._require_session():
            return []
        url = f"{self.base_url}/csgo/home/match/list"
        headers = {
            "appversion": self.appversion,
            "token": self.token,
            "platform": "android"
        }
        payload = {
            "csgoSeasonId": "recent",
            "dataSource": 3,
            "mySteamId": int(self.my_steam_id),
            "page": 1,
            "pageSize": count,
            "pvpType": -1,
            "toSteamId": int(target_steam_id)
        }
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("statusCode") == 0:
                    match_list = data.get("data", {}).get("matchList", [])
                    return match_list
                else:
                    logger.error(f"PW Match List API Error: {data.get('errorMessage')}")
                    return []
            except Exception as e:
                logger.error(f"Error getting PW recent matches: {e}")
                return []
