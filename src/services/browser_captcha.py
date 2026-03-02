"""
基于 RT 的本地 reCAPTCHA 打码服务 (终极闭环版 - 无 fake_useragent 纯净版)
支持：自动刷新 Session Token、外部触发指纹切换、死磕重试
"""
import os
import sys
import subprocess
# 修复 Windows 上 playwright 的 asyncio 兼容性问题
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import asyncio
import time
import re
import random
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
from urllib.parse import urlparse, unquote

from ..core.logger import debug_logger


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return True
    # 方法2: 检查 cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


# ==================== playwright 自动安装 ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令"""
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] 正在安装 {package}...")
        print(f"[BrowserCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} 安装成功")
            print(f"[BrowserCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装异常: {e}")
        return False


def _run_playwright_install(use_mirror: bool = False) -> bool:
    """安装 playwright chromium 浏览器"""
    cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
    env = os.environ.copy()
    
    if use_mirror:
        # 使用国内镜像
        env['PLAYWRIGHT_DOWNLOAD_HOST'] = 'https://npmmirror.com/mirrors/playwright'
    
    try:
        debug_logger.log_info("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        print("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if result.returncode == 0:
            debug_logger.log_info("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            print("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装异常: {e}")
        return False


def _ensure_playwright_installed() -> bool:
    """确保 playwright 已安装"""
    try:
        import playwright
        debug_logger.log_info("[BrowserCaptcha] playwright 已安装")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    print("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_pip_install('playwright', use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('playwright', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    print("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    return False


def _ensure_browser_installed() -> bool:
    """确保 chromium 浏览器已安装"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # 尝试获取浏览器路径，如果失败说明未安装
            browser_path = p.chromium.executable_path
            if browser_path and os.path.exists(browser_path):
                debug_logger.log_info(f"[BrowserCaptcha] chromium 浏览器已安装: {browser_path}")
                return True
    except Exception as e:
        debug_logger.log_info(f"[BrowserCaptcha] 检测浏览器时出错: {e}")
    
    debug_logger.log_info("[BrowserCaptcha] chromium 浏览器未安装，开始自动安装...")
    print("[BrowserCaptcha] chromium 浏览器未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_playwright_install(use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_playwright_install(use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
    print("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
    return False


# 尝试导入 playwright
async_playwright = None
Route = None
BrowserContext = None
PLAYWRIGHT_AVAILABLE = False

if IS_DOCKER:
    debug_logger.log_warning("[BrowserCaptcha] 检测到 Docker 环境，有头浏览器打码不可用，请使用第三方打码服务")
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，有头浏览器打码不可用")
    print("[BrowserCaptcha] 请使用第三方打码服务: yescaptcha, capmonster, ezcaptcha, capsolver")
else:
    if _ensure_playwright_installed():
        try:
            from playwright.async_api import async_playwright, Route, BrowserContext
            PLAYWRIGHT_AVAILABLE = True
            # 检查并安装浏览器
            _ensure_browser_installed()
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] playwright 导入失败: {e}")
            print(f"[BrowserCaptcha] ❌ playwright 导入失败: {e}")


# 配置
LABS_URL = "https://labs.google/fx/tools/flow"

# ==========================================
# 代理解析工具函数
# ==========================================
def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL"""
    if not proxy_url: return None
    if not re.match(r'^(http|https|socks5)://', proxy_url): proxy_url = f"http://{proxy_url}"
    match = re.match(r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', proxy_url)
    if match:
        protocol, username, password, host, port = match.groups()
        proxy_config = {'server': f'{protocol}://{host}:{port}'}
        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password
        return proxy_config
    return None

def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    if not proxy_url: return True, None
    parsed = parse_proxy_url(proxy_url)
    if not parsed: return False, "代理格式错误"
    return True, None

class TokenBrowser:
    """CDP browser: connects to a running Google Chrome instance via DevTools Protocol.

    Chrome runs as a systemd service with a persistent profile (logged into Google),
    giving the highest reCAPTCHA trust score.
    """

    CDP_ENDPOINT = "http://127.0.0.1:9222"

    def __init__(self, token_id: int, user_data_dir: str, db=None):
        self.token_id = token_id
        self.user_data_dir = user_data_dir  # unused with CDP, kept for compat
        self.db = db
        self._semaphore = asyncio.Semaphore(1)
        self._solve_count = 0
        self._error_count = 0

    async def _create_browser(self) -> tuple:
        """Connect to running Chrome instance via CDP, returns (playwright, browser, context)"""
        playwright = await async_playwright().start()

        try:
            browser = await playwright.chromium.connect_over_cdp(self.CDP_ENDPOINT)
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = await browser.new_context()
            debug_logger.log_info(f"[BrowserCaptcha] Token-{self.token_id} connected to Chrome via CDP")
            return playwright, browser, context
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] Token-{self.token_id} CDP connection failed: {type(e).__name__}: {str(e)[:200]}")
            try:
                await playwright.stop()
            except: pass
            raise

    async def _close_browser(self, playwright, browser, context):
        """Disconnect from Chrome (do NOT close browser/context - Chrome keeps running)"""
        try:
            if browser:
                browser.close()
        except: pass
        try:
            if playwright:
                await playwright.stop()
        except: pass
    
    async def _execute_captcha(self, context, project_id: str, website_key: str, action: str) -> Optional[str]:
        """在给定 context 中执行打码逻辑"""
        page = None
        try:
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            
            page_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            
            async def handle_route(route):
                if route.request.url.rstrip('/') == page_url.rstrip('/'):
                    html = f"""<html><head><script src="https://www.google.com/recaptcha/enterprise.js?render={website_key}"></script></head><body></body></html>"""
                    await route.fulfill(status=200, content_type="text/html", body=html)
                elif any(d in route.request.url for d in ["google.com", "gstatic.com", "recaptcha.net"]):
                    await route.continue_()
                else:
                    await route.abort()
            
            await page.route("**/*", handle_route)
            try:
                await page.goto(page_url, wait_until="load", timeout=30000)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} page.goto 失败: {type(e).__name__}: {str(e)[:200]}")
                return None
            
            try:
                await page.wait_for_function("typeof grecaptcha !== 'undefined'", timeout=15000)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} grecaptcha 未就绪: {type(e).__name__}: {str(e)[:200]}")
                return None
            
            token = await asyncio.wait_for(
                page.evaluate(f"""
                    (actionName) => {{
                        return new Promise((resolve, reject) => {{
                            const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                            grecaptcha.enterprise.execute('{website_key}', {{action: actionName}})
                                .then(t => {{ resolve(t); }})
                                .catch(e => {{ reject(e); }});
                        }});
                    }}
                """, action),
                timeout=30
            )
            return token
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 打码失败: {msg[:200]}")
            return None
        finally:
            if page:
                try: await page.close()
                except: pass
    
    async def get_token(self, project_id: str, website_key: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """获取 Token：启动新浏览器 -> 打码 -> 关闭浏览器"""
        async with self._semaphore:
            MAX_RETRIES = 3
            
            for attempt in range(MAX_RETRIES):
                playwright = None
                browser = None
                context = None
                try:
                    start_ts = time.time()
                    
                    # 每次都启动新浏览器（新 UA）
                    playwright, browser, context = await self._create_browser()
                    
                    # 执行打码
                    token = await self._execute_captcha(context, project_id, website_key, action)
                    
                    if token:
                        self._solve_count += 1
                        debug_logger.log_info(f"[BrowserCaptcha] Token-{self.token_id} 获取成功 ({(time.time()-start_ts)*1000:.0f}ms)")
                        return token
                    
                    self._error_count += 1
                    debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 尝试 {attempt+1}/{MAX_RETRIES} 失败")
                    
                except Exception as e:
                    self._error_count += 1
                    debug_logger.log_error(f"[BrowserCaptcha] Token-{self.token_id} 浏览器错误: {type(e).__name__}: {str(e)[:200]}")
                finally:
                    # 无论成功失败都关闭浏览器
                    await self._close_browser(playwright, browser, context)
                
                # 重试前等待
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
            
            return None
    

class BrowserCaptchaService:
    """多浏览器轮询打码服务（单例模式）
    
    支持配置浏览器数量，每个浏览器只开 1 个标签页，请求轮询分配
    """
    
    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()
    
    def __init__(self, db=None):
        self.db = db
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.base_user_data_dir = os.path.join(os.getcwd(), "browser_data_rt")
        self._browsers: Dict[int, TokenBrowser] = {}
        self._browsers_lock = asyncio.Lock()
        
        # 浏览器数量配置
        self._browser_count = 1  # 默认 1 个，会从数据库加载
        self._round_robin_index = 0  # 轮询索引
        
        # 统计指标
        self._stats = {
            "req_total": 0,
            "gen_ok": 0,
            "gen_fail": 0,
            "api_403": 0
        }
        
        # 并发限制将在 _load_browser_count 中根据配置设置
        self._token_semaphore = None
    
    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # 从数据库加载 browser_count 配置
                    await cls._instance._load_browser_count()
        return cls._instance
    
    def _check_available(self):
        """检查服务是否可用"""
        if IS_DOCKER:
            raise RuntimeError(
                "有头浏览器打码在 Docker 环境中不可用。"
                "请使用第三方打码服务: yescaptcha, capmonster, ezcaptcha, capsolver"
            )
        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            raise RuntimeError(
                "playwright 未安装或不可用。"
                "请手动安装: pip install playwright && python -m playwright install chromium"
            )
    
    async def _load_browser_count(self):
        """从数据库加载浏览器数量配置"""
        if self.db:
            try:
                captcha_config = await self.db.get_captcha_config()
                self._browser_count = max(1, captcha_config.browser_count)
                debug_logger.log_info(f"[BrowserCaptcha] 浏览器数量配置: {self._browser_count}")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 加载 browser_count 配置失败: {e}，使用默认值 1")
                self._browser_count = 1
        # 并发限制 = 浏览器数量，不再硬编码限制
        self._token_semaphore = asyncio.Semaphore(self._browser_count)
        debug_logger.log_info(f"[BrowserCaptcha] 并发上限: {self._browser_count}")
    
    async def reload_browser_count(self):
        """重新加载浏览器数量配置（用于配置更新后热重载）"""
        old_count = self._browser_count
        await self._load_browser_count()
        
        # 如果数量减少，移除多余的浏览器实例
        if self._browser_count < old_count:
            async with self._browsers_lock:
                for browser_id in list(self._browsers.keys()):
                    if browser_id >= self._browser_count:
                        self._browsers.pop(browser_id)
                        debug_logger.log_info(f"[BrowserCaptcha] 移除多余浏览器实例 {browser_id}")
    
    def _log_stats(self):
        total = self._stats["req_total"]
        gen_fail = self._stats["gen_fail"]
        api_403 = self._stats["api_403"]
        gen_ok = self._stats["gen_ok"]
        
        valid_success = gen_ok - api_403
        if valid_success < 0: valid_success = 0
        
        rate = (valid_success / total * 100) if total > 0 else 0.0

    
    async def _get_or_create_browser(self, browser_id: int) -> TokenBrowser:
        """获取或创建指定 ID 的浏览器实例"""
        async with self._browsers_lock:
            if browser_id not in self._browsers:
                user_data_dir = os.path.join(self.base_user_data_dir, f"browser_{browser_id}")
                browser = TokenBrowser(browser_id, user_data_dir, db=self.db)
                self._browsers[browser_id] = browser
                debug_logger.log_info(f"[BrowserCaptcha] 创建浏览器实例 {browser_id}")
            return self._browsers[browser_id]
    
    def _get_next_browser_id(self) -> int:
        """轮询获取下一个浏览器 ID"""
        browser_id = self._round_robin_index % self._browser_count
        self._round_robin_index += 1
        return browser_id
    
    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION", token_id: int = None) -> tuple[Optional[str], int]:
        """获取 reCAPTCHA Token（轮询分配到不同浏览器）
        
        Args:
            project_id: 项目 ID
            action: reCAPTCHA action
            token_id: 忽略，使用轮询分配
        
        Returns:
            (token, browser_id) 元组，调用方失败时用 browser_id 调用 report_error
        """
        # 检查服务是否可用
        self._check_available()
        
        self._stats["req_total"] += 1
        
        # 全局并发限制（如果已配置）
        if self._token_semaphore:
            async with self._token_semaphore:
                # 轮询选择浏览器
                browser_id = self._get_next_browser_id()
                browser = await self._get_or_create_browser(browser_id)
                
                token = await browser.get_token(project_id, self.website_key, action)
            
            if token:
                self._stats["gen_ok"] += 1
            else:
                self._stats["gen_fail"] += 1
                
            self._log_stats()
            return token, browser_id
        
        # 无并发限制时直接执行
        browser_id = self._get_next_browser_id()
        browser = await self._get_or_create_browser(browser_id)
        
        token = await browser.get_token(project_id, self.website_key, action)
        
        if token:
            self._stats["gen_ok"] += 1
        else:
            self._stats["gen_fail"] += 1
            
        self._log_stats()
        return token, browser_id

    async def report_error(self, browser_id: int = None):
        """上层举报：Token 无效（统计用）
        
        Args:
            browser_id: 浏览器 ID（当前架构下每次都是新浏览器，此参数仅用于日志）
        """
        async with self._browsers_lock:
            self._stats["api_403"] += 1
            if browser_id is not None:
                debug_logger.log_info(f"[BrowserCaptcha] 浏览器 {browser_id} 的 token 验证失败")

    async def remove_browser(self, browser_id: int):
        async with self._browsers_lock:
            if browser_id in self._browsers:
                self._browsers.pop(browser_id)

    async def close(self):
        async with self._browsers_lock:
            self._browsers.clear()
            
    async def open_login_browser(self): return {"success": False, "error": "Not implemented"}
    async def create_browser_for_token(self, t, s=None): pass
    def get_stats(self): 
        base_stats = {
            "total_solve_count": self._stats["gen_ok"],
            "total_error_count": self._stats["gen_fail"],
            "risk_403_count": self._stats["api_403"],
            "browser_count": len(self._browsers),
            "configured_browser_count": self._browser_count,
            "browsers": []
        }
        return base_stats
