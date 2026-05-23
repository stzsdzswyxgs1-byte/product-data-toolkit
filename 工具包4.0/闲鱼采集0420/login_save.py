"""
登录脚本 — 扫码登录后保存cookie
1. 打开浏览器 (persistent profile 自动保留cookie)
2. --force-login: 使用临时干净profile, 跳过已登录检测
3. 普通模式: 检测是否已登录且token有效 → 已登录直接退出
4. 未登录或token过期 → 等你扫码/刷新 → 保存cookie到JSON备份
"""
import asyncio, json, os, sys, time, tempfile, shutil
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright
from pathlib import Path

STEALTH_JS = """
// 隐藏 webdriver 标记
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
try { delete navigator.__proto__.webdriver; } catch(e) {}

// 补全 chrome 对象 (Playwright启动时可能缺失部分属性)
if (!window.chrome || !window.chrome.runtime) {
    window.chrome = {
        runtime: { onMessage: { addListener: function(){} }, onConnect: { addListener: function(){} } },
        loadTimes: function(){ return {}; },
        csi: function(){ return {}; },
        app: { isInstalled: false }
    };
}

// 权限查询修复
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery(parameters);

// 隐藏自动化全局变量
['callPhantom','_phantom','__nightmare','domAutomation','domAutomationController',
 '_Selenium_IDE_Recorder','_selenium','__webdriver_evaluate','__driver_evaluate'
].forEach(p => {
    try { Object.defineProperty(window, p, { get: () => undefined }); } catch(e) {}
});
"""

PROFILE_DIR = Path(__file__).parent / 'browser_profile'
COOKIE_FILE = Path(__file__).parent / 'cookies.json'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'


async def save_cookies(browser):
    """显式保存cookie到JSON文件"""
    cookies = await browser.cookies()
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f'  Cookie已保存到 {COOKIE_FILE} ({len(cookies)} 条)')


async def check_token_valid(browser):
    """检查 _m_h5_tk cookie 是否存在且未过期"""
    cookies = await browser.cookies()
    now = time.time()
    for c in cookies:
        if c.get('name') == '_m_h5_tk':
            expires = c.get('expires', 0)
            if expires > 0 and expires > now:
                return True
            return False
    return False


async def wait_for_fresh_token(page, browser, timeout=15):
    """等待页面JS设置新的 _m_h5_tk cookie"""
    for i in range(timeout):
        if await check_token_valid(browser):
            return True
        await asyncio.sleep(1)
    return False


async def _wait_for_login(page, browser):
    """等待扫码登录 (最多10分钟)"""
    print('\n[*] 请在浏览器中扫码登录闲鱼')
    print('    登录成功后自动保存cookie (最多等10分钟)')

    for attempt in range(600):
        try:
            is_logged = await page.evaluate('''() => {
                return document.cookie.includes('unb=') || document.cookie.includes('sid=');
            }''')
        except Exception:
            await asyncio.sleep(2)
            continue
        if is_logged:
            print(f'\n[*] 登录成功! (耗时 {attempt+1}s)')
            await asyncio.sleep(3)
            try:
                await page.goto('https://www.goofish.com/', wait_until='domcontentloaded', timeout=30000)
            except Exception:
                await asyncio.sleep(3)
            await asyncio.sleep(5)

            if await check_token_valid(browser):
                print('[*] Token验证通过!')
            else:
                print('[!] 等待token生成...')
                await wait_for_fresh_token(page, browser, timeout=15)

            await save_cookies(browser)
            return True
        await asyncio.sleep(1)
        if attempt % 15 == 14:
            print(f'    等待中... ({attempt+1}s)')

    print('[!] 等待超时 (10分钟)')
    return False


async def main(force=False):
    if force:
        # 强制重登: 使用临时干净profile, 彻底避免旧状态残留
        tmp_profile = tempfile.mkdtemp(prefix='xianyu_login_')
        print(f'[*] 强制重登模式: 使用临时profile')
        profile_dir = tmp_profile
    else:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        profile_dir = str(PROFILE_DIR)
        tmp_profile = None

    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            channel='chrome',
            viewport={'width': 1280, 'height': 800},
            user_agent=UA,
            args=['--disable-blink-features=AutomationControlled', '--disable-infobars',
                  '--no-first-run', '--no-default-browser-check'],
            ignore_default_args=['--enable-automation'],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.add_init_script(STEALTH_JS)

        print('[*] 打开 goofish.com ...')
        await page.goto('https://www.goofish.com/', wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(3)

        if not force:
            # 普通模式: 检查是否已登录
            is_logged = await page.evaluate('''() => {
                return document.cookie.includes('unb=') || document.cookie.includes('sid=');
            }''')

            if is_logged:
                print('[*] 检测到登录状态, 验证token...')
                await page.goto('https://www.goofish.com/', wait_until='domcontentloaded', timeout=30000)
                await asyncio.sleep(5)

                if await check_token_valid(browser):
                    print('[*] Token有效!')
                    await save_cookies(browser)
                    await browser.close()
                    await pw.stop()
                    print('[*] 完成')
                    return
                else:
                    print('[!] Token已过期, 需要重新登录')
                    print('    正在清除旧登录状态...')
                    await browser.clear_cookies()
                    await page.goto('https://www.goofish.com/', wait_until='domcontentloaded', timeout=30000)
                    await asyncio.sleep(3)

        # 等待扫码登录
        logged = await _wait_for_login(page, browser)
        if logged:
            print('[*] 完成')
        else:
            print('[!] 登录未完成')

        await browser.close()
        browser = None
        await pw.stop()

    finally:
        # 确保浏览器关闭
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
        # 清理临时profile (Windows下等一下再删)
        if tmp_profile and os.path.exists(tmp_profile):
            await asyncio.sleep(1)
            shutil.rmtree(tmp_profile, ignore_errors=True)


if __name__ == '__main__':
    force = '--force-login' in sys.argv
    asyncio.run(main(force=force))
