#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猫眼操作录制器
============

这个工具会：
1. 打开猫眼网站
2. 让你手动完成一次完整的购票流程
3. 记录下每一步的操作和页面状态
4. 生成针对猫眼的自动化代码

使用方法：
    python recorder.py

然后在浏览器中手动操作，完成后按 Ctrl+C 停止。
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Page


class MaoyanRecorder:
    """猫眼操作录制器"""

    def __init__(self):
        self.actions = []
        self.screenshots = []
        self.api_calls = []
        self.current_step = 0

    async def record(self):
        """开始录制"""
        print("=" * 60)
        print("猫眼操作录制器")
        print("=" * 60)
        print("\n准备工作：")
        print("1. 确保你有猫眼账号")
        print("2. 准备好要购买的电影信息")
        print("3. 浏览器会自动打开，你手动完成购票流程")
        print("4. 完成后关闭浏览器或按 Ctrl+C")
        print("\n即将打开浏览器...\n")

        async with async_playwright() as p:
            # 启动浏览器（非无头模式，方便操作）
            browser = await p.chromium.launch(
                headless=False,
                args=['--start-maximized']
            )

            context = await browser.new_context(
                viewport=None,
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            )

            page = await context.new_page()

            # 监听网络请求
            page.on('request', lambda req: self._on_request(req))
            page.on('response', lambda resp: self._on_response(resp))

            # 监听页面导航
            page.on('framenavigated', lambda frame: self._on_navigation(frame))

            try:
                # 访问猫眼
                print("正在打开猫眼网站...")
                await page.goto('https://www.maoyan.com')

                print("\n✅ 浏览器已打开！")
                print("\n请在浏览器中完成以下操作：")
                print("=" * 60)
                print("第 1 步：登录账号（如果需要）")
                print("第 2 步：搜索电影（例如：奥德赛）")
                print("第 3 步：选择影城")
                print("第 4 步：选择场次")
                print("第 5 步：进入选座页面（不要真的购买！）")
                print("第 6 步：观察座位图")
                print("=" * 60)
                print("\n提示：")
                print("- 每一步操作后会自动截图")
                print("- 所有网络请求会被记录")
                print("- 完成后直接关闭浏览器")
                print("\n开始录制...\n")

                # 等待用户操作
                await self._wait_for_user(page)

            except KeyboardInterrupt:
                print("\n\n录制已停止")
            except Exception as e:
                print(f"\n错误：{e}")
            finally:
                # 保存录制结果
                await self.save_recording()
                await browser.close()

    async def _wait_for_user(self, page: Page):
        """等待用户操作"""
        step_hints = [
            "等待登录...",
            "等待搜索电影...",
            "等待选择影城...",
            "等待选择场次...",
            "等待进入选座...",
            "录制完成！"
        ]

        while True:
            try:
                # 每隔 5 秒检查一次页面状态
                await asyncio.sleep(5)

                # 截图
                await self._take_screenshot(page)

                # 记录当前状态
                current_url = page.url
                title = await page.title()

                self.actions.append({
                    'step': self.current_step,
                    'timestamp': datetime.now().isoformat(),
                    'url': current_url,
                    'title': title
                })

                # 简单的步骤推断
                if '登录' in title or 'login' in current_url:
                    self._print_step(0, "检测到登录页面")
                elif '搜索' in current_url or 'search' in current_url:
                    self._print_step(1, "检测到搜索结果")
                elif 'cinema' in current_url or '影城' in title:
                    self._print_step(2, "检测到影城页面")
                elif 'show' in current_url or '场次' in title:
                    self._print_step(3, "检测到场次页面")
                elif 'seat' in current_url or '选座' in title:
                    self._print_step(4, "检测到选座页面")
                    print("\n✅ 已进入选座页面！可以关闭浏览器了")

            except Exception as e:
                print(f"监控出错：{e}")
                break

    def _on_request(self, request):
        """记录请求"""
        url = request.url
        method = request.method

        # 只记录 API 请求
        if any(keyword in url for keyword in ['ajax', 'api', 'search', 'cinema', 'show', 'seat', 'order']):
            self.api_calls.append({
                'type': 'request',
                'method': method,
                'url': url,
                'headers': request.headers,
                'timestamp': datetime.now().isoformat()
            })
            print(f"📤 {method} {url}")

    def _on_response(self, response):
        """记录响应"""
        url = response.url

        # 只记录 API 响应
        if any(keyword in url for keyword in ['ajax', 'api', 'search', 'cinema', 'show', 'seat', 'order']):
            print(f"📥 {response.status} {url}")

    def _on_navigation(self, frame):
        """记录页面导航"""
        if frame == frame.page.main_frame:
            url = frame.url
            print(f"🔗 导航到: {url}")

    async def _take_screenshot(self, page: Page):
        """截图"""
        try:
            screenshot_dir = Path('recordings/screenshots')
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            filename = f"step_{self.current_step}_{datetime.now():%H%M%S}.png"
            filepath = screenshot_dir / filename

            await page.screenshot(path=str(filepath))
            self.screenshots.append(str(filepath))

        except Exception as e:
            print(f"截图失败：{e}")

    def _print_step(self, step_num, message):
        """打印步骤信息"""
        if step_num > self.current_step:
            self.current_step = step_num
            print(f"\n✓ 步骤 {step_num + 1}: {message}")

    async def save_recording(self):
        """保存录制结果"""
        print("\n保存录制数据...")

        recording_dir = Path('recordings')
        recording_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        # 保存操作记录
        actions_file = recording_dir / f'actions_{timestamp}.json'
        with open(actions_file, 'w', encoding='utf-8') as f:
            json.dump(self.actions, f, ensure_ascii=False, indent=2)
        print(f"✅ 操作记录: {actions_file}")

        # 保存 API 调用
        api_file = recording_dir / f'api_calls_{timestamp}.json'
        with open(api_file, 'w', encoding='utf-8') as f:
            json.dump(self.api_calls, f, ensure_ascii=False, indent=2)
        print(f"✅ API 记录: {api_file}")

        # 生成分析报告
        await self._generate_report(recording_dir / f'report_{timestamp}.md')

    async def _generate_report(self, filepath):
        """生成分析报告"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# 猫眼操作录制报告\n\n")
            f.write(f"录制时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("## 操作流程\n\n")
            for i, action in enumerate(self.actions, 1):
                f.write(f"### 步骤 {i}\n")
                f.write(f"- 时间: {action['timestamp']}\n")
                f.write(f"- URL: {action['url']}\n")
                f.write(f"- 页面标题: {action['title']}\n\n")

            f.write("## API 调用分析\n\n")

            # 按类型分组
            api_types = {}
            for call in self.api_calls:
                url = call['url']
                if 'search' in url:
                    key = '搜索'
                elif 'cinema' in url:
                    key = '影城'
                elif 'show' in url:
                    key = '场次'
                elif 'seat' in url:
                    key = '座位'
                elif 'order' in url:
                    key = '订单'
                else:
                    key = '其他'

                if key not in api_types:
                    api_types[key] = []
                api_types[key].append(call)

            for api_type, calls in api_types.items():
                f.write(f"### {api_type} API\n\n")
                for call in calls:
                    f.write(f"```\n")
                    f.write(f"{call['method']} {call['url']}\n")
                    f.write(f"```\n\n")

            f.write("## 下一步建议\n\n")
            f.write("1. 查看 `api_calls_*.json` 文件，找出关键 API\n")
            f.write("2. 分析请求参数和响应格式\n")
            f.write("3. 根据真实 API 改写 app.py\n")
            f.write("4. 测试每个 API 调用\n")

        print(f"✅ 分析报告: {filepath}")


async def main():
    recorder = MaoyanRecorder()
    await recorder.record()

    print("\n" + "=" * 60)
    print("录制完成！")
    print("=" * 60)
    print("\n请查看 recordings/ 目录下的文件：")
    print("- actions_*.json: 操作记录")
    print("- api_calls_*.json: API 调用记录")
    print("- report_*.md: 分析报告")
    print("- screenshots/: 截图")
    print("\n下一步：根据这些数据改写 app.py")


if __name__ == '__main__':
    asyncio.run(main())
